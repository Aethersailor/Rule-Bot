import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Add repo root to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.data_manager import DataManager


def _build_config(data_dir: str, interval: float = 0.05) -> SimpleNamespace:
    return SimpleNamespace(
        DATA_DIR=data_dir,
        GEOSITE_CACHE_SIZE=32,
        GEOSITE_CACHE_TTL=60,
        DATA_UPDATE_INTERVAL=interval,
        GEOIP_URLS=[],
        CN_IPV4_URLS=[],
        GEOSITE_URL="",
    )


def _geosite_payload(prefix: str, count: int) -> bytes:
    return "\n".join(
        f"domain:{prefix}-{index}.example" for index in range(count)
    ).encode()


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers=None, on_enter=None):
        self.status = status
        self.headers = headers or {}
        self.content = self
        self._body = body
        self._on_enter = on_enter

    async def __aenter__(self):
        if self._on_enter:
            self._on_enter()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def iter_chunked(self, chunk_size: int):
        yield self._body


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": headers or {}})
        return self.responses.pop(0)


class TestDataManagerScheduling(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_runs_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _build_config(temp_dir, interval=0.05)
            manager = DataManager(config)

            update_called = asyncio.Event()

            async def _fake_update():
                update_called.set()

            with patch.object(manager, "_download_initial_data", AsyncMock()):
                with patch.object(manager, "_update_data", AsyncMock(side_effect=_fake_update)):
                    await manager.initialize()
                    await asyncio.wait_for(update_called.wait(), timeout=0.5)
                    self.assertIsNotNone(manager._scheduler_task)
                    self.assertFalse(manager._scheduler_task.done())

                    await manager.close()
                    self.assertIsNone(manager._scheduler_task)

    async def test_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _build_config(temp_dir, interval=3600)
            manager = DataManager(config)

            session1 = await manager._get_session()
            session2 = await manager._get_session()

            self.assertIs(session1, session2)
            self.assertFalse(session1.closed)

            await manager.close()
            self.assertTrue(session1.closed)
            self.assertIsNone(manager._session)

    async def test_download_without_local_file_ignores_stale_meta_headers(self):
        class FakeResponse:
            def __init__(self, body: bytes):
                self.status = 200
                self.headers = {}
                self.content = self
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def iter_chunked(self, chunk_size: int):
                yield self._body

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None):
                self.calls.append({"url": url, "headers": headers or {}})
                return FakeResponse(b"payload")

        with tempfile.TemporaryDirectory() as temp_dir:
            config = _build_config(temp_dir, interval=3600)
            manager = DataManager(config)
            fake_session = FakeSession()

            meta = {
                "etag": "stale-etag",
                "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            }
            manager.geoip_meta.write_text(json.dumps(meta), encoding="utf-8")

            with patch.object(manager, "_get_session", AsyncMock(return_value=fake_session)):
                with patch.object(manager, "_validate_download"):
                    changed = await manager._download_with_fallback(
                        ["https://example.test/geoip.mmdb"],
                        manager.geoip_file,
                        "geoip",
                        manager.geoip_meta,
                    )

            self.assertTrue(changed)
            self.assertEqual(fake_session.calls[0]["headers"], {})
            self.assertTrue(manager.geoip_file.exists())

    async def test_initial_download_replaces_fresh_but_invalid_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DataManager(_build_config(temp_dir, interval=3600))

            def inspect(path, label, meta_path):
                if label == "geoip":
                    return False, None, None
                return True, "valid", 100

            with patch.object(manager, "_inspect_existing_data", side_effect=inspect):
                with patch.object(manager, "_is_file_outdated", return_value=False):
                    with patch.object(manager, "_download_geoip", AsyncMock(return_value=True)) as geoip:
                        with patch.object(manager, "_download_cn_ipv4", AsyncMock()) as cn_ipv4:
                            with patch.object(manager, "_download_geosite", AsyncMock()) as geosite:
                                with patch.object(manager, "_load_geosite_data", AsyncMock()):
                                    await manager._download_initial_data()

            geoip.assert_awaited_once()
            cn_ipv4.assert_not_awaited()
            geosite.assert_not_awaited()

    async def test_initial_download_never_falls_back_to_invalid_existing_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DataManager(_build_config(temp_dir, interval=3600))
            manager.geoip_file.write_bytes(b"corrupt but present")

            def inspect(path, label, meta_path):
                if label == "geoip":
                    return False, None, None
                return True, "valid", 100

            with patch.object(manager, "_inspect_existing_data", side_effect=inspect):
                with patch.object(manager, "_is_file_outdated", return_value=False):
                    with patch.object(
                        manager,
                        "_download_geoip",
                        AsyncMock(side_effect=RuntimeError("network unavailable")),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "geoip"):
                            await manager._download_initial_data()

    async def test_conditional_headers_are_scoped_to_meta_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DataManager(_build_config(temp_dir, interval=3600))
            old_payload = _geosite_payload("old", 120)
            new_payload = _geosite_payload("new", 120)
            manager.geosite_file.write_bytes(old_payload)
            manager.geosite_meta.write_text(
                json.dumps(
                    {
                        "source": "https://mirror.example/geosite",
                        "etag": "mirror-etag",
                        "sha256": hashlib.sha256(old_payload).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            session = _FakeSession([_FakeResponse(200, new_payload)])

            with patch.object(manager, "_get_session", AsyncMock(return_value=session)):
                changed = await manager._download_with_fallback(
                    ["https://primary.example/geosite"],
                    manager.geosite_file,
                    "geosite",
                    manager.geosite_meta,
                )

            self.assertTrue(changed)
            self.assertEqual(session.calls[0]["headers"], {})
            self.assertEqual(manager.geosite_file.read_bytes(), new_payload)

    async def test_corrupt_data_after_304_is_downloaded_unconditionally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DataManager(_build_config(temp_dir, interval=3600))
            url = "https://primary.example/geosite"
            old_payload = _geosite_payload("old", 120)
            new_payload = _geosite_payload("new", 120)
            manager.geosite_file.write_bytes(old_payload)
            manager.geosite_meta.write_text(
                json.dumps(
                    {
                        "source": url,
                        "etag": "current-etag",
                        "sha256": hashlib.sha256(old_payload).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

            def corrupt_local_file():
                manager.geosite_file.write_text("<html>broken</html>", encoding="utf-8")

            session = _FakeSession(
                [
                    _FakeResponse(304, on_enter=corrupt_local_file),
                    _FakeResponse(200, new_payload),
                ]
            )

            with patch.object(manager, "_get_session", AsyncMock(return_value=session)):
                changed = await manager._download_with_fallback(
                    [url],
                    manager.geosite_file,
                    "geosite",
                    manager.geosite_meta,
                )

            self.assertTrue(changed)
            self.assertEqual(session.calls[0]["headers"], {"If-None-Match": "current-etag"})
            self.assertEqual(session.calls[1]["headers"], {})
            self.assertEqual(manager.geosite_file.read_bytes(), new_payload)

    async def test_meta_hash_match_does_not_hide_corrupt_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DataManager(_build_config(temp_dir, interval=3600))
            url = "https://primary.example/geosite"
            valid_payload = _geosite_payload("valid", 120)
            manager.geosite_file.write_text("<html>broken</html>", encoding="utf-8")
            manager.geosite_meta.write_text(
                json.dumps(
                    {
                        "source": url,
                        "etag": "stale-etag",
                        "sha256": hashlib.sha256(valid_payload).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            session = _FakeSession([_FakeResponse(200, valid_payload)])

            with patch.object(manager, "_get_session", AsyncMock(return_value=session)):
                changed = await manager._download_with_fallback(
                    [url],
                    manager.geosite_file,
                    "geosite",
                    manager.geosite_meta,
                )

            self.assertTrue(changed)
            self.assertEqual(session.calls[0]["headers"], {})
            self.assertEqual(manager.geosite_file.read_bytes(), valid_payload)

    async def test_suspicious_shrink_keeps_last_valid_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DataManager(_build_config(temp_dir, interval=3600))
            url = "https://primary.example/geosite"
            old_payload = _geosite_payload("old", 400)
            smaller_payload = _geosite_payload("small", 150)
            manager.geosite_file.write_bytes(old_payload)
            manager.geosite_meta.write_text(
                json.dumps(
                    {
                        "source": url,
                        "sha256": hashlib.sha256(old_payload).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            session = _FakeSession([_FakeResponse(200, smaller_payload)])

            with patch.object(manager, "_get_session", AsyncMock(return_value=session)):
                with self.assertRaisesRegex(Exception, "异常缩减"):
                    await manager._download_with_fallback(
                        [url],
                        manager.geosite_file,
                        "geosite",
                        manager.geosite_meta,
                    )

            self.assertEqual(manager.geosite_file.read_bytes(), old_payload)
            self.assertFalse(
                manager.geosite_file.with_suffix(".txt.tmp").exists()
            )

    async def test_geosite_validation_rejects_large_html_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            html_path = Path(temp_dir) / "direct-list.txt"
            html_path.write_text("\n".join(["<div>error</div>"] * 150), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "非域名格式"):
                DataManager._validate_download(html_path, "geosite")


if __name__ == "__main__":
    unittest.main()
