import os
import sys
import unittest
from types import SimpleNamespace

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.services.group_service import GroupService


class TestGroupService(unittest.TestCase):
    def test_join_group_message_escapes_markdown_sensitive_content(self):
        config = SimpleNamespace(
            GROUP_CHECK_ENABLED=True,
            REQUIRED_GROUP_NAME="group_name[test]",
            REQUIRED_GROUP_LINK="https://t.me/group_name(test)",
        )
        service = GroupService(config, bot=None)

        message = service.get_join_group_message()

        self.assertIn("group\\_name\\[test\\]", message)
        self.assertIn("https://t.me/group\\_name\\(test\\)", message)


if __name__ == "__main__":
    unittest.main()
