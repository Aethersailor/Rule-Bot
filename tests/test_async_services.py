import asyncio
import threading
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os
import base64

from github import GithubException

# Add repo root to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.services.dns_service import DNSService
from src.services.github_service import GitHubService
from src.config import Config
from src.handlers.handler_manager import HandlerManager

class TestServices(unittest.IsolatedAsyncioTestCase):
    async def test_dns_service_lifecycle(self):
        print("\nTesting DNSService lifecycle...")
        service = DNSService({'google': 'https://dns.google/dns-query'})
        
        # Test start
        await service.start()
        self.assertIsNotNone(service.session)
        self.assertFalse(service.session.closed)
        print("DNSService started successfully, session created.")
        
        # Test close
        await service.close()
        self.assertTrue(service.session.closed)
        print("DNSService closed successfully.")

    async def test_github_service_async_wrapper(self):
        print("\nTesting GitHubService async wrapper...")
        config = MagicMock(spec=Config)
        config.GITHUB_TOKEN = "token"
        config.GITHUB_REPO = "test/repo"
        config.GITHUB_COMMIT_NAME = "Rule-Bot"
        config.GITHUB_COMMIT_EMAIL = "noreply@users.noreply.github.com"
        config.DIRECT_RULE_FILE = "rule.list"
        config.GITHUB_BRANCH = "release"

        with patch.object(GitHubService, "_initialize_repo"):
            service = GitHubService(config)
        service.repo = MagicMock()
        
        # Mock get_contents to return a mock file content
        file_content_str = "test content"
        encoded_content = base64.b64encode(file_content_str.encode('utf-8')).decode('utf-8')
        
        mock_content = MagicMock()
        mock_content.content = encoded_content
        service.repo.get_contents.return_value = mock_content
        
        # Test async get_rule_file_content
        content = await service.get_rule_file_content("test.txt")
        self.assertEqual(content, file_content_str)
        service.repo.get_contents.assert_called_once_with("test.txt", ref="release")
        print(f"Async get_rule_file_content returned correct content: {content}")
        
    async def test_github_service_add_domain_wrapper(self):
        print("\nTesting GitHubService add_domain wrapper...")
        config = MagicMock(spec=Config)
        config.GITHUB_TOKEN = "token"
        config.DIRECT_RULE_FILE = "rule.list"
        config.GITHUB_REPO = "test/repo"
        config.GITHUB_COMMIT_NAME = "bot"
        config.GITHUB_COMMIT_EMAIL = "bot@test.com"
        config.GITHUB_BRANCH = "dev"
        
        with patch.object(GitHubService, "_initialize_repo"):
            service = GitHubService(config)
        service.repo = MagicMock()
        
        # Mock existing content
        existing_content = "# initial\n"
        encoded_content = base64.b64encode(existing_content.encode('utf-8')).decode('utf-8')
        mock_file = MagicMock()
        mock_file.content = encoded_content
        mock_file.sha = "old_sha"
        service.repo.get_contents.return_value = mock_file
        
        # Mock update_file
        mock_commit = MagicMock()
        mock_commit.sha = "new_sha"
        service.repo.update_file.return_value = {'commit': mock_commit}
        
        # Test async add_domain_to_rules
        result = await service.add_domain_to_rules("example.com", "user", "desc")
        
        self.assertTrue(result["success"])
        self.assertEqual(result["commit_sha"], "new_sha")
        self.assertEqual(service.repo.get_contents.call_args.kwargs.get("ref"), "dev")
        self.assertEqual(service.repo.update_file.call_args.kwargs.get("branch"), "dev")
        print("Async add_domain_to_rules executed successfully.")

    async def test_github_service_remove_domain_preserves_section_marker(self):
        config = MagicMock(spec=Config)
        config.GITHUB_TOKEN = "token"
        config.DIRECT_RULE_FILE = "rule.list"
        config.GITHUB_REPO = "test/repo"
        config.GITHUB_COMMIT_NAME = "bot"
        config.GITHUB_COMMIT_EMAIL = "bot@test.com"
        config.GITHUB_BRANCH = "dev"

        with patch.object(GitHubService, "_initialize_repo"):
            service = GitHubService(config)
        service.repo = MagicMock()

        existing_content = (
            "# 以下域名待提交 PR\n"
            "DOMAIN-SUFFIX,example.com\n"
            "# add by Telegram user: someone / Date: 2026-05-07 00:00:00\n"
            "DOMAIN-SUFFIX,other.com\n"
        )
        encoded_content = base64.b64encode(existing_content.encode("utf-8")).decode("utf-8")
        mock_file = MagicMock()
        mock_file.content = encoded_content
        mock_file.sha = "old_sha"
        service.repo.get_contents.return_value = mock_file

        mock_commit = MagicMock()
        mock_commit.sha = "new_sha"
        service.repo.update_file.return_value = {"commit": mock_commit}

        result = await service.remove_domain_from_rules("example.com", "user")

        self.assertTrue(result["success"])
        updated_content = service.repo.update_file.call_args.args[2]
        self.assertIn("# 以下域名待提交 PR", updated_content)
        self.assertIn("DOMAIN-SUFFIX,other.com", updated_content)

    async def test_github_rule_analysis_is_reused_by_queries_and_stats(self):
        config = MagicMock(spec=Config)
        config.GITHUB_TOKEN = "token"
        config.DIRECT_RULE_FILE = "rule.list"
        config.GITHUB_REPO = "test/repo"
        config.GITHUB_BRANCH = "master"
        config.GITHUB_FILE_CACHE_SIZE = 4
        config.GITHUB_FILE_CACHE_TTL = 60

        with patch.object(GitHubService, "_initialize_repo"):
            service = GitHubService(config)
        service.repo = MagicMock()

        content = (
            "# rules\n"
            "DOMAIN-SUFFIX,example.com\n"
            "DOMAIN-SUFFIX,other.com\n"
        )
        mock_file = MagicMock()
        mock_file.content = base64.b64encode(content.encode()).decode()
        mock_file.sha = "same_sha"
        service.repo.get_contents.return_value = mock_file

        first = await service.check_domain_in_rules("sub.example.com")
        second = await service.check_domain_in_rules("example.com")
        stats = await service.get_file_stats()

        self.assertTrue(first["exists"])
        self.assertEqual(first["matches"][0]["type"], "suffix_match")
        self.assertTrue(second["exists"])
        self.assertEqual(second["matches"][0]["type"], "exact_match")
        self.assertEqual(stats["rule_count"], 2)
        self.assertEqual(stats["comment_count"], 1)
        self.assertEqual(service.repo.get_contents.call_count, 1)
        self.assertEqual(len(service._analysis_cache), 1)

    async def test_github_service_rejects_multiline_description(self):
        config = MagicMock(spec=Config)
        config.GITHUB_TOKEN = "token"
        config.DIRECT_RULE_FILE = "rule.list"
        config.GITHUB_REPO = "test/repo"
        config.GITHUB_COMMIT_NAME = "bot"
        config.GITHUB_COMMIT_EMAIL = "bot@test.com"
        config.GITHUB_BRANCH = "master"

        with patch.object(GitHubService, "_initialize_repo"):
            service = GitHubService(config)
        service.repo = MagicMock()

        result = await service.add_domain_to_rules(
            "example.com",
            "user",
            "x\nDOMAIN-SUFFIX,ai\nx",
        )

        self.assertFalse(result["success"])
        service.repo.update_file.assert_not_called()

    async def test_github_commit_transport_failure_is_marked_uncertain(self):
        service = GitHubService.__new__(GitHubService)
        service.config = MagicMock(spec=Config)
        service.config.DIRECT_RULE_FILE = "rule.list"
        service.config.GITHUB_REPO = "test/repo"
        service.config.GITHUB_BRANCH = "master"
        service.config.GITHUB_COMMIT_NAME = "bot"
        service.config.GITHUB_COMMIT_EMAIL = "bot@test.com"
        service.repo = MagicMock()
        service.repo.update_file.side_effect = OSError(
            "connection lost after request"
        )
        service.get_rule_file_data = AsyncMock(
            return_value={
                "content": "# 以下域名待提交 PR\n",
                "sha": "old-sha",
            }
        )

        result = await service._add_domain_to_rules_unlocked(
            "example.com", "user"
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["submission_uncertain"])

    async def test_cancelled_update_file_marks_result_uncertain(self):
        service = GitHubService.__new__(GitHubService)
        service.config = MagicMock(spec=Config)
        service.config.DIRECT_RULE_FILE = "rule.list"
        service.config.GITHUB_REPO = "test/repo"
        service.config.GITHUB_BRANCH = "master"
        service.config.GITHUB_COMMIT_NAME = "bot"
        service.config.GITHUB_COMMIT_EMAIL = "bot@test.com"
        service.repo = MagicMock()
        started = threading.Event()
        release = threading.Event()

        def blocking_update(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return {"commit": SimpleNamespace(sha="late-commit")}

        service.repo.update_file.side_effect = blocking_update
        service.get_rule_file_data = AsyncMock(
            return_value={
                "content": "# 以下域名待提交 PR\n",
                "sha": "old-sha",
            }
        )
        task = asyncio.create_task(
            service._add_domain_to_rules_unlocked("example.com", "user")
        )

        try:
            entered = await asyncio.to_thread(started.wait, 2)
            self.assertTrue(entered)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertTrue(caught.exception.submission_uncertain)
        finally:
            release.set()

    async def test_explicit_github_rejections_release_rate_reservation(self):
        for status in (403, 422):
            with self.subTest(status=status):
                service = GitHubService.__new__(GitHubService)
                service.config = MagicMock(spec=Config)
                service.config.DIRECT_RULE_FILE = "rule.list"
                service.config.GITHUB_REPO = "test/repo"
                service.config.GITHUB_BRANCH = "master"
                service.config.GITHUB_COMMIT_NAME = "bot"
                service.config.GITHUB_COMMIT_EMAIL = "bot@test.com"
                service._write_lock = asyncio.Lock()
                service.repo = MagicMock()
                service.repo.update_file.side_effect = GithubException(
                    status, {"message": "request rejected"}
                )
                service.get_rule_file_data = AsyncMock(
                    return_value={
                        "content": "# 以下域名待提交 PR\n",
                        "sha": "old-sha",
                    }
                )
                manager = HandlerManager.__new__(HandlerManager)
                manager.MAX_ADDS_PER_HOUR = 1
                manager.user_add_history = defaultdict(list)
                manager._last_history_cleanup = 0
                manager.github_service = service

                result = await manager._add_domain_with_limit(
                    123,
                    "example.com",
                    "user",
                )

                self.assertFalse(result["success"])
                self.assertNotIn("submission_uncertain", result)
                self.assertNotIn(123, manager.user_add_history)

if __name__ == '__main__':
    unittest.main()
