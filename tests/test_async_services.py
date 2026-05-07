import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import asyncio
import base64

# Add repo root to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.services.dns_service import DNSService
from src.services.github_service import GitHubService
from src.config import Config

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

if __name__ == '__main__':
    unittest.main()
