import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TelegramError

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.services.group_service import GroupService


class TestGroupService(unittest.TestCase):
    def test_join_group_message_escapes_markdown_sensitive_content(self):
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=True,
            REQUIRED_GROUP_NAME="group_name[test]",
            REQUIRED_GROUP_LINK="https://t.me/group_name(test)",
        )
        service = GroupService(config, bot=None)

        message = service.get_join_group_message()
        keyboard = service.get_join_group_keyboard()

        self.assertIn("👤 当前账号尚未通过群组成员验证", message)
        self.assertIn("👥 *所需群组*", message)
        self.assertIn("✅ 加入群组后", message)
        self.assertIn("group\\_name\\[test\\]", message)
        self.assertNotIn("https://t.me", message)
        self.assertEqual(
            keyboard.inline_keyboard[0][0].url,
            "https://t.me/group_name(test)",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            GroupService.MEMBERSHIP_RETRY_CALLBACK,
        )


class TestGroupMembership(unittest.IsolatedAsyncioTestCase):
    async def test_force_refresh_bypasses_cached_membership(self):
        bot = AsyncMock()
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=True,
            ANNOUNCEMENT_GROUP_ID=None,
            REQUIRED_GROUP_ID=-1001234567890,
        )
        service = GroupService(config, bot)
        service._membership_cache.set(123, False)

        self.assertFalse(await service.check_user_in_group(123))
        self.assertTrue(await service.check_user_in_group(123, force_refresh=True))
        bot.get_chat_member.assert_awaited_once()


class TestGroupAnnouncements(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_announcement_does_not_call_telegram(self):
        bot = AsyncMock()
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=False,
            ANNOUNCEMENT_GROUP_ID=None,
        )
        service = GroupService(config, bot)

        result = await service.announce_rule_submission("example.com", "abc123")

        self.assertFalse(result)
        bot.send_message.assert_not_awaited()

    async def test_announcement_includes_submission_context(self):
        bot = AsyncMock()
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=False,
            ANNOUNCEMENT_GROUP_ID=-1001234567890,
        )
        service = GroupService(config, bot)

        result = await service.announce_rule_submission(
            "example.com",
            "abcdef1234567890",
            "https://github.com/example/repo/commit/abcdef1234567890",
            "example/repo",
            "rules/direct.list",
            "Alice_Name",
        )

        self.assertTrue(result)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -1001234567890)
        self.assertIn("直连规则已更新", kwargs["text"])
        self.assertIn("🧾 *已写入规则*", kwargs["text"])
        self.assertIn("👤 提交者", kwargs["text"])
        self.assertIn("DOMAIN-SUFFIX", kwargs["text"])
        self.assertIn("example.com", kwargs["text"])
        self.assertNotIn("example/repo", kwargs["text"])
        self.assertNotIn("rules/direct.list", kwargs["text"])
        self.assertIn("提交者", kwargs["text"])
        self.assertIn("Alice\\_Name", kwargs["text"])
        commit_button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertIn("abcdef12", commit_button.text)
        self.assertEqual(
            commit_button.url,
            "https://github.com/example/repo/commit/abcdef1234567890",
        )
        self.assertTrue(kwargs["disable_notification"])

    async def test_announcement_keeps_dynamic_values_on_safe_lines(self):
        bot = AsyncMock()
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=False,
            ANNOUNCEMENT_GROUP_ID=-1001234567890,
        )
        service = GroupService(config, bot)

        await service.announce_rule_submission(
            "example.com`\n*injected*",
            repo_path="owner/repo`\nnext",
            rule_path="rules/direct.list`\nnext",
            user_name="Alice_Name\n*admin*",
        )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("\n*injected*", text)
        self.assertNotIn("\nnext", text)
        self.assertNotIn("\n*admin*", text)
        self.assertIn("\\`", text)

    async def test_telegram_failure_is_isolated(self):
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramError("forbidden")
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=False,
            ANNOUNCEMENT_GROUP_ID=-1001234567890,
        )
        service = GroupService(config, bot)

        result = await service.announce_rule_submission("example.com")

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
