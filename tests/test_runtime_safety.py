import asyncio
import ast
import tempfile
import time
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src import healthcheck
from src.handlers.group_handler import GroupHandler
from src.handlers.handler_manager import HandlerManager
from src.update_processor import PerUserUpdateProcessor


class TestRuntimeSafety(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_rate_limited_manager(max_adds=50, add_result=None):
        manager = HandlerManager.__new__(HandlerManager)
        manager.MAX_ADDS_PER_HOUR = max_adds
        manager.user_add_history = defaultdict(list)
        manager._last_history_cleanup = 0
        manager.github_service = SimpleNamespace(
            add_domain_to_rules=AsyncMock(
                return_value=add_result or {"success": True}
            )
        )
        return manager

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

    async def test_update_processor_bounds_idle_key_locks(self):
        processor = PerUserUpdateProcessor(2, max_key_locks=64)

        async with processor:
            for user_id in range(200):
                update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id))

                async def complete():
                    return None

                await processor.process_update(update, complete())

            self.assertLessEqual(len(processor._locks), 64)

    async def test_handler_manager_bounds_user_states(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.user_states = {}
        manager._pending_actions = {}
        manager._last_state_cleanup = time.monotonic()
        manager.STATE_TTL = 1800
        manager.ACTION_TTL = 900
        manager.MAX_USER_STATES = 4

        for user_id in range(10):
            manager.get_user_state(user_id)

        self.assertEqual(len(manager.user_states), 4)
        self.assertIn(9, manager.user_states)

    async def test_add_gate_blocks_at_limit_without_github_write(self):
        manager = self._make_rate_limited_manager(max_adds=2)
        manager.user_add_history[123] = [time.time(), time.time()]

        result = await manager._add_domain_with_limit(
            123,
            "example.com",
            "Alice",
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["rate_limit_remaining"], 0)
        self.assertEqual(len(manager.user_add_history[123]), 2)
        manager.github_service.add_domain_to_rules.assert_not_awaited()

    async def test_add_gate_counts_success_and_rolls_back_failures(self):
        manager = self._make_rate_limited_manager()
        manager.github_service.add_domain_to_rules.side_effect = [
            {"success": True, "commit_sha": "abc"},
            {"success": False, "error": "write failed"},
            RuntimeError("write crashed"),
        ]

        success = await manager._add_domain_with_limit(
            123,
            "one.example",
            "Alice",
            "admin add",
            force_add=True,
        )
        failure = await manager._add_domain_with_limit(
            123,
            "two.example",
            "Alice",
        )
        with self.assertRaisesRegex(RuntimeError, "write crashed"):
            await manager._add_domain_with_limit(
                123,
                "three.example",
                "Alice",
            )

        self.assertTrue(success["success"])
        self.assertEqual(success["rate_limit_remaining"], 49)
        self.assertFalse(failure["success"])
        self.assertEqual(len(manager.user_add_history[123]), 1)
        self.assertEqual(
            manager.github_service.add_domain_to_rules.await_args_list[0].args,
            ("one.example", "Alice", "admin add"),
        )
        self.assertTrue(
            manager.github_service.add_domain_to_rules.await_args_list[0].kwargs[
                "force_add"
            ]
        )

    async def test_add_gate_keeps_uncertain_write_reserved(self):
        manager = self._make_rate_limited_manager(
            max_adds=1,
            add_result={
                "success": False,
                "submission_uncertain": True,
                "error": "response lost after request",
            },
        )

        uncertain = await manager._add_domain_with_limit(
            123,
            "one.example",
            "Alice",
        )
        retry = await manager._add_domain_with_limit(
            123,
            "two.example",
            "Alice",
        )

        self.assertTrue(uncertain["submission_uncertain"])
        self.assertEqual(len(manager.user_add_history[123]), 1)
        self.assertTrue(retry["rate_limited"])
        manager.github_service.add_domain_to_rules.assert_awaited_once()

    async def test_add_gate_preserves_marked_cancelled_write(self):
        manager = self._make_rate_limited_manager(max_adds=1)
        cancelled = asyncio.CancelledError()
        cancelled.submission_uncertain = True
        manager.github_service.add_domain_to_rules.side_effect = cancelled

        with self.assertRaises(asyncio.CancelledError):
            await manager._add_domain_with_limit(
                123,
                "example.com",
                "Alice",
            )

        self.assertEqual(len(manager.user_add_history[123]), 1)

    async def test_add_gate_rolls_back_cancelled_write(self):
        manager = self._make_rate_limited_manager()
        manager.github_service.add_domain_to_rules.side_effect = (
            asyncio.CancelledError
        )

        with self.assertRaises(asyncio.CancelledError):
            await manager._add_domain_with_limit(
                123,
                "example.com",
                "Alice",
            )

        self.assertNotIn(123, manager.user_add_history)

    async def test_add_gate_reserves_slot_before_concurrent_write(self):
        manager = self._make_rate_limited_manager(max_adds=1)
        write_started = asyncio.Event()
        allow_write = asyncio.Event()

        async def slow_write(*args, **kwargs):
            write_started.set()
            await allow_write.wait()
            return {"success": True}

        manager.github_service.add_domain_to_rules.side_effect = slow_write
        first_task = asyncio.create_task(
            manager._add_domain_with_limit(123, "one.example", "Alice")
        )
        await write_started.wait()

        second = await manager._add_domain_with_limit(
            123,
            "two.example",
            "Alice",
        )
        allow_write.set()
        first = await first_task

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertTrue(second["rate_limited"])
        self.assertEqual(
            manager.github_service.add_domain_to_rules.await_count,
            1,
        )
        self.assertEqual(len(manager.user_add_history[123]), 1)

    def test_source_has_one_github_add_gateway(self):
        direct_callers = []

        class GithubAddVisitor(ast.NodeVisitor):
            def __init__(self, source_path):
                self.source_path = source_path
                self.current_function = None

            def visit_FunctionDef(self, node):
                previous = self.current_function
                self.current_function = node.name
                self.generic_visit(node)
                self.current_function = previous

            def visit_AsyncFunctionDef(self, node):
                previous = self.current_function
                self.current_function = node.name
                self.generic_visit(node)
                self.current_function = previous

            def visit_Call(self, node):
                function = node.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "add_domain_to_rules"
                ):
                    direct_callers.append(
                        (self.source_path, self.current_function)
                    )
                self.generic_visit(node)

        source_root = Path(__file__).parents[1] / "src"
        for source_file in source_root.rglob("*.py"):
            source_path = source_file.relative_to(source_root).as_posix()
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            GithubAddVisitor(source_path).visit(tree)

        self.assertEqual(
            direct_callers,
            [("handlers/handler_manager.py", "_add_domain_with_limit")],
        )

    async def test_announcement_only_runs_for_private_success(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.group_service = SimpleNamespace(
            announce_rule_submission=AsyncMock(return_value=True)
        )
        manager.config = SimpleNamespace(GITHUB_REPO="example/repo")
        result = {
            "success": True,
            "commit_sha": "abc",
            "commit_url": "https://example.test",
            "file_path": "rules/direct.list",
        }

        private_result = await manager._announce_private_addition(
            SimpleNamespace(type="private"), "example.com", result, "Alice"
        )
        group_result = await manager._announce_private_addition(
            SimpleNamespace(type="supergroup"), "other.com", result, "Bob"
        )

        self.assertTrue(private_result)
        self.assertFalse(group_result)
        manager.group_service.announce_rule_submission.assert_awaited_once_with(
            "example.com", "abc", "https://example.test", "example/repo", "rules/direct.list", "Alice"
        )

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
