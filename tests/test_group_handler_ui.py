import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.handlers.group_handler import GroupHandler


class TestGroupHandlerText(unittest.TestCase):
    def setUp(self):
        self.handler = GroupHandler.__new__(GroupHandler)
        self.handler.handler_manager = SimpleNamespace(
            escape_markdown=str
        )

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

    async def test_error_detail_is_single_line_and_complete(self):
        detail = f"bad\n{'x' * 300}*"
        handler = self._handler_for_result(
            {
                "action": "error",
                "message": detail,
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
        self.assertIn(f"bad {'x' * 300}\\*", text)
        self.assertNotIn("...", text)

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
