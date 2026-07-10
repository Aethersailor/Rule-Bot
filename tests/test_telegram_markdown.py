import os
import sys
import unittest
import ast
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


class TestTelegramMarkdown(unittest.TestCase):
    def test_markdown_message_sources_do_not_use_double_asterisks(self):
        repo_root = Path(__file__).resolve().parent.parent
        files = [
            repo_root / "src" / "handlers" / "handler_manager.py",
            repo_root / "src" / "handlers" / "group_handler.py",
            repo_root / "src" / "services" / "group_service.py",
        ]

        for path in files:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content)
            string_literals = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            for literal in string_literals:
                self.assertNotIn(
                    "**",
                    literal,
                    f"{path.name} 仍包含 Telegram Markdown 不兼容的双星号粗体标记",
                )


if __name__ == "__main__":
    unittest.main()
