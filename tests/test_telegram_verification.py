import json
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Bot
from telegram.request import BaseRequest

from src.handlers.group_handler import GroupHandler
from src.handlers.handler_manager import HandlerManager
from src.services.group_service import GroupService


def config():
    return SimpleNamespace(
        GROUP_CHECK_ENABLED=True,
        REQUIRED_GROUP_ID=-100123456,
        REQUIRED_GROUP_NAME="Custom_OpenClash_Rules | 交流群",
        REQUIRED_GROUP_LINK="https://t.me/example_group",
        GITHUB_REPO="Aethersailor/Custom_OpenClash_Rules",
        RULE_BOT_CLIENT_COMMUNITY_API_ENABLED=True,
        ALLOWED_GROUP_IDS=[],
    )


def manager(service=None):
    instance = HandlerManager.__new__(HandlerManager)
    instance.config = config()
    instance.group_service = service
    instance.user_states = {}
    instance._pending_actions = {}
    instance._last_state_cleanup = 0.0
    instance.MAX_USER_STATES = 4096
    instance.STATE_TTL = 1800
    return instance


class TelegramRequest(BaseRequest):
    """Serve Bot API responses through the real PTB request and type parser."""

    def __init__(self):
        self.bot_status = "administrator"
        self.user_status = "creator"
        self.is_member = True
        self.failure = False
        self.unchanged = False
        self.calls = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        elif endpoint == "getChatMember":
            if self.failure:
                return 400, json.dumps({"ok": False, "description": "Bad Request: chat not found"}).encode()
            uid = params["user_id"]
            status = self.bot_status if uid == 1 else self.user_status
            result = {
                "user": {"id": uid, "is_bot": uid == 1, "first_name": "Test"},
                "status": status,
                "until_date": 0,
                "is_member": self.is_member,
            }
            for field in (
                "is_anonymous", "can_be_edited", "can_manage_chat", "can_delete_messages",
                "can_manage_video_chats", "can_restrict_members", "can_promote_members",
                "can_change_info", "can_invite_users", "can_post_stories", "can_edit_stories",
                "can_delete_stories", "can_send_messages", "can_send_audios", "can_send_documents",
                "can_send_photos", "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
                "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
                "can_pin_messages", "can_manage_topics", "can_edit_tag", "can_react_to_messages",
            ):
                result[field] = False
        elif endpoint == "editMessageText" and self.unchanged:
            return 400, json.dumps({"ok": False, "description": "Bad Request: message is not modified"}).encode()
        elif endpoint == "answerCallbackQuery":
            result = True
        else:
            result = {
                "message_id": 10, "date": 1,
                "chat": {"id": 2, "type": "private", "first_name": "Test"},
                "text": params.get("text", ""),
            }
        return 200, json.dumps({"ok": True, "result": result}).encode()


class MenuHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.links = []
        self.tags = []
        self.stack = []
        self.feed(text)
        self.close()
        if self.stack:
            raise AssertionError(f"Unclosed tags: {self.stack}")

    def handle_starttag(self, tag, attrs):
        if tag not in ("b", "code", "a"):
            raise AssertionError(f"Unexpected start tag: {tag}")
        self.tags.append(tag)
        self.stack.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise AssertionError(f"Unexpected end tag: {tag}")
        self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


class FormattingTests(unittest.TestCase):
    def test_menu_preserves_names_and_repository_link(self):
        for name in ("csailor", "a_b*[x]` (test)!", r"C:\name", '<b>&"用户🙂'):
            with self.subTest(name=name):
                rendered = MenuHTML(manager()._build_main_menu_text(name))
                text = "".join(rendered.text)
                self.assertIn(f"欢迎使用 Rule-Bot，{name}！", text)
                self.assertIn("Aethersailor/Custom_OpenClash_Rules", text)
                self.assertNotIn(r"\_", text)
                self.assertEqual(rendered.links, ["https://github.com/Aethersailor/Custom_OpenClash_Rules"])
                self.assertEqual(rendered.tags.count("b"), 4)

    def test_legacy_plain_text_does_not_escape_ordinary_punctuation(self):
        text = r"C:\rules\file (中国) | A+B-C! #1 ~x {test}=ok"
        self.assertEqual(manager().escape_markdown(text), text)
        self.assertEqual(GroupService._escape_markdown(text), text)
        group = GroupHandler(config(), None, manager())
        self.assertEqual(group._escape_detail(text), text)

    def test_legacy_markup_characters_are_still_escaped(self):
        for text, expected in (("a_b", r"a\_b"), ("*name*", r"\*name\*"), ("[x]", r"\[x]"), ("`x`", r"\`x\`")):
            with self.subTest(text=text):
                self.assertEqual(manager().escape_markdown(text), expected)
                self.assertEqual(GroupService._escape_markdown(text), expected)

    def test_group_prompt_does_not_add_backslash_before_pipe(self):
        prompt = GroupService(config(), None).get_join_group_message()
        self.assertIn(r"Custom\_OpenClash\_Rules | 交流群", prompt)
        self.assertNotIn(r"\|", prompt)


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.request = TelegramRequest()
        self.bot = Bot("123456:TEST", request=self.request, get_updates_request=self.request)
        await self.bot.initialize()
        self.service = GroupService(config(), self.bot)
        self.request.calls.clear()

    async def asyncTearDown(self):
        await self.bot.shutdown()

    async def test_all_telegram_membership_states(self):
        for status, is_member, expected in (
            ("creator", True, True), ("administrator", True, True), ("member", True, True),
            ("restricted", True, True), ("restricted", False, False),
            ("left", False, False), ("kicked", False, False),
        ):
            with self.subTest(status=status, is_member=is_member):
                self.request.user_status = status
                self.request.is_member = is_member
                self.assertIs(await self.service.check_user_in_group(2, force_refresh=True), expected)

    async def test_non_admin_bot_does_not_report_user_left(self):
        self.request.is_member = False
        for bot_status in ("member", "left", "kicked"):
            for user_status in ("left", "kicked", "restricted"):
                with self.subTest(bot_status=bot_status, user_status=user_status):
                    self.request.bot_status = bot_status
                    self.request.user_status = user_status
                    self.request.calls.clear()
                    self.assertIsNone(await self.service.check_user_in_group(2, force_refresh=True))
                    self.assertEqual([p["user_id"] for e, p in self.request.calls], [2, 1])
                    self.assertIsNone(self.service._membership_cache.get(2))

    async def test_non_admin_bot_can_verify_existing_members(self):
        self.request.bot_status = "member"
        for status in ("creator", "administrator", "member", "restricted"):
            with self.subTest(user_status=status):
                self.request.user_status = status
                self.request.calls.clear()
                self.assertTrue(await self.service.check_user_in_group(2, force_refresh=True))
                self.assertEqual([p["user_id"] for e, p in self.request.calls], [2])

    async def test_promotion_is_seen_on_next_check(self):
        self.request.bot_status = "member"
        self.request.user_status = "left"
        self.assertIsNone(await self.service.check_user_in_group(2))
        self.request.bot_status = "administrator"
        self.request.user_status = "creator"
        self.assertTrue(await self.service.check_user_in_group(2))

    async def test_positive_cache_and_forced_refresh(self):
        self.assertTrue(await self.service.check_user_in_group(2))
        self.request.calls.clear()
        self.request.user_status = "left"
        self.assertTrue(await self.service.check_user_in_group(2))
        self.assertEqual(self.request.calls, [])
        self.assertFalse(await self.service.check_user_in_group(2, force_refresh=True))
        self.assertIsNone(self.service._membership_cache.get(2))

    async def test_negative_result_does_not_delay_new_members(self):
        self.request.user_status = "left"
        self.assertFalse(await self.service.check_user_in_group(2))
        self.request.user_status = "member"
        self.assertTrue(await self.service.check_user_in_group(2))

    async def test_api_failure_cannot_reuse_forced_refresh_cache(self):
        self.assertTrue(await self.service.check_user_in_group(2))
        self.request.failure = True
        self.assertIsNone(await self.service.check_user_in_group(2, force_refresh=True))
        self.assertIsNone(await self.service.check_user_in_group(2))

    async def test_disabled_verification_does_not_query_telegram(self):
        self.service._group_check_enabled = False
        self.assertTrue(await self.service.check_user_in_group(2))
        self.assertEqual(self.request.calls, [])

    async def test_all_menu_entrypoints_send_html(self):
        m = manager()
        m.group_service = None
        user = SimpleNamespace(id=2, first_name="test_user", username="test_user")
        message = SimpleNamespace(from_user=user, reply_text=AsyncMock())
        query = SimpleNamespace(from_user=user, edit_message_text=AsyncMock())
        await m.start_command(SimpleNamespace(effective_user=user, message=message), None)
        await m._show_main_menu(query)
        await m._show_main_menu_message(message)
        for args in message.reply_text.call_args_list + query.edit_message_text.call_args_list:
            self.assertEqual(args.kwargs["parse_mode"], "HTML")
            self.assertIn("test_user", "".join(MenuHTML(args.args[0]).text))

    async def test_unreliable_membership_shows_verification_error(self):
        self.request.bot_status = "member"
        self.request.user_status = "left"
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=2), callback_query=None, message=message)
        self.assertFalse(await manager(self.service).check_group_membership(update))
        text = message.reply_text.call_args.args[0]
        self.assertIn("群管理员权限", text)
        self.assertNotIn("加入群组后继续", text)

    async def test_confirmed_non_member_gets_join_prompt(self):
        self.request.user_status = "left"
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=2), callback_query=None, message=message)
        self.assertFalse(await manager(self.service).check_group_membership(update))
        self.assertIn("加入群组后继续", message.reply_text.call_args.args[0])

    async def test_group_announcement_preserves_punctuation_and_backslashes(self):
        cfg = config()
        cfg.ANNOUNCEMENT_GROUP_ID = cfg.REQUIRED_GROUP_ID
        service = GroupService(cfg, self.bot)
        self.assertTrue(await service.announce_rule_submission("example.com", user_name=r"C:\name (test)! | 用户"))
        messages = [p for endpoint, p in self.request.calls if endpoint == "sendMessage"]
        self.assertIn(r"C:\name (test)! | 用户", messages[0]["text"])
        self.assertEqual(messages[0]["parse_mode"], "Markdown")

    async def test_token_issue_does_not_issue_when_verification_is_unavailable(self):
        self.request.bot_status = "member"
        self.request.user_status = "left"
        m = manager(self.service)
        m.rule_bot_client_token_service = SimpleNamespace(issue=AsyncMock())
        query = SimpleNamespace(edit_message_text=AsyncMock())
        await m._perform_rule_bot_client_issue(query, 2)
        m.rule_bot_client_token_service.issue.assert_not_awaited()
        self.assertIn("群管理员权限", query.edit_message_text.call_args.args[0])

    async def test_repeated_retry_keeps_existing_prompt(self):
        from telegram import Update

        self.request.bot_status = "member"
        self.request.user_status = "left"
        self.request.unchanged = True
        update = Update.de_json({
            "update_id": 1,
            "callback_query": {
                "id": "retry", "chat_instance": "test", "data": "membership_retry",
                "from": {"id": 2, "is_bot": False, "first_name": "Test"},
                "message": {"message_id": 10, "date": 1, "chat": {"id": 2, "type": "private"}},
            },
        }, self.bot)
        await manager(self.service).handle_callback(update, None)
        edits = [p for endpoint, p in self.request.calls if endpoint == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertIn("暂时无法验证", edits[0]["text"])


if __name__ == "__main__":
    unittest.main()
