import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.deferred_domain_service import DeferredDomainService
from src.handlers.handler_manager import HandlerManager


class TestDeferredDomains(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        return SimpleNamespace(
            config=SimpleNamespace(RULE_BOT_CLIENT_PRIVATE_API_RATE_LIMIT_PER_HOUR=50),
            check_and_add_domain_auto=AsyncMock(return_value={"action": "error", "error_code": "temporary_dns"}),
        )

    async def test_durable_deduplication_capacity_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager()
            service = DeferredDomainService(Path(tmp)/"queue.sqlite3", manager)
            service.MAX_PENDING = 1
            self.assertTrue(await service.enqueue("example.com", "rule_bot_client_private"))
            original = service._next()
            self.assertTrue(await service.enqueue("example.com", "rule_bot_client_private"))
            self.assertEqual(service._next(), original)
            self.assertFalse(await service.enqueue("other.example", "rule_bot_client_private"))
            restored = DeferredDomainService(service.path, manager)
            self.assertTrue(await restored.contains("example.com", "rule_bot_client_private"))
            await restored.process(restored._next())
            row = restored._next()
            self.assertEqual(row["attempts"], 1)
            self.assertGreater(row["next_attempt"], original["next_attempt"])
            manager.check_and_add_domain_auto.return_value = {"action": "added"}
            await restored.process(row)
            self.assertIsNone(restored._next())
            manager.check_and_add_domain_auto.assert_awaited_with(
                "example.com", "Rule-Bot Client", user_id=("deferred", "rule_bot_client_private"),
                source="rule_bot_client_private", max_adds=50,
            )

    async def test_worker_retries_due_record_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager()
            manager.check_and_add_domain_auto.return_value = {"action": "exists"}
            service = DeferredDomainService(Path(tmp)/"queue.sqlite3", manager)
            service.INITIAL_DELAY = 0
            await service.enqueue("example.com", "rule_bot_client_private")
            service.start()
            try:
                async with asyncio.timeout(2):
                    while service._next() is not None:
                        await asyncio.sleep(.01)
            finally:
                await service.stop()
            self.assertIsNone(service._task)

    async def test_old_client_gets_terminal_status_only_after_durable_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = HandlerManager.__new__(HandlerManager)
            manager.check_and_add_domain_auto = self.manager().check_and_add_domain_auto
            manager.deferred_domains = DeferredDomainService(Path(tmp)/"queue.sqlite3", manager)
            result = await manager.submit_rule_bot_client_domain(
                "www.example.com", source="rule_bot_client_private", rate_key=("private",0), max_adds=50,
            )
            self.assertEqual(result, {"status":"rejected_policy", "domain":"example.com", "deferred":True})
            self.assertEqual(manager.deferred_domains._next()["domain"], "example.com")
            manager.check_and_add_domain_auto.reset_mock()
            self.assertEqual(await manager.submit_rule_bot_client_domain(
                "example.com", source="rule_bot_client_private", rate_key=("private",0), max_adds=50,
            ), result)
            manager.check_and_add_domain_auto.assert_not_awaited()
            manager.deferred_domains.MAX_PENDING = 1
            full = await manager.submit_rule_bot_client_domain(
                "other.net", source="rule_bot_client_private", rate_key=("private",0), max_adds=50,
            )
            self.assertEqual(full["status"], "temporary_error")

    async def test_github_failure_is_not_acknowledged_or_enqueued(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.deferred_domains = SimpleNamespace(enqueue=AsyncMock(), contains=AsyncMock(return_value=False))
        manager.check_and_add_domain_auto = AsyncMock(return_value={"action":"error"})
        result = await manager.submit_rule_bot_client_domain(
            "example.com",source="rule_bot_client_private",rate_key=("private",0),max_adds=50,
        )
        self.assertEqual(result["status"], "temporary_error")
        manager.deferred_domains.enqueue.assert_not_awaited()

    async def test_ordinary_submissions_do_not_open_the_queue_database(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            service = DeferredDomainService(Path(tmp)/"queue.sqlite3", self.manager())
            with patch.object(service, "_connect", wraps=service._connect) as connect:
                results = await asyncio.gather(*(service.contains(f"site-{i}.com", "rule_bot_client_private") for i in range(1000)))
                self.assertFalse(any(results))
                connect.assert_not_called()
