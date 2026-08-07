import time
import re
import unicodedata
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from src.bot import RuleBot
from src.handlers.group_handler import GroupHandler
from src.handlers.handler_manager import HandlerManager


def _button_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _display_width(text):
    text = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", text)
    visible = re.sub(r"\\([_*`\[\]()~>#+\-=|{}.!])", r"\1", text)
    visible = visible.replace("*", "").replace("`", "")
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in visible
    )


def _assert_compact_page(case, text, *, max_lines=22, max_width=60):
    case.assertNotIn("\n\n\n", text)
    case.assertLessEqual(len(text.splitlines()), max_lines)
    for line in text.splitlines():
        case.assertLessEqual(
            _display_width(line),
            max_width,
            msg=f"line is too wide ({_display_width(line)}): {line!r}",
        )


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
        manager.MAX_DETAIL_LINES = 4
        manager.MAX_DETAIL_LINE_LENGTH = 56
        manager.MAX_DESCRIPTION_LENGTH = 20
        return manager

    def test_main_menu_preserves_complete_product_structure(self):
        manager = self._manager()

        text = manager._build_main_menu_text("Alice")
        keyboard = manager._build_main_menu_keyboard()
        labels = _button_labels(keyboard)
        callbacks = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }

        _assert_compact_page(self, text, max_lines=16)
        self.assertIn("欢迎使用 Rule-Bot", text)
        self.assertIn("🧭 *直连规则查询与提交助手*", text)
        self.assertIn("判断依据与公开范围", text)
        self.assertIn("🌍 明确确认后，规则才会写入公开 GitHub", text)
        for label in ("📚", "🌐", "🧾", "🌍"):
            self.assertIn(label, text)
        self.assertIn("📂 *公开仓库*", text)
        self.assertNotIn("•", text)
        self.assertIn("Aethersailor/Custom", text)
        self.assertIn("➖ 删除规则（暂未开放）", labels)
        self.assertIn("🔗 MatchScope", labels)
        self.assertIn("ℹ️ 使用帮助", labels)
        self.assertEqual(callbacks["➖ 删除规则（暂未开放）"], "delete_rule")
        self.assertEqual(len(keyboard.inline_keyboard), 3)
        self.assertLessEqual(max(len(row) for row in keyboard.inline_keyboard), 2)

    def test_help_lists_only_real_commands_and_visible_workflows(self):
        manager = self._manager()

        text = manager._build_help_text()

        keyboard = manager._build_help_keyboard()

        for command in ("/query", "/add", "/id", "/help", "/skip"):
            self.assertIn(command, text)
        self.assertIn("群聊", text)
        self.assertIn("MatchScope", text)
        self.assertIn("删除规则", text)
        self.assertNotIn(manager.config.GITHUB_REPO, text)
        self.assertEqual(_button_labels(keyboard), ["📂 查看公开仓库", "🏠 返回首页"])
        self.assertEqual(
            keyboard.inline_keyboard[0][0].url,
            "https://github.com/Aethersailor/Custom_OpenClash_Rules",
        )
        _assert_compact_page(self, text, max_lines=22)
        self.assertLess(len(text), 4096)

    def test_planned_delete_page_never_claims_availability(self):
        manager = self._manager()

        delete_text = manager._build_delete_unavailable_text()

        self.assertIn("后续版本", delete_text)
        self.assertIn("当前暂未开放", delete_text)
        self.assertIn("不会删除或修改任何规则", delete_text)
        self.assertIn("🛡️", delete_text)
        self.assertNotIn(manager.config.GITHUB_REPO, delete_text)
        self.assertNotIn(manager.config.DIRECT_RULE_FILE, delete_text)
        _assert_compact_page(self, delete_text, max_lines=7)

    def test_prompts_use_accurate_registered_domain_term(self):
        manager = self._manager()

        query = manager._build_query_prompt("📊 *当前数据*\n📚 可用")
        add = manager._build_add_prompt("📊 *当前数据*\n📚 可用")

        self.assertIn("可注册域名（主域名）", query)
        self.assertIn("可注册域名（主域名）", add)
        self.assertNotIn("二级域名", query + add)
        for label in ("📚", "🇨🇳", "🌐", "📡", "🧪", "📊"):
            self.assertIn(label, query)
        self.assertIn("🧾 *提交说明*", add)
        self.assertIn("只有明确确认后", add)
        self.assertNotIn("我会", query + add)
        _assert_compact_page(self, query, max_lines=19)
        _assert_compact_page(self, add, max_lines=19)

    def test_dynamic_details_are_bounded_for_narrow_screens(self):
        manager = self._manager()

        values = manager._format_value_list(
            ["2001:db8::1", "2001:db8::2", "2001:db8::3", "2001:db8::4"]
        )
        details = manager._format_detail_lines(["中" * 100, "short"])
        matches = manager._format_rule_matches(
            [{"line": 123, "rule": "DOMAIN-SUFFIX," + "x" * 100}]
        )

        self.assertEqual(len(values.splitlines()), 4)
        self.assertIn("另有 1 项", values)
        for line in (values + "\n" + details + "\n" + matches).splitlines():
            self.assertLessEqual(_display_width(line), 60)
        self.assertNotIn("   •", details + matches)

    def test_matchscope_pages_use_sections_instead_of_text_walls(self):
        manager = self._manager()

        access = manager._build_matchscope_access_text(
            "有效", "2026-08-07 10:00 UTC"
        )
        privacy = manager._build_matchscope_privacy_text(False)

        _assert_compact_page(self, access, max_lines=13)
        _assert_compact_page(self, privacy, max_lines=24, max_width=62)
        for heading in (
            "📱 *官方 MatchScope 客户端*",
            "🚫 *默认不会主动上报*",
            "👤 *账号关联*",
            "🌍 *公开范围*",
            "🌐 *网络与客户端*",
        ):
            self.assertIn(heading, privacy)
        self.assertNotIn("•", privacy)

    def test_description_and_failure_pages_keep_one_clear_action(self):
        manager = self._manager()
        user = SimpleNamespace(id=1, username="alice", first_name="Alice")

        description = manager._build_description_prompt_text("example.com", user)
        failure = manager._build_add_failure_text("example.com")

        self.assertIn("如不需要说明，可直接选择跳过", description)
        self.assertIn("🌍 *公开信息*", description)
        self.assertIn("提交者：@alice", description)
        self.assertIn("本次操作未修改任何规则", failure)
        _assert_compact_page(self, description, max_lines=15)
        _assert_compact_page(self, failure, max_lines=8)

    def test_uncertain_submission_page_never_claims_no_write(self):
        manager = self._manager()

        text = manager._build_submission_uncertain_text("example.com")

        self.assertIn("提交结果暂时无法确认", text)
        self.assertIn("避免重复提交", text)
        self.assertNotIn("没有修改任何规则", text)
        self.assertIn("🧾 *待核对规则*", text)
        _assert_compact_page(self, text, max_lines=8)

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

    def test_identity_collapses_lines_escapes_backslashes_and_limits_width(self):
        manager = self._manager()

        unusual = manager._format_telegram_identity(
            SimpleNamespace(id=3, username=None, first_name="Alice\nBob\\")
        )
        long_name = manager._format_telegram_identity(
            SimpleNamespace(id=4, username=None, first_name="中" * 100)
        )

        self.assertEqual(unusual, "Alice Bob\\\\")
        self.assertNotIn("\n", unusual)
        self.assertTrue(long_name.endswith("…"))
        self.assertLessEqual(_display_width(long_name), 42)

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
        _assert_compact_page(self, text, max_lines=16)

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

    async def test_delete_placeholder_resets_state_without_deleting_rules(self):
        manager = self._stateful_manager()
        manager.github_service = SimpleNamespace(remove_domain_from_rules=AsyncMock())
        query = SimpleNamespace(edit_message_text=AsyncMock())

        await manager._show_delete_not_supported(query, 42)

        self.assertEqual(manager.user_states[42]["state"], "idle")
        self.assertEqual(manager.user_states[42]["data"], {})
        self.assertNotIn((42, "old"), manager._pending_actions)
        manager.github_service.remove_domain_from_rules.assert_not_awaited()

        text = query.edit_message_text.await_args.args[0]
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("当前暂未开放", text)
        self.assertEqual(_button_labels(markup), ["🏠 返回首页"])
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            "main_menu",
        )

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

    async def test_query_summary_is_compact_and_details_keep_network_values(self):
        manager = self._stateful_manager()
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"exists": False})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(
                return_value={
                    "domain_ips": [
                        "2001:db8::1",
                        "2001:db8::2",
                        "2001:db8::3",
                        "2001:db8::4",
                    ],
                    "second_level_ips": ["1.2.3.4"],
                    "details": ["境外解析结果"],
                    "domain_china_status": False,
                    "second_level_china_status": False,
                    "ns_china_status": False,
                    "recommendation": "未检测到中国大陆信号",
                }
            )
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock(return_value=processing)),
            effective_user=SimpleNamespace(id=42),
        )

        await manager._handle_domain_query(update, "example.com", 42)

        final = processing.edit_text.await_args_list[-1]
        summary = final.args[0]
        self.assertTrue(summary.startswith("ℹ️ *暂不建议添加*"))
        self.assertIn("🌐 DNS 解析：输入 4 · 可注册域名 1", summary)
        self.assertNotIn("2001:db8::1", summary)
        _assert_compact_page(self, summary, max_lines=11)
        self.assertEqual(
            _button_labels(final.kwargs["reply_markup"]),
            ["🔎 技术详情", "🔍 查询其他域名", "🏠 返回首页"],
        )
        self.assertEqual(
            len(final.kwargs["reply_markup"].inline_keyboard[-1]), 2
        )

        details_button = final.kwargs["reply_markup"].inline_keyboard[0][0]
        query = SimpleNamespace(edit_message_text=AsyncMock())
        await manager._show_query_result_page(
            query,
            42,
            details_button.callback_data,
            detail=True,
        )

        detail = query.edit_message_text.await_args.args[0]
        self.assertIn("输入域名 IP\n• 2001:db8::1", detail)
        self.assertIn("• 另有 1 项", detail)
        self.assertNotIn(", 2001:db8", detail)
        self.assertIn(
            "↩️ 返回摘要",
            _button_labels(query.edit_message_text.await_args.kwargs["reply_markup"]),
        )
        manager.github_service.check_domain_in_rules.assert_awaited_once()
        manager.data_manager.is_domain_in_geosite.assert_awaited_once()
        manager.domain_checker.check_domain_comprehensive.assert_awaited_once()

    def test_query_summary_has_one_business_conclusion(self):
        manager = self._stateful_manager()
        check_result = {
            "normalized_domain": "example.com",
            "second_level_domain": "example.com",
            "domain_ips": ["1.2.3.4"],
            "second_level_ips": [],
            "domain_china_status": True,
            "second_level_china_status": False,
            "ns_china_status": False,
            "recommendation": "✅ 添加可注册域名 example.com",
        }

        available = manager._build_query_summary_text(
            "example.com",
            {"exists": False},
            False,
            check_result,
            "✅ *可以继续评估添加*",
        )
        covered = manager._build_query_summary_text(
            "example.com",
            {"exists": True, "matches": [{"rule": "example.com"}]},
            False,
            check_result,
            "✅ *已被直连规则覆盖*",
        )

        self.assertIn("主域名同输入", available)
        self.assertNotIn("可注册域名 0", available)
        self.assertIn("判断：", available)
        self.assertNotIn("判断：", covered)
        self.assertNotIn("建议添加", covered)

    async def test_repeated_query_page_click_keeps_current_page(self):
        manager = self._stateful_manager()
        token = manager.create_pending_action(
            42,
            "query_result_pages",
            summary_text="summary",
            detail_text="details",
            add_callback="",
        )
        query = SimpleNamespace(
            edit_message_text=AsyncMock(
                side_effect=BadRequest("Message is not modified")
            )
        )

        await manager._show_query_result_page(
            query,
            42,
            f"query_details|{token}",
            detail=True,
        )

        self.assertEqual(
            manager.user_states[42]["state"], "waiting_query_domain"
        )

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

    def test_extreme_query_result_keeps_default_page_short(self):
        manager = self._stateful_manager()
        github_result = {
            "exists": True,
            "matches": [
                {"line": index, "rule": "DOMAIN-SUFFIX," + "x" * 80}
                for index in range(1, 8)
            ],
        }
        check_result = {
            "domain_ips": [f"2001:db8::{index}" for index in range(8)],
            "second_level_ips": [f"192.0.2.{index}" for index in range(8)],
            "details": ["中" * 100 for _ in range(8)],
            "domain_china_status": True,
            "second_level_china_status": True,
            "ns_china_status": True,
            "recommendation": "中" * 200,
        }

        summary = manager._build_query_summary_text(
            "example.com",
            github_result,
            False,
            check_result,
            "✅ *已被直连规则覆盖*",
        )
        detail = manager._build_query_detail_text(
            "example.com", github_result, check_result
        )

        _assert_compact_page(self, summary, max_lines=11)
        self.assertNotIn("2001:db8::", summary)
        self.assertIn("7 条匹配", summary)
        self.assertLess(len(detail), 4096)
        self.assertIn("另有 3 条匹配未显示", detail)
        self.assertIn("另有 5 项", detail)
        for line in detail.splitlines():
            self.assertLessEqual(_display_width(line), 60)

        long_domain = ".".join(["a" * 63, "b" * 63, "c" * 40, "com"])
        long_summary = manager._build_query_summary_text(
            long_domain,
            {"exists": False},
            False,
            check_result,
            "ℹ️ *暂不建议添加*",
        )
        long_detail = manager._build_query_detail_text(
            long_domain, {"exists": False}, check_result
        )
        for line in (long_summary + "\n" + long_detail).splitlines():
            self.assertLessEqual(_display_width(line), 60)

    async def test_add_review_page_has_confirm_cancel_and_home(self):
        manager = self._stateful_manager()
        manager.check_user_add_limit = MagicMock(return_value=(True, 50))
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"exists": False})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(
                return_value={
                    "recommendation": "检测到中国大陆 IP",
                    "details": ["1.2.3.4 位于中国大陆"],
                }
            ),
            get_target_domain_to_add=MagicMock(return_value="example.com"),
            should_reject=MagicMock(return_value=False),
            should_add_directly=MagicMock(return_value=True),
        )
        processing = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock(return_value=processing)),
            effective_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
        )

        await manager._handle_add_domain_input(update, "example.com", 42)

        final = processing.edit_text.await_args_list[-1]
        self.assertTrue(final.args[0].startswith("✅ *可以提交直连规则*"))
        self.assertNotIn("检查详情", final.args[0])
        _assert_compact_page(self, final.args[0], max_lines=13)
        labels = _button_labels(final.kwargs["reply_markup"])
        self.assertEqual(labels, ["✅ 确认公开提交", "↩️ 取消", "🏠 返回首页"])

    async def test_rejected_add_page_has_policy_status_without_duplicate_title(self):
        manager = self._stateful_manager()
        manager.check_user_add_limit = MagicMock(return_value=(True, 50))
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"exists": False})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(
                return_value={
                    "recommendation": "未检测到中国大陆 IP 或 NS",
                    "details": [],
                }
            ),
            get_target_domain_to_add=MagicMock(return_value="example.com"),
            should_reject=MagicMock(return_value=True),
            should_add_directly=MagicMock(return_value=False),
        )
        manager.is_admin = MagicMock(return_value=False)
        processing = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock(return_value=processing)),
            effective_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
        )

        await manager._handle_add_domain_input(update, "example.com", 42)

        final = processing.edit_text.await_args_list[-1]
        text = final.args[0]
        self.assertEqual(text.count("暂不符合添加条件"), 1)
        self.assertTrue(text.startswith("⛔"))
        self.assertEqual(
            _button_labels(final.kwargs["reply_markup"]),
            ["➕ 添加其他域名", "🏠 返回首页"],
        )

    async def test_query_to_rejected_add_keeps_next_domain_action(self):
        for is_admin, expected in (
            (False, ["➕ 添加其他域名", "🏠 返回首页"]),
            (
                True,
                [
                    "🛡️ 管理员权限添加",
                    "➕ 添加其他域名",
                    "🏠 返回首页",
                ],
            ),
        ):
            manager = self._stateful_manager()
            manager.is_admin = MagicMock(return_value=is_admin)
            manager.domain_checker = SimpleNamespace(
                check_domain_comprehensive=AsyncMock(
                    return_value={
                        "recommendation": "不符合条件",
                        "details": [],
                    }
                ),
                get_target_domain_to_add=MagicMock(return_value="example.com"),
                should_reject=MagicMock(return_value=True),
            )
            token = manager.create_pending_action(
                42, "add_domain", domain="example.com"
            )
            query = SimpleNamespace(
                from_user=SimpleNamespace(
                    id=42, username="alice", first_name="Alice"
                ),
                edit_message_text=AsyncMock(),
            )

            await manager._handle_add_domain_callback(
                query, 42, f"add_domain|{token}"
            )

            markup = query.edit_message_text.await_args.kwargs["reply_markup"]
            self.assertEqual(_button_labels(markup), expected)
            self.assertEqual(manager.user_states[42]["state"], "waiting_add_domain")

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
        self.assertIn("吊销当前 Token", revoke_text)
        revoke_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("🚫 确认吊销", _button_labels(revoke_markup))
        manager.matchscope_token_service.revoke.assert_not_awaited()

        await manager._withdraw_matchscope_privacy(query, 42)
        withdraw_text = query.edit_message_text.await_args.args[0]
        self.assertIn("撤回隐私同意", withdraw_text)
        withdraw_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("🚫 确认撤回并吊销", _button_labels(withdraw_markup))
        manager.matchscope_token_service.withdraw_consent.assert_not_awaited()

    async def test_membership_page_has_join_and_force_refresh_recovery(self):
        manager = self._stateful_manager()
        keyboard = SimpleNamespace(inline_keyboard=[])
        manager.group_service = SimpleNamespace(
            is_group_check_enabled=MagicMock(return_value=True),
            check_user_in_group=AsyncMock(return_value=False),
            get_join_group_message=MagicMock(return_value="join"),
            get_join_group_keyboard=MagicMock(return_value=keyboard),
        )
        callback = SimpleNamespace(
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            callback_query=callback,
        )

        allowed = await manager.check_group_membership(
            update,
            force_refresh=True,
            callback_answered=True,
        )

        self.assertFalse(allowed)
        manager.group_service.check_user_in_group.assert_awaited_once_with(
            42, force_refresh=True
        )
        callback.answer.assert_not_awaited()
        self.assertIs(
            callback.edit_message_text.await_args.kwargs["reply_markup"],
            keyboard,
        )

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

    async def test_group_admin_force_callback_remains_routable(self):
        manager = self._stateful_manager()
        manager.config.ALLOWED_GROUP_IDS = {-100123}
        manager.check_group_membership = AsyncMock(return_value=True)
        manager._handle_admin_force_add_callback = AsyncMock()
        query = SimpleNamespace(
            data="admin_force_add|token",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(type="supergroup", id=-100123),
        )

        await manager.handle_callback(update, None)

        query.answer.assert_not_awaited()
        manager.check_group_membership.assert_not_awaited()
        manager._handle_admin_force_add_callback.assert_awaited_once_with(
            query, 42, "admin_force_add|token"
        )

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

    async def test_group_admin_token_cleanup_race_does_not_edit_shared_message(self):
        manager = self._stateful_manager()
        manager.config.ADMIN_USER_IDS = {42}
        manager.get_pending_action = MagicMock(
            side_effect=[{"domain": "example.com"}, None]
        )
        query = SimpleNamespace(
            data="admin_force_add|token",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(type="supergroup")),
        )

        await manager._handle_admin_force_add_callback(
            query, 42, "admin_force_add|token"
        )

        query.answer.assert_awaited_once_with()
        self.assertEqual(manager.get_pending_action.call_count, 2)
        query.edit_message_text.assert_not_awaited()

    async def test_group_admin_answer_failure_does_not_edit_shared_message(self):
        manager = self._stateful_manager()
        manager.config.ALLOWED_GROUP_IDS = {-100123}
        manager.config.ADMIN_USER_IDS = {42}
        token = manager.create_pending_action(
            42, "admin_force_add", domain="example.com"
        )
        query = SimpleNamespace(
            data=f"admin_force_add|{token}",
            answer=AsyncMock(side_effect=RuntimeError("telegram unavailable")),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="supergroup")
            ),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=43),
            effective_chat=SimpleNamespace(type="supergroup", id=-100123),
        )

        await manager.handle_callback(update, None)

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

    async def test_private_admin_force_add_keeps_private_navigation(self):
        manager = self._stateful_manager()
        manager.config.ADMIN_USER_IDS = {42}
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
            return_value={"success": False, "error": "definite failure"}
        )
        token = manager.create_pending_action(
            42, "admin_force_add", domain="example.com"
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            edit_message_text=AsyncMock(),
        )

        await manager._handle_admin_force_add_callback(
            query, 42, f"admin_force_add|{token}"
        )
        confirmation = query.edit_message_text.await_args_list[-1]
        manager._add_domain_with_limit.assert_not_awaited()
        confirm_callback = confirmation.kwargs["reply_markup"].inline_keyboard[
            0
        ][0].callback_data

        await manager._handle_admin_force_add_callback(
            query, 42, confirm_callback
        )

        final = query.edit_message_text.await_args_list[-1]
        self.assertEqual(
            _button_labels(final.kwargs["reply_markup"]),
            ["➕ 继续添加", "🏠 返回首页"],
        )
        self.assertEqual(manager.user_states[42]["state"], "waiting_add_domain")

    async def test_matchscope_access_hides_long_endpoint_in_copy_button(self):
        manager = self._stateful_manager()
        manager.matchscope_token_service = SimpleNamespace(
            status=AsyncMock(
                return_value={"enabled": True, "expires_at": int(time.time()) + 3600}
            ),
            has_current_consent=AsyncMock(return_value=True),
        )
        query = SimpleNamespace(edit_message_text=AsyncMock())

        await manager._show_matchscope_access(query, 42)

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

    async def test_credential_message_distinguishes_delete_from_revoke(self):
        manager = self._stateful_manager()
        manager.group_service = SimpleNamespace(
            check_user_in_group=AsyncMock(return_value=True)
        )
        manager.matchscope_token_service = SimpleNamespace(
            issue=AsyncMock(
                return_value={
                    "token": "secret-token",
                    "expires_at": int(time.time()) + 3600,
                }
            )
        )
        manager._show_matchscope_access = AsyncMock()
        query = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

        await manager._perform_matchscope_issue(query, 42)

        text = query.message.reply_text.await_args.args[0]
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertIn("只显示一次", text)
        self.assertIn("删除消息不会吊销 Token", text)
        self.assertEqual(_button_labels(markup)[-1], "🗑️ 删除凭据消息")

    async def test_invalid_description_keeps_skip_action_visible(self):
        manager = self._stateful_manager()
        manager.MAX_DESCRIPTION_LENGTH = 20
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

        await manager._handle_description_input(update, "x" * 21, 42)

        markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            _button_labels(markup),
            ["⏭️ 不填说明，直接提交", "↩️ 取消"],
        )

    async def test_add_exception_replaces_processing_page_with_recovery(self):
        manager = self._stateful_manager()
        manager.check_user_add_limit = MagicMock(side_effect=RuntimeError("boom"))
        processing = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock(return_value=processing))
        )

        await manager._handle_add_domain_input(update, "example.com", 42)

        update.message.reply_text.assert_awaited_once()
        final = processing.edit_text.await_args
        self.assertIn("本次操作未提交或修改任何规则", final.args[0])
        self.assertIn("🏠 返回首页", _button_labels(final.kwargs["reply_markup"]))

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

    async def test_uncertain_service_result_is_not_rendered_as_definite_failure(self):
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
                "success": False,
                "submission_uncertain": True,
                "error": "response lost",
            }
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=42, username="alice"),
            edit_message_text=AsyncMock(),
        )

        await manager._add_domain_to_github(query, 42, "")

        final_text = query.edit_message_text.await_args_list[-1].args[0]
        self.assertIn("提交结果暂时无法确认", final_text)
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

    async def test_callback_success_falls_back_before_group_announcement(self):
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
        events = []

        async def fallback_reply(*args, **kwargs):
            events.append("fallback")

        async def announce(*args, **kwargs):
            events.append("announcement")
            return True

        manager._announce_private_addition = AsyncMock(
            side_effect=announce
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(side_effect=fallback_reply),
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="alice", first_name="Alice"
            ),
            message=message,
            edit_message_text=AsyncMock(
                side_effect=[None, RuntimeError("result edit failed")]
            ),
        )

        await manager._add_domain_to_github(query, 42, "")

        fallback_text = message.reply_text.await_args.args[0]
        self.assertIn("直连规则已添加", fallback_text)
        self.assertEqual(events, ["fallback", "announcement"])
        self.assertEqual(
            manager.user_states[42]["state"], "waiting_add_domain"
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
