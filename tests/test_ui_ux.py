import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot import RuleBot
from src.handlers.group_handler import GroupHandler
from src.handlers.handler_manager import HandlerManager


def _button_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


class TestVisibleCopy(unittest.TestCase):
    def _manager(self, matchscope_enabled=True):
        manager = HandlerManager.__new__(HandlerManager)
        manager.config = SimpleNamespace(
            GITHUB_REPO="Aethersailor/Custom_OpenClash_Rules",
            DIRECT_RULE_FILE="rule/Custom_Clash_Direct.list",
            PROXY_RULE_FILE="rule/Custom_Clash_Proxy.list",
            MATCHSCOPE_PUBLIC_API_ENABLED=matchscope_enabled,
            ANNOUNCEMENT_GROUP_ID=-100123 if matchscope_enabled else None,
        )
        manager.MAX_DETAIL_LINES = 6
        manager.MAX_DETAIL_LINE_LENGTH = 120
        return manager

    def test_main_menu_is_compact_and_hides_unavailable_features(self):
        manager = self._manager()

        text = manager._build_main_menu_text("Alice")
        keyboard = manager._build_main_menu_keyboard()
        labels = _button_labels(keyboard)

        self.assertLessEqual(len(text.strip().splitlines()), 8)
        self.assertNotIn("添加代理规则", text)
        self.assertNotIn("删除规则", text)
        self.assertNotIn("➕ 添加代理规则", labels)
        self.assertNotIn("➖ 删除规则", labels)
        self.assertLessEqual(max(len(row) for row in keyboard.inline_keyboard), 2)

    def test_help_lists_only_real_commands_and_visible_workflows(self):
        manager = self._manager()

        text = manager._build_help_text()

        for command in ("/query", "/add", "/id", "/help", "/skip"):
            self.assertIn(command, text)
        self.assertIn("群聊", text)
        self.assertIn("MatchScope", text)
        self.assertNotIn("暂不支持", text)
        self.assertLess(len(text), 4096)

    def test_prompts_use_accurate_registered_domain_term(self):
        manager = self._manager()

        query = manager._build_query_prompt("📊 *当前统计：* 可用\n\n")
        add = manager._build_add_prompt("📊 *当前统计：* 可用\n\n")

        self.assertIn("可注册域名（主域名）", query)
        self.assertIn("可注册域名（主域名）", add)
        self.assertNotIn("二级域名", query + add)
        self.assertLessEqual(len(query.strip().splitlines()), 16)
        self.assertLessEqual(len(add.strip().splitlines()), 16)

    def test_identity_does_not_fake_a_username(self):
        manager = self._manager()

        username = manager._format_telegram_identity(
            SimpleNamespace(id=1, username="alice", first_name="Alice")
        )
        display_name = manager._format_telegram_identity(
            SimpleNamespace(id=2, username=None, first_name="Alice Smith")
        )

        self.assertEqual(username, "@alice")
        self.assertEqual(display_name, "Alice Smith")
        self.assertNotIn("@", display_name)

    def test_add_review_discloses_exact_rule_and_public_scope(self):
        manager = self._manager()
        manager.domain_checker = SimpleNamespace(
            get_target_domain_to_add=MagicMock(return_value="example.com"),
            should_reject=MagicMock(return_value=False),
        )
        user = SimpleNamespace(id=2, username=None, first_name="Alice")
        check_result = {"recommendation": "建议添加", "details": []}

        text, target = manager._build_add_review_text(
            "sub.example.com", check_result, user
        )

        self.assertEqual(target, "example.com")
        self.assertIn("DOMAIN-SUFFIX,example.com", text)
        self.assertIn("公开 GitHub", text)
        self.assertIn("Alice", text)
        self.assertIn("群组播报", text)

    def test_command_menu_omits_dead_end_commands(self):
        commands = RuleBot._build_bot_commands()
        names = [command.command for command in commands]

        self.assertEqual(names, ["start", "query", "add", "help", "id", "skip"])
        self.assertNotIn("delete", names)


class TestVisibleFlows(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _stateful_manager():
        manager = HandlerManager.__new__(HandlerManager)
        manager.config = SimpleNamespace(
            GITHUB_REPO="Aethersailor/Custom_OpenClash_Rules",
            DIRECT_RULE_FILE="rule/Custom_Clash_Direct.list",
            PROXY_RULE_FILE="rule/Custom_Clash_Proxy.list",
            MATCHSCOPE_PUBLIC_API_ENABLED=True,
            MATCHSCOPE_PUBLIC_BASE_URL="https://example.test",
            MATCHSCOPE_PUBLIC_API_PATH="/community",
            ANNOUNCEMENT_GROUP_ID=-100123,
        )
        manager.user_states = {
            42: {"state": "waiting_add_domain", "data": {"domain": "old.example"}, "updated_at": time.monotonic()}
        }
        manager._pending_actions = {
            (42, "old"): {"action": "confirm_add", "data": {}, "created_at": time.monotonic()}
        }
        manager._last_state_cleanup = time.monotonic()
        manager.STATE_TTL = 1800
        manager.ACTION_TTL = 900
        manager.MAX_USER_STATES = 4096
        manager.MAX_DETAIL_LINES = 6
        manager.MAX_DETAIL_LINE_LENGTH = 120
        return manager

    async def test_return_to_main_menu_resets_state_and_stale_actions(self):
        manager = self._stateful_manager()
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=42, first_name="Alice", username="alice"),
            edit_message_text=AsyncMock(),
        )

        await manager._show_main_menu(query, 42)

        self.assertEqual(manager.user_states[42]["state"], "idle")
        self.assertEqual(manager.user_states[42]["data"], {})
        self.assertNotIn((42, "old"), manager._pending_actions)

    async def test_starting_a_new_flow_invalidates_old_confirmation_buttons(self):
        manager = self._stateful_manager()
        manager._build_stats_text = AsyncMock(return_value="")
        query = SimpleNamespace(edit_message_text=AsyncMock())

        await manager._start_domain_query(query, 42)

        self.assertEqual(manager.user_states[42]["state"], "waiting_query_domain")
        self.assertNotIn((42, "old"), manager._pending_actions)

    async def test_start_command_invalidates_old_confirmation_buttons(self):
        manager = self._stateful_manager()
        manager.check_group_membership = AsyncMock(return_value=True)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=42, first_name="Alice", username="alice"
            ),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

        await manager.start_command(update, None)

        self.assertEqual(manager.user_states[42]["state"], "idle")
        self.assertNotIn((42, "old"), manager._pending_actions)

    async def test_unknown_command_keeps_membership_gate(self):
        manager = self._stateful_manager()
        manager.check_group_membership = AsyncMock(return_value=False)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

        await manager.unknown_command(update, None)

        update.message.reply_text.assert_not_awaited()
        self.assertIn((42, "old"), manager._pending_actions)

    async def test_stats_failure_is_not_reported_as_zero_or_loading(self):
        manager = self._stateful_manager()
        manager.github_service = SimpleNamespace(
            get_file_stats=AsyncMock(return_value={"error": "unavailable"})
        )
        manager.data_manager = SimpleNamespace(geosite_domains={"one.example"})

        text = await manager._build_stats_text()

        self.assertIn("暂时无法获取", text)
        self.assertNotIn("直连规则数量：0", text)
        self.assertNotIn("加载中", text)

    async def test_query_github_failure_never_claims_no_china_signal(self):
        manager = self._stateful_manager()
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"error": "unavailable"})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(
                return_value={
                    "domain_ips": ["1.2.3.4"],
                    "second_level_ips": [],
                    "details": ["1.2.3.4 - 中国大陆"],
                    "domain_china_status": True,
                    "second_level_china_status": False,
                    "ns_china_status": False,
                    "recommendation": "检测到中国大陆 IP",
                }
            ),
            get_target_domain_to_add=MagicMock(return_value="example.com"),
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock(return_value=processing)),
            effective_user=SimpleNamespace(id=42, username="alice", first_name="Alice"),
        )

        await manager._handle_domain_query(update, "example.com", 42)

        text = processing.edit_text.await_args_list[-1].args[0]
        self.assertIn("检测到中国大陆", text)
        self.assertIn("GitHub", text)
        self.assertNotIn("IP 和 NS 均不在中国大陆", text)
        buttons = _button_labels(processing.edit_text.await_args_list[-1].kwargs["reply_markup"])
        self.assertNotIn("➕ 添加到直连规则", buttons)

    async def test_privacy_copy_is_conditional_and_names_third_party_clients(self):
        manager = self._stateful_manager()
        manager.matchscope_token_service = SimpleNamespace(
            has_current_consent=AsyncMock(return_value=False)
        )
        query = SimpleNamespace(edit_message_text=AsyncMock())

        await manager._show_matchscope_privacy(query, 42)

        text = query.edit_message_text.await_args.args[0]
        self.assertIn("官方 MatchScope", text)
        self.assertIn("第三方", text)
        self.assertIn("若入口经过", text)
        self.assertNotIn("公网连接会让 Cloudflare", text)
        self.assertIn("公开出现在 GitHub", text)
        self.assertEqual(manager.user_states[42]["state"], "idle")
        self.assertNotIn((42, "old"), manager._pending_actions)

    async def test_reissue_revoke_and_withdraw_require_confirmation(self):
        manager = self._stateful_manager()
        manager.matchscope_token_service = SimpleNamespace(
            has_current_consent=AsyncMock(return_value=True),
            status=AsyncMock(
                return_value={"enabled": True, "expires_at": int(time.time()) + 3600}
            ),
            issue=AsyncMock(),
            revoke=AsyncMock(),
            withdraw_consent=AsyncMock(),
        )
        manager.group_service = SimpleNamespace(
            check_user_in_group=AsyncMock(return_value=True)
        )
        query = SimpleNamespace(edit_message_text=AsyncMock())

        await manager._issue_matchscope_token(query, 42)
        issue_text = query.edit_message_text.await_args.args[0]
        self.assertIn("旧 Token", issue_text)
        manager.matchscope_token_service.issue.assert_not_awaited()

        await manager._revoke_matchscope_token(query, 42)
        revoke_text = query.edit_message_text.await_args.args[0]
        self.assertIn("确认吊销", revoke_text)
        manager.matchscope_token_service.revoke.assert_not_awaited()

        await manager._withdraw_matchscope_privacy(query, 42)
        withdraw_text = query.edit_message_text.await_args.args[0]
        self.assertIn("确认撤回", withdraw_text)
        manager.matchscope_token_service.withdraw_consent.assert_not_awaited()

    async def test_matchscope_reissue_confirmation_is_scoped_and_single_use(self):
        manager = self._stateful_manager()
        manager.matchscope_token_service = SimpleNamespace(
            has_current_consent=AsyncMock(return_value=True),
            status=AsyncMock(
                return_value={"enabled": True, "expires_at": int(time.time()) + 3600}
            ),
            issue=AsyncMock(
                return_value={"token": "new-token", "expires_at": int(time.time()) + 3600}
            ),
        )
        manager.group_service = SimpleNamespace(
            check_user_in_group=AsyncMock(return_value=True)
        )
        query = SimpleNamespace(
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

        await manager._issue_matchscope_token(query, 42)
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        callback_data = markup.inline_keyboard[0][0].callback_data

        await manager._confirm_matchscope_issue(query, 99, callback_data)
        manager.matchscope_token_service.issue.assert_not_awaited()

        await manager._confirm_matchscope_issue(query, 42, callback_data)
        manager.matchscope_token_service.issue.assert_awaited_once_with(42)

        await manager._confirm_matchscope_issue(query, 42, callback_data)
        self.assertEqual(manager.matchscope_token_service.issue.await_count, 1)

    async def test_invalid_description_preview_cannot_break_markdown(self):
        manager = self._stateful_manager()
        manager.MAX_DESCRIPTION_LENGTH = 20
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock())
        )

        await manager._handle_description_input(update, "`" * 21, 42)

        text = update.message.reply_text.await_args.args[0]
        self.assertNotIn("`" * 20, text)

    async def test_group_processing_message_discloses_automatic_public_write(self):
        handler = GroupHandler.__new__(GroupHandler)
        handler.handler_manager = SimpleNamespace(
            MAX_ADDS_PER_HOUR=50,
            check_user_add_limit=MagicMock(return_value=(True, 50)),
            check_and_add_domain_auto=AsyncMock(
                return_value={
                    "action": "exists",
                    "message": "已存在",
                }
            ),
            escape_markdown=lambda value: value,
            is_admin=MagicMock(return_value=False),
            _format_telegram_identity=lambda user: "@alice",
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=processing))

        await handler._process_domain_request(
            message, "example.com", "alice", 42
        )

        initial_text = message.reply_text.await_args.args[0]
        self.assertIn("将自动写入公开", initial_text)
        self.assertIn("公开 GitHub", initial_text)


if __name__ == "__main__":
    unittest.main()
