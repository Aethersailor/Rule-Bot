import tempfile
import unittest
import base64
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from src.handlers.handler_manager import HandlerManager
from src.services.github_service import GitHubService
from src.services.rule_bot_client_api import ListenerConfig, RuleBotClientAPIServer
from src.services.rule_bot_client_token_service import RuleBotClientTokenService
from src.utils.privacy import log_reference


class TestRuleBotClientTokens(unittest.IsolatedAsyncioTestCase):
    async def test_database_is_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tokens.sqlite3"
            RuleBotClientTokenService(database_path, "s" * 32, 90)

            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(database_path.stat().st_mode), 0o600)

    async def test_existing_token_database_reopens_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tokens.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE rule_bot_client_tokens (
                        user_id INTEGER PRIMARY KEY,
                        subject TEXT NOT NULL UNIQUE,
                        version INTEGER NOT NULL,
                        enabled INTEGER NOT NULL,
                        issued_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        last_used_at INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO rule_bot_client_tokens VALUES
                        (123, 'existing-subject', 4, 1, 100, 4102444800, 999)
                    """
                )

            service = RuleBotClientTokenService(database_path, "s" * 32, 90)
            status = await service.status(123)
            self.assertEqual(status["version"], 4)
            self.assertTrue(status["enabled"])
            with closing(sqlite3.connect(database_path)) as connection:
                last_used_at = connection.execute(
                    "SELECT last_used_at FROM rule_bot_client_tokens WHERE user_id = 123"
                ).fetchone()[0]
            self.assertIsNone(last_used_at)
            self.assertFalse(await service.has_current_consent(123))
            with self.assertRaises(PermissionError):
                await service.issue(123)
            await service.consent(123)
            with closing(sqlite3.connect(database_path)) as connection:
                last_used_at = connection.execute(
                    "SELECT last_used_at FROM rule_bot_client_tokens WHERE user_id = 123"
                ).fetchone()[0]
            self.assertIsNone(last_used_at)

    async def test_reissue_and_revoke_invalidate_old_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuleBotClientTokenService(
                Path(temp_dir) / "tokens.sqlite3", "s" * 32, 90
            )
            with self.assertRaises(PermissionError):
                await service.issue(123)
            await service.consent(123)

            first = await service.issue(123)
            verified_subject = await service.verify(first["token"])
            self.assertIsInstance(verified_subject, str)
            self.assertNotEqual(verified_subject, "123")
            encoded_payload = first["token"].split(".")[1]
            payload = json.loads(
                base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
            )
            self.assertIsInstance(payload["sub"], str)
            self.assertNotEqual(payload["sub"], "123")

            second = await service.issue(123)
            self.assertIsNone(await service.verify(first["token"]))
            self.assertIsInstance(await service.verify(second["token"]), str)

            self.assertTrue(await service.revoke(123))
            self.assertIsNone(await service.verify(second["token"]))

    async def test_withdrawal_stops_existing_token_until_new_consent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuleBotClientTokenService(
                Path(temp_dir) / "tokens.sqlite3", "s" * 32, 90
            )
            await service.consent(321)
            issued = await service.issue(321)
            self.assertIsNotNone(await service.verify(issued["token"]))

            self.assertTrue(await service.withdraw_consent(321))
            self.assertFalse(await service.has_current_consent(321))
            self.assertIsNone(await service.verify(issued["token"]))

    async def test_tampered_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuleBotClientTokenService(
                Path(temp_dir) / "tokens.sqlite3", "k" * 32, 90
            )
            await service.consent(456)
            issued = await service.issue(456)
            tampered = issued["token"][:-1] + (
                "A" if issued["token"][-1] != "A" else "B"
            )
            self.assertIsNone(await service.verify(tampered))

    def test_log_reference_is_stable_without_revealing_domain(self):
        first = log_reference("Sensitive.Example")
        second = log_reference("sensitive.example")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertNotIn("sensitive", first)


class TestRuleBotClientAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = SimpleNamespace(
            rule_bot_client_token_service=None,
            submit_rule_bot_client_domain=AsyncMock(
                return_value={
                    "status": "added",
                    "domain": "example.com",
                    "commit_url": "https://github.example/commit/abc",
                }
            ),
        )
        self.config = SimpleNamespace(
            RULE_BOT_CLIENT_PRIVATE_API_RATE_LIMIT_PER_HOUR=2,
            RULE_BOT_CLIENT_COMMUNITY_API_RATE_LIMIT_PER_HOUR=2,
        )
        self.api = RuleBotClientAPIServer(self.config, self.handler)
        self.listener = ListenerConfig(
            name="private",
            host="127.0.0.1",
            port=0,
            path="/api/hidden-test-path",
            source="rule_bot_client_private",
            static_token="p" * 32,
        )
        self.client = TestClient(TestServer(self.api._build_app(self.listener)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_route_is_concealed_and_requires_bearer_token(self):
        wrong_path = await self.client.post("/", json={"version": 1, "domain": "example.com"})
        self.assertEqual(wrong_path.status, 404)
        self.assertEqual(wrong_path.headers.get("Server"), "")
        wrong_method = await self.client.get(self.listener.path)
        self.assertEqual(wrong_method.status, 404)
        unauthorized = await self.client.post(
            self.listener.path, json={"version": 1, "domain": "example.com"}
        )
        self.assertEqual(unauthorized.status, 401)
        self.handler.submit_rule_bot_client_domain.assert_not_awaited()

    async def test_valid_request_uses_server_owned_source(self):
        response = await self.client.post(
            self.listener.path,
            headers={"Authorization": f"Bearer {'p' * 32}"},
            json={"version": 1, "domain": "www.example.com"},
        )

        self.assertEqual(response.status, 201)
        payload = await response.json()
        self.assertEqual(payload["status"], "added")
        self.handler.submit_rule_bot_client_domain.assert_awaited_once_with(
            "www.example.com",
            source="rule_bot_client_private",
            rate_key=("rule_bot_client_private:adds", 0),
            max_adds=2,
        )

    async def test_schema_is_strict_and_requests_are_rate_limited(self):
        headers = {"Authorization": f"Bearer {'p' * 32}"}
        invalid = await self.client.post(
            self.listener.path,
            headers=headers,
            json={"version": 1, "domain": "example.com", "source": "telegram"},
        )
        self.assertEqual(invalid.status, 400)
        accepted = await self.client.post(
            self.listener.path,
            headers=headers,
            json={"version": 1, "domain": "example.com"},
        )
        self.assertEqual(accepted.status, 201)
        limited = await self.client.post(
            self.listener.path,
            headers=headers,
            json={"version": 1, "domain": "example.com"},
        )
        self.assertEqual(limited.status, 429)

    async def test_terminal_status_contract_is_unchanged(self):
        self.config.RULE_BOT_CLIENT_PRIVATE_API_RATE_LIMIT_PER_HOUR = 20
        headers = {"Authorization": f"Bearer {'p' * 32}"}
        expected_http_statuses = {
            "added": 201,
            "exists_rules": 200,
            "exists_geosite": 200,
            "ignored_cn": 200,
            "rejected_policy": 200,
            "invalid_domain": 400,
        }

        for status, expected_http_status in expected_http_statuses.items():
            with self.subTest(status=status):
                self.handler.submit_rule_bot_client_domain.return_value = {
                    "status": status,
                    "domain": "example.com",
                }
                response = await self.client.post(
                    self.listener.path,
                    headers=headers,
                    json={"version": 1, "domain": "example.com"},
                )
                self.assertEqual(response.status, expected_http_status)
                self.assertEqual((await response.json())["status"], status)

    async def test_community_api_uses_opaque_token_subject(self):
        self.handler.rule_bot_client_token_service = SimpleNamespace(
            verify=AsyncMock(return_value="opaque-random-subject")
        )
        listener = ListenerConfig(
            name="community",
            host="127.0.0.1",
            port=0,
            path="/api/hidden-community-path",
            source="rule_bot_client_community",
        )
        client = TestClient(TestServer(self.api._build_app(listener)))
        await client.start_server()
        try:
            response = await client.post(
                listener.path,
                headers={"Authorization": "Bearer community-token"},
                json={"version": 1, "domain": "example.com"},
            )
            self.assertEqual(response.status, 201)
            self.handler.submit_rule_bot_client_domain.assert_awaited_with(
                "example.com",
                source="rule_bot_client_community",
                rate_key=("rule_bot_client_community:adds", "opaque-random-subject"),
                max_adds=2,
            )
        finally:
            await client.close()


class TestRuleBotClientSubmission(unittest.IsolatedAsyncioTestCase):
    def test_main_menu_button_follows_community_api_switch(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.config = SimpleNamespace(RULE_BOT_CLIENT_COMMUNITY_API_ENABLED=False)
        disabled_keyboard = manager._build_main_menu_keyboard().inline_keyboard
        disabled_labels = [
            button.text
            for row in disabled_keyboard
            for button in row
        ]
        manager.config.RULE_BOT_CLIENT_COMMUNITY_API_ENABLED = True
        enabled_labels = [
            button.text
            for row in manager._build_main_menu_keyboard().inline_keyboard
            for button in row
        ]
        self.assertNotIn("🔗 Rule-Bot Client", disabled_labels)
        self.assertIn("🔗 Rule-Bot Client", enabled_labels)
        self.assertEqual(
            [[button.text for button in row] for row in disabled_keyboard],
            [
                ["🔍 查询域名", "➕ 添加直连"],
                ["ℹ️ 使用帮助"],
                ["➖ 删除规则（暂未开放）"],
            ],
        )

    async def test_cn_and_invalid_domains_are_terminal_without_checks(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.check_and_add_domain_auto = AsyncMock()

        cn = await manager.submit_rule_bot_client_domain(
            "www.example.cn",
            source="rule_bot_client_private",
            rate_key=("private", 0),
            max_adds=10,
        )
        invalid = await manager.submit_rule_bot_client_domain(
            "not a domain",
            source="rule_bot_client_private",
            rate_key=("private", 0),
            max_adds=10,
        )

        self.assertEqual(cn["status"], "ignored_cn")
        self.assertEqual(invalid["status"], "invalid_domain")
        manager.check_and_add_domain_auto.assert_not_awaited()

    async def test_token_issue_requires_current_privacy_consent(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.user_states = {}
        manager._pending_actions = {}
        manager.MAX_USER_STATES = 4096
        manager.rule_bot_client_token_service = SimpleNamespace(
            has_current_consent=AsyncMock(return_value=False)
        )
        manager.group_service = SimpleNamespace(
            check_user_in_group=AsyncMock(return_value=True)
        )
        manager._show_rule_bot_client_privacy = AsyncMock()
        query = MagicMock()

        await manager._issue_rule_bot_client_token(query, 42)

        manager._show_rule_bot_client_privacy.assert_awaited_once_with(query, 42)
        manager.group_service.check_user_in_group.assert_not_awaited()

    async def test_subdomain_is_reduced_before_shared_business_logic(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.check_and_add_domain_auto = AsyncMock(
            return_value={"action": "exists", "reason": "rules"}
        )

        result = await manager.submit_rule_bot_client_domain(
            "https://a.b.example.com/path",
            source="rule_bot_client_community",
            rate_key=("rule_bot_client_community", 42),
            max_adds=50,
        )

        self.assertEqual(result, {"status": "exists_rules", "domain": "example.com"})
        manager.check_and_add_domain_auto.assert_awaited_once_with(
            "example.com",
            "Rule-Bot Client Community",
            user_id=("rule_bot_client_community", 42),
            source="rule_bot_client_community",
            max_adds=50,
        )

    async def test_confirmed_nxdomain_is_returned_as_terminal_invalid_domain(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.check_and_add_domain_auto = AsyncMock(
            return_value={"action": "error", "error_code": "nxdomain"}
        )

        result = await manager.submit_rule_bot_client_domain(
            "missing.example.com",
            source="rule_bot_client_private",
            rate_key=("rule_bot_client_private", 0),
            max_adds=50,
        )

        self.assertEqual(
            result,
            {"status": "invalid_domain", "domain": "example.com"},
        )

    async def test_confirmed_empty_dns_answer_is_terminal_policy_rejection(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.check_and_add_domain_auto = AsyncMock(
            return_value={"action": "error", "error_code": "empty_dns"}
        )

        result = await manager.submit_rule_bot_client_domain(
            "empty.example.com",
            source="rule_bot_client_private",
            rate_key=("rule_bot_client_private", 0),
            max_adds=50,
        )

        self.assertEqual(
            result,
            {"status": "rejected_policy", "domain": "example.com"},
        )

    async def test_duplicate_detected_during_write_is_terminal(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.github_service = SimpleNamespace(
            check_domain_in_rules=AsyncMock(return_value={"exists": False})
        )
        manager.data_manager = SimpleNamespace(
            is_domain_in_geosite=AsyncMock(return_value=False)
        )
        manager.domain_checker = SimpleNamespace(
            check_domain_comprehensive=AsyncMock(return_value={}),
            should_reject=MagicMock(return_value=False),
            get_target_domain_to_add=MagicMock(return_value="example.com"),
        )
        manager._add_domain_with_limit = AsyncMock(
            return_value={
                "success": False,
                "already_exists": True,
                "error": "already exists",
            }
        )

        result = await manager.check_and_add_domain_auto(
            "example.com", "Rule-Bot Client", user_id=("private", 0)
        )

        self.assertEqual(result["action"], "exists")
        self.assertEqual(result["reason"], "rules")


class TestRuleBotClientCommitIdentity(unittest.IsolatedAsyncioTestCase):
    def test_rule_bot_client_comments_are_recognized_as_managed(self):
        self.assertTrue(
            GitHubService._is_managed_rule_comment(
                "# add by Rule-Bot Client / Date: 2026-08-07 12:00:00"
            )
        )
        self.assertTrue(
            GitHubService._is_managed_rule_comment(
                "# add by Rule-Bot Client Community / Date: 2026-08-07 12:00:00"
            )
        )

    async def test_duplicate_write_is_marked_idempotent(self):
        service = GitHubService.__new__(GitHubService)
        service.config = SimpleNamespace(
            DIRECT_RULE_FILE="rules.list",
            GITHUB_REPO="example/repo",
            GITHUB_BRANCH="main",
        )
        service.repo = MagicMock()
        service.get_rule_file_data = AsyncMock(
            return_value={
                "content": "# existing\nDOMAIN-SUFFIX,example.com\n",
                "sha": "old-sha",
            }
        )

        result = await service._add_domain_to_rules_unlocked(
            "example.com", "Rule-Bot Client", source="rule_bot_client_private"
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["already_exists"])
        service.repo.update_file.assert_not_called()

    async def test_private_submission_has_rule_bot_client_commit_and_comment(self):
        service = GitHubService.__new__(GitHubService)
        service.config = SimpleNamespace(
            DIRECT_RULE_FILE="rules.list",
            GITHUB_REPO="example/repo",
            GITHUB_BRANCH="main",
            GITHUB_COMMIT_NAME="Rule-Bot",
            GITHUB_COMMIT_EMAIL="bot@example.com",
        )
        service.repo = MagicMock()
        service.repo.update_file.return_value = {
            "commit": SimpleNamespace(sha="abc123")
        }
        service.get_rule_file_data = AsyncMock(
            return_value={
                "content": "# 以下域名待提交 PR\n",
                "sha": "old-sha",
            }
        )
        service._file_cache = MagicMock()
        service._analysis_cache = MagicMock()

        result = await service._add_domain_to_rules_unlocked(
            "example.com", "Rule-Bot Client", source="rule_bot_client_private"
        )

        self.assertTrue(result["success"])
        update_args = service.repo.update_file.call_args.args
        self.assertEqual(
            update_args[1],
            "feat(rules): add direct domain example.com by Rule-Bot (from Rule-Bot Client)",
        )
        self.assertIn("# add by Rule-Bot Client / Date:", update_args[2])
        self.assertIn("DOMAIN-SUFFIX,example.com", update_args[2])

    async def test_community_submission_has_anonymous_source_identity(self):
        service = GitHubService.__new__(GitHubService)
        service.config = SimpleNamespace(
            DIRECT_RULE_FILE="rules.list",
            GITHUB_REPO="example/repo",
            GITHUB_BRANCH="main",
            GITHUB_COMMIT_NAME="Rule-Bot",
            GITHUB_COMMIT_EMAIL="bot@example.com",
        )
        service.repo = MagicMock()
        service.repo.update_file.return_value = {
            "commit": SimpleNamespace(sha="def456")
        }
        service.get_rule_file_data = AsyncMock(
            return_value={"content": "# 以下域名待提交 PR\n", "sha": "old-sha"}
        )
        service._file_cache = MagicMock()
        service._analysis_cache = MagicMock()

        result = await service._add_domain_to_rules_unlocked(
            "example.net",
            "Rule-Bot Client Community",
            source="rule_bot_client_community",
        )

        self.assertTrue(result["success"])
        update_args = service.repo.update_file.call_args.args
        self.assertEqual(
            update_args[1],
            "feat(rules): add direct domain example.net by Rule-Bot (from Rule-Bot Client Community)",
        )
        self.assertIn("# add by Rule-Bot Client Community / Date:", update_args[2])
        self.assertNotRegex(
            update_args[2],
            r"(?:user|Admin):\s*42\b",
        )


if __name__ == "__main__":
    unittest.main()
