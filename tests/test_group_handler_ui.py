import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.handlers.group_handler import GroupHandler


class TestGroupHandlerText(unittest.TestCase):
    def setUp(self):
        self.handler = GroupHandler.__new__(GroupHandler)
        self.handler.handler_manager = SimpleNamespace(
            escape_markdown=lambda value: str(value)
        )

    def test_missing_domain_page_is_compact_and_actionable(self):
        text = self.handler._build_missing_domain_text()

        self.assertIn("没有识别到域名", text)
        self.assertIn("重新 @机器人", text)
        self.assertIn("`example.com`", text)
        self.assertNotIn("•", text)
        self.assertIn("🧪 *输入示例*", text)
        self.assertIn("💬", text)
        self.assertLessEqual(len(text.splitlines()), 9)

    def test_cn_page_has_no_nested_bold_code(self):
        text = self.handler._build_cn_domain_text("example.cn")

        self.assertIn("*.cn 域名无需添加*", text)
        self.assertIn("`example.cn`", text)
        self.assertNotIn("*域名 `", text)


class TestGroupHandlerResultPages(unittest.IsolatedAsyncioTestCase):
    def _handler_for_result(self, result, *, is_admin=False):
        handler = GroupHandler.__new__(GroupHandler)
        handler.handler_manager = SimpleNamespace(
            MAX_ADDS_PER_HOUR=50,
            check_user_add_limit=MagicMock(return_value=(True, 50)),
            check_and_add_domain_auto=AsyncMock(return_value=result),
            escape_markdown=lambda value: str(value).replace("*", "\\*"),
            is_admin=MagicMock(return_value=is_admin),
            get_admin_force_add_callback=MagicMock(return_value="admin_force|token"),
            _format_telegram_identity=lambda user: "@alice",
        )
        return handler

    async def test_added_page_uses_commit_button_and_short_lines(self):
        handler = self._handler_for_result(
            {
                "action": "added",
                "target_domain": "example.com",
                "rate_limit_remaining": 49,
                "commit_sha": "abcdef123456",
                "commit_url": "https://github.com/example/repo/commit/abcdef123456",
            }
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=processing))

        await handler._process_domain_request(
            message, "example.com", "alice", 42
        )

        initial_text = message.reply_text.await_args.args[0]
        self.assertIn("将自动写入公开 GitHub", initial_text)
        self.assertIn("⚠️ 群聊流程不会再次要求确认", initial_text)
        result_call = processing.edit_text.await_args
        self.assertIn("直连规则已添加", result_call.args[0])
        self.assertIn("🧾 *已写入规则*", result_call.args[0])
        self.assertNotIn("https://", result_call.args[0])
        button = result_call.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertIn("abcdef12", button.text)
        self.assertEqual(
            button.url,
            "https://github.com/example/repo/commit/abcdef123456",
        )

    async def test_rejected_page_uses_policy_status_and_admin_action(self):
        handler = self._handler_for_result(
            {
                "action": "rejected",
                "message": "IP 和 NS 均不符合条件",
            },
            is_admin=True,
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=processing))

        await handler._process_domain_request(
            message, "example.com", "alice", 42
        )

        result_call = processing.edit_text.await_args
        self.assertTrue(result_call.args[0].startswith("⛔"))
        button = result_call.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "admin_force|token")

    async def test_error_detail_is_single_line_and_bounded(self):
        handler = self._handler_for_result(
            {
                "action": "error",
                "message": f"bad\n{'x' * 300}*",
            }
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=processing))

        await handler._process_domain_request(
            message, "example.com", "alice", 42
        )

        text = processing.edit_text.await_args.args[0]
        self.assertTrue(text.startswith("❌"))
        self.assertNotIn("bad\n", text)
        self.assertIn("请稍后再次 @机器人", text)
        self.assertIn("🛡️ 本次操作未修改任何规则", text)
        self.assertLess(len(text), 320)

    async def test_uncertain_write_result_warns_before_any_retry(self):
        handler = self._handler_for_result(
            {
                "action": "error",
                "submission_uncertain": True,
                "message": "提交结果暂时无法确认，请先查询规则，避免重复提交",
            }
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=processing))

        await handler._process_domain_request(
            message, "example.com", "alice", 42
        )

        text = processing.edit_text.await_args.args[0]
        self.assertTrue(text.startswith("⚠️ *提交结果暂时无法确认*"))
        self.assertEqual(text.count("提交结果暂时无法确认"), 1)
        self.assertIn("避免重复提交", text)
        self.assertNotIn("请稍后再次", text)

    async def test_added_result_render_failure_warns_against_duplicate_submit(self):
        handler = self._handler_for_result(
            {
                "action": "added",
                "target_domain": "example.com",
                "rate_limit_remaining": 49,
            }
        )
        processing = SimpleNamespace(
            edit_text=AsyncMock(side_effect=RuntimeError("telegram unavailable"))
        )
        message = SimpleNamespace(reply_text=AsyncMock(return_value=processing))

        await handler._process_domain_request(
            message, "example.com", "alice", 42
        )

        recovery_text = message.reply_text.await_args_list[-1].args[0]
        self.assertIn("规则已添加", recovery_text)
        self.assertIn("避免重复提交", recovery_text)
        self.assertNotIn("处理失败", recovery_text)


if __name__ == "__main__":
    unittest.main()
