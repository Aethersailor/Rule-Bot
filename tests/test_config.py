import os
import unittest
from unittest.mock import patch

from src.config import Config


class TestConfig(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
