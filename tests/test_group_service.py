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

        self.assertIn("group\\_name\\[test\\]", message)
        self.assertIn("https://t.me/group\\_name\\(test\\)", message)


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
        self.assertIn("已成功提交", kwargs["text"])
        self.assertIn("DOMAIN-SUFFIX", kwargs["text"])
        self.assertIn("example.com", kwargs["text"])
        self.assertIn("abcdef12", kwargs["text"])
        self.assertIn("example/repo", kwargs["text"])
        self.assertIn("rules/direct.list", kwargs["text"])
        self.assertIn("添加人", kwargs["text"])
        self.assertIn("Alice\\_Name", kwargs["text"])
        self.assertTrue(kwargs["disable_notification"])

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
