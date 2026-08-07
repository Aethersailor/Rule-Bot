import os
import unittest
from unittest.mock import patch

from src.config import Config


class TestConfig(unittest.TestCase):
    @staticmethod
    def _base_env():
        return {
            "TELEGRAM_BOT_TOKEN": "telegram-token",
            "GITHUB_TOKEN": "github-token",
            "GITHUB_REPO": "example/repo",
            "DIRECT_RULE_FILE": "rules.list",
        }

    def test_announcement_group_id_is_optional_and_parsed(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-token",
            "GITHUB_TOKEN": "github-token",
            "GITHUB_REPO": "example/repo",
            "DIRECT_RULE_FILE": "rules.list",
            "ANNOUNCEMENT_GROUP_ID": "-1001234567890",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config()

        self.assertEqual(config.ANNOUNCEMENT_GROUP_ID, -1001234567890)

    def test_rule_bot_client_apis_are_disabled_by_default(self):
        with patch.dict(os.environ, self._base_env(), clear=True):
            config = Config()

        self.assertFalse(config.RULE_BOT_CLIENT_PRIVATE_API_ENABLED)
        self.assertFalse(config.RULE_BOT_CLIENT_COMMUNITY_API_ENABLED)
        self.assertEqual(config.RULE_BOT_CLIENT_PRIVATE_API_PORT, 8765)
        self.assertEqual(config.RULE_BOT_CLIENT_COMMUNITY_API_PORT, 7654)

    def test_private_api_requires_hidden_path_and_strong_token(self):
        env = {
            **self._base_env(),
            "RULE_BOT_CLIENT_PRIVATE_API_ENABLED": "true",
            "RULE_BOT_CLIENT_PRIVATE_API_PATH": "/api/hidden-private-path",
            "RULE_BOT_CLIENT_PRIVATE_API_TOKEN": "t" * 32,
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config()

        self.assertTrue(config.RULE_BOT_CLIENT_PRIVATE_API_ENABLED)

    def test_community_api_requires_group_verification(self):
        env = {
            **self._base_env(),
            "RULE_BOT_CLIENT_COMMUNITY_API_ENABLED": "true",
            "RULE_BOT_CLIENT_COMMUNITY_API_PATH": "/api/hidden-public-path",
            "RULE_BOT_CLIENT_COMMUNITY_API_BASE_URL": "https://rule-bot.example",
            "RULE_BOT_CLIENT_COMMUNITY_TOKEN_SIGNING_KEY": "s" * 32,
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "group membership"):
                Config()

    def test_secret_file_is_supported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            secret_file = os.path.join(temp_dir, "token")
            with open(secret_file, "w", encoding="utf-8") as handle:
                handle.write("f" * 32)
            env = {
                **self._base_env(),
                "RULE_BOT_CLIENT_PRIVATE_API_ENABLED": "true",
                "RULE_BOT_CLIENT_PRIVATE_API_PATH": "/api/hidden-private-path",
                "RULE_BOT_CLIENT_PRIVATE_API_TOKEN_FILE": secret_file,
            }
            with patch.dict(os.environ, env, clear=True):
                config = Config()

        self.assertEqual(config.RULE_BOT_CLIENT_PRIVATE_API_TOKEN, "f" * 32)


if __name__ == "__main__":
    unittest.main()
