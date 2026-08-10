import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.handlers.handler_manager import HandlerManager


def _button_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


class TestVisibleSafety(unittest.TestCase):
    def _manager(self, rule_bot_client_enabled=True):
        manager = HandlerManager.__new__(HandlerManager)
        manager.config = SimpleNamespace(
            GITHUB_REPO="Aethersailor/Custom_OpenClash_Rules",
            DIRECT_RULE_FILE="rule/Custom_Clash_Direct.list",
            PROXY_RULE_FILE="rule/Custom_Clash_Proxy.list",
            RULE_BOT_CLIENT_COMMUNITY_API_ENABLED=rule_bot_client_enabled,
            ANNOUNCEMENT_GROUP_ID=-100123 if rule_bot_client_enabled else None,
        )
        manager.MAX_DETAIL_LINES = 4
        manager.MAX_DETAIL_LINE_LENGTH = 56
        manager.MAX_DESCRIPTION_LENGTH = 20
        return manager

    def test_uncertain_submission_page_never_claims_no_write(self):
        manager = self._manager()

        text = manager._build_submission_uncertain_text("example.com")

        self.assertIn("提交结果暂时无法确认", text)
        self.assertIn("避免重复提交", text)
        self.assertNotIn("没有修改任何规则", text)
        self.assertIn("🧾 *待核对规则*", text)

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
        self.assertIn("🧾 *拟提交规则*", text)
        self.assertIn("🔎 *原始输入*", text)
        self.assertIn("💡 *判断依据*", text)
        self.assertIn("🌍 *公开范围*", text)
        self.assertIn("公开 GitHub", text)
        self.assertIn("Alice", text)
        self.assertIn("群组公告", text)
        self.assertNotIn("结论：", text)
        self.assertNotIn("检查详情", text)


class TestVisibleFlows(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _stateful_manager():
        manager = HandlerManager.__new__(HandlerManager)
        manager.config = SimpleNamespace(
            GITHUB_REPO="Aethersailor/Custom_OpenClash_Rules",
            DIRECT_RULE_FILE="rule/Custom_Clash_Direct.list",
            PROXY_RULE_FILE="rule/Custom_Clash_Proxy.list",
            RULE_BOT_CLIENT_COMMUNITY_API_ENABLED=True,
            RULE_BOT_CLIENT_COMMUNITY_API_BASE_URL="https://example.test",
            RULE_BOT_CLIENT_COMMUNITY_API_PATH="/community",
            ANNOUNCEMENT_GROUP_ID=-100123,
            ADMIN_USER_IDS=set(),
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
        manager.MAX_DETAIL_LINES = 4
        manager.MAX_DETAIL_LINE_LENGTH = 56
        manager.MAX_DESCRIPTION_LENGTH = 20
        manager.MAX_ADDS_PER_HOUR = 50
        return manager

    @staticmethod
    def _query_candidate_manager():
        manager = TestVisibleFlows._stateful_manager()
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"exists": False})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(
                return_value={
                    "domain_ips": ["1.2.3.4"],
                    "second_level_ips": [],
                    "details": ["1.2.3.4 位于中国大陆"],
                    "domain_china_status": True,
                    "second_level_china_status": False,
                    "ns_china_status": False,
                    "recommendation": "检测到中国大陆 IP",
                }
            ),
            get_target_domain_to_add=MagicMock(return_value="example.com"),
            should_reject=MagicMock(return_value=False),
        )
        manager.check_group_membership = AsyncMock(return_value=True)
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
        self.assertTrue(
            query.edit_message_text.await_args.kwargs[
                "disable_web_page_preview"
            ]
        )

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

    async def test_new_query_invalidates_previous_add_callback(self):
        manager = self._query_candidate_manager()
        first_processing = SimpleNamespace(edit_text=AsyncMock())
        second_processing = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            reply_text=AsyncMock(
                side_effect=[first_processing, second_processing]
            )
        )
        query_update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
        )

        await manager._handle_domain_query(query_update, "first.example", 42)
        first_markup = first_processing.edit_text.await_args_list[-1].kwargs[
            "reply_markup"
        ]
        old_add_callback = next(
            button.callback_data
            for row in first_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("add_domain|")
        )

        await manager._handle_domain_query(query_update, "second.example", 42)

        callback = SimpleNamespace(
            data=old_add_callback,
            from_user=query_update.effective_user,
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        callback_update = SimpleNamespace(
            callback_query=callback,
            effective_user=query_update.effective_user,
            effective_chat=SimpleNamespace(type="private", id=42),
        )
        await manager.handle_callback(callback_update, None)

        self.assertIn("操作已过期", callback.edit_message_text.await_args.args[0])
        self.assertEqual(
            manager.domain_checker.check_domain_comprehensive.await_count, 2
        )

    async def test_consumed_query_add_cannot_restore_old_detail_page(self):
        manager = self._query_candidate_manager()
        processing = SimpleNamespace(edit_text=AsyncMock())
        user = SimpleNamespace(id=42, username="alice", first_name="Alice")
        query_update = SimpleNamespace(
            message=SimpleNamespace(
                reply_text=AsyncMock(return_value=processing)
            ),
            effective_user=user,
        )

        await manager._handle_domain_query(query_update, "example.com", 42)
        markup = processing.edit_text.await_args_list[-1].kwargs["reply_markup"]
        add_callback = next(
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("add_domain|")
        )
        detail_callback = next(
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("query_details|")
        )

        add_query = SimpleNamespace(
            data=add_callback,
            from_user=user,
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        add_update = SimpleNamespace(
            callback_query=add_query,
            effective_user=user,
            effective_chat=SimpleNamespace(type="private", id=42),
        )
        await manager.handle_callback(add_update, None)

        detail_query = SimpleNamespace(
            data=detail_callback,
            from_user=user,
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        detail_update = SimpleNamespace(
            callback_query=detail_query,
            effective_user=user,
            effective_chat=SimpleNamespace(type="private", id=42),
        )
        await manager.handle_callback(detail_update, None)

        restored_text = detail_query.edit_message_text.await_args.args[0]
        self.assertIn("查询详情已过期", restored_text)
        self.assertNotIn("技术详情", restored_text)

    async def test_reissue_revoke_and_withdraw_require_confirmation(self):
        manager = self._stateful_manager()
        manager.rule_bot_client_token_service = SimpleNamespace(
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

        await manager._issue_rule_bot_client_token(query, 42)
        issue_text = query.edit_message_text.await_args.args[0]
        self.assertIn("旧 Token", issue_text)
        manager.rule_bot_client_token_service.issue.assert_not_awaited()

        await manager._revoke_rule_bot_client_token(query, 42)
        revoke_text = query.edit_message_text.await_args.args[0]
        self.assertIn("吊销当前 Token", revoke_text)
        revoke_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("🚫 确认吊销", _button_labels(revoke_markup))
        manager.rule_bot_client_token_service.revoke.assert_not_awaited()

        await manager._withdraw_rule_bot_client_privacy(query, 42)
        withdraw_text = query.edit_message_text.await_args.args[0]
        self.assertIn("撤回隐私同意", withdraw_text)
        withdraw_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("🚫 确认撤回并吊销", _button_labels(withdraw_markup))
        manager.rule_bot_client_token_service.withdraw_consent.assert_not_awaited()

    async def test_membership_retry_callback_bypasses_stale_gate(self):
        manager = self._stateful_manager()
        manager.group_service = SimpleNamespace(
            is_group_check_enabled=MagicMock(return_value=True),
            check_user_in_group=AsyncMock(return_value=True),
        )
        manager._show_main_menu = AsyncMock()
        query = SimpleNamespace(
            data="membership_retry",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(type="private", id=42),
        )

        await manager.handle_callback(update, None)

        query.answer.assert_awaited_once()
        manager.group_service.check_user_in_group.assert_awaited_once_with(
            42, force_refresh=True
        )
        manager._show_main_menu.assert_awaited_once_with(query, 42)

    async def test_private_navigation_callback_is_rejected_in_group(self):
        manager = self._stateful_manager()
        manager.config.ALLOWED_GROUP_IDS = {-100123}
        manager.check_group_membership = AsyncMock(return_value=True)
        manager._show_main_menu = AsyncMock()
        original_state = dict(manager.user_states[42])
        query = SimpleNamespace(
            data="main_menu",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(type="supergroup", id=-100123),
        )

        await manager.handle_callback(update, None)

        query.answer.assert_awaited_once_with(
            "此按钮仅用于私聊；请私聊机器人继续操作。",
            show_alert=True,
        )
        manager.check_group_membership.assert_not_awaited()
        manager._show_main_menu.assert_not_awaited()
        self.assertEqual(manager.user_states[42], original_state)

    async def test_group_admin_button_intruders_do_not_edit_shared_message(self):
        manager = self._stateful_manager()
        manager.config.ALLOWED_GROUP_IDS = {-100123}
        manager.config.ADMIN_USER_IDS = {42, 44}
        token = manager.create_pending_action(
            42, "admin_force_add", domain="example.com"
        )

        for intruder_id in (43, 44):
            with self.subTest(intruder_id=intruder_id):
                query = SimpleNamespace(
                    data=f"admin_force_add|{token}",
                    answer=AsyncMock(),
                    edit_message_text=AsyncMock(),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="supergroup")
                    ),
                )
                update = SimpleNamespace(
                    callback_query=query,
                    effective_user=SimpleNamespace(id=intruder_id),
                    effective_chat=SimpleNamespace(
                        type="supergroup", id=-100123
                    ),
                )

                await manager.handle_callback(update, None)

                query.answer.assert_awaited_once()
                self.assertTrue(query.answer.await_args.kwargs["show_alert"])
                query.edit_message_text.assert_not_awaited()
                self.assertIsNotNone(
                    manager.get_pending_action(
                        42,
                        token,
                        "admin_force_add",
                        consume=False,
                    )
                )

    async def test_group_admin_force_add_does_not_change_private_state(self):
        manager = self._stateful_manager()
        manager.config.ADMIN_USER_IDS = {42}
        original_state = {
            "state": "waiting_description",
            "data": {"domain": "private.example"},
            "updated_at": manager.user_states[42]["updated_at"],
        }
        manager.user_states[42] = dict(original_state)
        manager.check_user_add_limit = MagicMock(return_value=(True, 50))
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"exists": False})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(return_value={}),
            get_target_domain_to_add=MagicMock(return_value="example.com"),
        )
        manager._add_domain_with_limit = AsyncMock(
            return_value={
                "success": True,
                "rate_limit_remaining": 49,
                "commit_url": "https://github.com/example/repo/commit/abc123",
                "commit_sha": "abc123",
            }
        )
        manager._announce_private_addition = AsyncMock()
        token = manager.create_pending_action(
            42, "admin_force_add", domain="example.com"
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="supergroup"),
                reply_text=AsyncMock(),
            ),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(
                side_effect=[
                    None,
                    None,
                    RuntimeError("result edit failed"),
                ]
            ),
        )

        await manager._handle_admin_force_add_callback(
            query, 42, f"admin_force_add|{token}"
        )
        confirmation = query.edit_message_text.await_args_list[-1]
        self.assertIn("立即公开写入 GitHub", confirmation.args[0])
        self.assertEqual(
            _button_labels(confirmation.kwargs["reply_markup"]),
            ["⚠️ 确认立即公开提交", "↩️ 取消"],
        )
        manager._add_domain_with_limit.assert_not_awaited()

        confirm_callback = confirmation.kwargs["reply_markup"].inline_keyboard[
            0
        ][0].callback_data
        await manager._handle_admin_force_add_callback(
            query, 42, confirm_callback
        )

        final = query.message.reply_text.await_args
        self.assertIsNone(final.kwargs["reply_markup"])
        self.assertIn("重新 @机器人", final.args[0])
        self.assertIn("继续处理", final.args[0])
        self.assertNotIn("请稍后重新", final.args[0])
        manager._add_domain_with_limit.assert_awaited_once()
        self.assertEqual(manager.user_states[42], original_state)

    async def test_rule_bot_client_access_hides_long_endpoint_in_copy_button(self):
        manager = self._stateful_manager()
        manager.rule_bot_client_token_service = SimpleNamespace(
            status=AsyncMock(
                return_value={"enabled": True, "expires_at": int(time.time()) + 3600}
            ),
            has_current_consent=AsyncMock(return_value=True),
        )
        query = SimpleNamespace(edit_message_text=AsyncMock())

        await manager._show_rule_bot_client_access(query, 42)

        text = query.edit_message_text.await_args.args[0]
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        endpoint = "https://example.test/community"
        self.assertNotIn(endpoint, text)
        copy_buttons = [
            button
            for row in markup.inline_keyboard
            for button in row
            if button.copy_text is not None
        ]
        self.assertEqual(len(copy_buttons), 1)
        self.assertEqual(copy_buttons[0].copy_text.text, endpoint)
        self.assertEqual(_button_labels(markup)[-1], "🏠 返回首页")

    async def test_write_exception_reports_uncertain_result_before_retry(self):
        manager = self._stateful_manager()
        manager.user_states[42] = {
            "state": "waiting_description",
            "data": {
                "domain": "example.com",
                "check_result": {"recommendation": "ok"},
            },
            "updated_at": time.monotonic(),
        }
        manager.domain_checker = SimpleNamespace(
            get_target_domain_to_add=MagicMock(return_value="example.com")
        )
        manager._add_domain_with_limit = AsyncMock(
            side_effect=RuntimeError("connection lost after request")
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=42, username="alice"),
            edit_message_text=AsyncMock(),
        )

        await manager._add_domain_to_github(query, 42, "")

        final_text = query.edit_message_text.await_args_list[-1].args[0]
        self.assertIn("提交结果暂时无法确认", final_text)
        self.assertIn("避免重复提交", final_text)
        self.assertNotIn("没有修改任何规则", final_text)
        markup = query.edit_message_text.await_args_list[-1].kwargs[
            "reply_markup"
        ]
        self.assertEqual(
            _button_labels(markup), ["🔍 前往查询", "🏠 返回首页"]
        )
        self.assertEqual(
            manager.user_states[42]["state"], "waiting_query_domain"
        )

    async def test_confirmed_write_edit_failures_keep_success_feedback(self):
        manager = self._stateful_manager()
        manager.user_states[42] = {
            "state": "waiting_description",
            "data": {
                "domain": "example.com",
                "check_result": {"recommendation": "ok"},
            },
            "updated_at": time.monotonic(),
        }
        manager.domain_checker = SimpleNamespace(
            get_target_domain_to_add=MagicMock(return_value="example.com")
        )
        manager._add_domain_with_limit = AsyncMock(
            return_value={
                "success": True,
                "commit_sha": "abc123",
                "commit_url": "https://github.com/example/repo/commit/abc123",
                "rate_limit_remaining": 49,
            }
        )
        manager._announce_private_addition = AsyncMock()
        processing = SimpleNamespace(
            edit_text=AsyncMock(
                side_effect=[
                    RuntimeError("result edit failed"),
                    RuntimeError("recovery edit failed"),
                ]
            )
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(side_effect=[processing, None]),
        )

        await manager._add_domain_to_github_message(message, 42, "")

        fallback_text = message.reply_text.await_args_list[-1].args[0]
        self.assertIn("直连规则已添加", fallback_text)
        self.assertIn("查看 GitHub 提交", fallback_text)
        self.assertNotIn("没有修改任何规则", fallback_text)
        manager._announce_private_addition.assert_awaited_once()
        self.assertEqual(
            manager.user_states[42]["state"], "waiting_add_domain"
        )

    async def test_rule_bot_client_reissue_confirmation_is_scoped_and_single_use(self):
        manager = self._stateful_manager()
        manager.rule_bot_client_token_service = SimpleNamespace(
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

        await manager._issue_rule_bot_client_token(query, 42)
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        callback_data = markup.inline_keyboard[0][0].callback_data

        await manager._confirm_rule_bot_client_issue(query, 99, callback_data)
        manager.rule_bot_client_token_service.issue.assert_not_awaited()

        await manager._confirm_rule_bot_client_issue(query, 42, callback_data)
        manager.rule_bot_client_token_service.issue.assert_awaited_once_with(42)

        await manager._confirm_rule_bot_client_issue(query, 42, callback_data)
        self.assertEqual(manager.rule_bot_client_token_service.issue.await_count, 1)

    async def test_invalid_description_preview_cannot_break_markdown(self):
        manager = self._stateful_manager()
        manager.MAX_DESCRIPTION_LENGTH = 20
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock())
        )

        await manager._handle_description_input(update, "`" * 21, 42)

        text = update.message.reply_text.await_args.args[0]
        self.assertNotIn("`" * 20, text)


if __name__ == "__main__":
    unittest.main()
