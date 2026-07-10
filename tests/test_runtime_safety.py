import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import healthcheck
from src.handlers.group_handler import GroupHandler
from src.handlers.handler_manager import HandlerManager
from src.update_processor import PerUserUpdateProcessor


class TestRuntimeSafety(unittest.IsolatedAsyncioTestCase):
    async def test_update_processor_serializes_one_user_only(self):
        processor = PerUserUpdateProcessor(2)
        events = []
        user1 = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        user2 = SimpleNamespace(effective_user=SimpleNamespace(id=2))

        async def first():
            events.append("first-start")
            await asyncio.sleep(0.03)
            events.append("first-end")

        async def second():
            events.append("second")

        async def other_user():
            events.append("other-user")

        async with processor:
            task1 = asyncio.create_task(processor.process_update(user1, first()))
            await asyncio.sleep(0)
            task2 = asyncio.create_task(processor.process_update(user1, second()))
            task3 = asyncio.create_task(processor.process_update(user2, other_user()))
            await asyncio.gather(task1, task2, task3)

        self.assertLess(events.index("other-user"), events.index("first-end"))
        self.assertLess(events.index("first-end"), events.index("second"))

    async def test_group_mention_uses_utf16_aware_parser(self):
        handler = GroupHandler.__new__(GroupHandler)
        entity = SimpleNamespace(type="mention")
        message = MagicMock()
        message.text = "😊 @rulebot example.com"
        message.entities = [entity]
        message.parse_entity.return_value = "@rulebot"

        self.assertTrue(handler.is_bot_mentioned(message, "rulebot"))
        message.parse_entity.assert_called_once_with(entity)

    async def test_pending_action_callback_is_short_and_single_use(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager._pending_actions = {}
        manager._last_state_cleanup = time.monotonic()
        manager.user_states = {}
        manager.STATE_TTL = 1800
        manager.ACTION_TTL = 900

        domain = f"{'a' * 63}.com"
        callback = manager.get_admin_force_add_callback(123, domain)
        self.assertLessEqual(len(callback.encode("utf-8")), 64)
        token = callback.split("|", 1)[1]
        action = manager.get_pending_action(123, token, "admin_force_add", consume=True)
        self.assertEqual(action["domain"], domain)
        self.assertIsNone(manager.get_pending_action(123, token, "admin_force_add"))

    async def test_healthcheck_uses_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health"
            path.touch()
            with patch.object(healthcheck, "HEALTH_PATH", path):
                self.assertTrue(healthcheck.is_healthy())
                self.assertFalse(healthcheck.is_healthy(now=time.time() + 120))


if __name__ == "__main__":
    unittest.main()
