import asyncio
import json
import os
import sys
import tempfile
import unittest
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
                changed = await manager._download_with_fallback(
                    ["https://example.test/geoip.mmdb"],
                    manager.geoip_file,
                    "geoip",
                    manager.geoip_meta,
                )

            self.assertTrue(changed)
            self.assertEqual(fake_session.calls[0]["headers"], {})
            self.assertTrue(manager.geoip_file.exists())


if __name__ == "__main__":
    unittest.main()
