import tempfile
import unittest
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from src.handlers.handler_manager import HandlerManager
from src.services.github_service import GitHubService
from src.services.matchscope_api import ListenerConfig, MatchScopeAPIServer
from src.services.matchscope_token_service import MatchScopeTokenService


class TestMatchScopeTokens(unittest.IsolatedAsyncioTestCase):
    async def test_reissue_and_revoke_invalidate_old_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = MatchScopeTokenService(
                Path(temp_dir) / "tokens.sqlite3", "s" * 32, 90
            )

            first = await service.issue(123)
            self.assertEqual(await service.verify(first["token"]), 123)
            encoded_payload = first["token"].split(".")[1]
            payload = json.loads(
                base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
            )
            self.assertIsInstance(payload["sub"], str)
            self.assertNotEqual(payload["sub"], "123")

            second = await service.issue(123)
            self.assertIsNone(await service.verify(first["token"]))
            self.assertEqual(await service.verify(second["token"]), 123)

            self.assertTrue(await service.revoke(123))
            self.assertIsNone(await service.verify(second["token"]))

    async def test_tampered_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = MatchScopeTokenService(
                Path(temp_dir) / "tokens.sqlite3", "k" * 32, 90
            )
            issued = await service.issue(456)
            tampered = issued["token"][:-1] + (
                "A" if issued["token"][-1] != "A" else "B"
            )
            self.assertIsNone(await service.verify(tampered))


class TestMatchScopeAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = SimpleNamespace(
            matchscope_token_service=None,
            submit_matchscope_domain=AsyncMock(
                return_value={
                    "status": "added",
                    "domain": "example.com",
                    "commit_url": "https://github.example/commit/abc",
                }
            ),
        )
        self.config = SimpleNamespace(
            MATCHSCOPE_PRIVATE_RATE_LIMIT_PER_HOUR=2,
            MATCHSCOPE_PUBLIC_RATE_LIMIT_PER_HOUR=2,
        )
        self.api = MatchScopeAPIServer(self.config, self.handler)
        self.listener = ListenerConfig(
            name="private",
            host="127.0.0.1",
            port=0,
            path="/api/hidden-test-path",
            source="matchscope_private",
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
        self.handler.submit_matchscope_domain.assert_not_awaited()

    async def test_valid_request_uses_server_owned_source(self):
        response = await self.client.post(
            self.listener.path,
            headers={"Authorization": f"Bearer {'p' * 32}"},
            json={"version": 1, "domain": "www.example.com"},
        )

        self.assertEqual(response.status, 201)
        payload = await response.json()
        self.assertEqual(payload["status"], "added")
        self.handler.submit_matchscope_domain.assert_awaited_once_with(
            "www.example.com",
            source="matchscope_private",
            rate_key=("matchscope_private:adds", 0),
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


class TestMatchScopeSubmission(unittest.IsolatedAsyncioTestCase):
    def test_main_menu_button_follows_public_api_switch(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.config = SimpleNamespace(MATCHSCOPE_PUBLIC_API_ENABLED=False)
        disabled_labels = [
            button.text
            for row in manager._build_main_menu_keyboard().inline_keyboard
            for button in row
        ]
        manager.config.MATCHSCOPE_PUBLIC_API_ENABLED = True
        enabled_labels = [
            button.text
            for row in manager._build_main_menu_keyboard().inline_keyboard
            for button in row
        ]
        self.assertNotIn("🔗 MatchScope 接入", disabled_labels)
        self.assertIn("🔗 MatchScope 接入", enabled_labels)

    async def test_cn_and_invalid_domains_are_terminal_without_checks(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.check_and_add_domain_auto = AsyncMock()

        cn = await manager.submit_matchscope_domain(
            "www.example.cn",
            source="matchscope_private",
            rate_key=("private", 0),
            max_adds=10,
        )
        invalid = await manager.submit_matchscope_domain(
            "not a domain",
            source="matchscope_private",
            rate_key=("private", 0),
            max_adds=10,
        )

        self.assertEqual(cn["status"], "ignored_cn")
        self.assertEqual(invalid["status"], "invalid_domain")
        manager.check_and_add_domain_auto.assert_not_awaited()

    async def test_subdomain_is_reduced_before_shared_business_logic(self):
        manager = HandlerManager.__new__(HandlerManager)
        manager.check_and_add_domain_auto = AsyncMock(
            return_value={"action": "exists", "reason": "rules"}
        )

        result = await manager.submit_matchscope_domain(
            "https://a.b.example.com/path",
            source="matchscope_community",
            rate_key=("public", 42),
            max_adds=50,
        )

        self.assertEqual(result, {"status": "exists_rules", "domain": "example.com"})
        manager.check_and_add_domain_auto.assert_awaited_once_with(
            "example.com",
            "MatchScope Community",
            user_id=("public", 42),
            source="matchscope_community",
            max_adds=50,
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
            "example.com", "MatchScope", user_id=("private", 0)
        )

        self.assertEqual(result["action"], "exists")
        self.assertEqual(result["reason"], "rules")


class TestMatchScopeCommitIdentity(unittest.IsolatedAsyncioTestCase):
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
            "example.com", "MatchScope", source="matchscope_private"
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["already_exists"])
        service.repo.update_file.assert_not_called()

    async def test_private_submission_has_matchscope_commit_and_comment(self):
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
            "example.com", "MatchScope", source="matchscope_private"
        )

        self.assertTrue(result["success"])
        update_args = service.repo.update_file.call_args.args
        self.assertEqual(
            update_args[1],
            "feat(rules): add direct domain example.com by Rule-Bot (from MatchScope)",
        )
        self.assertIn("# add by MatchScope / Date:", update_args[2])
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
            "MatchScope Community",
            source="matchscope_community",
        )

        self.assertTrue(result["success"])
        update_args = service.repo.update_file.call_args.args
        self.assertEqual(
            update_args[1],
            "feat(rules): add direct domain example.net by Rule-Bot (from MatchScope Community)",
        )
        self.assertIn("# add by MatchScope Community / Date:", update_args[2])
        self.assertNotIn("42", update_args[2])


if __name__ == "__main__":
    unittest.main()
