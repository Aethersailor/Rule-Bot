import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src import main


class TestMain(unittest.TestCase):
    def test_set_memory_limit_skips_when_resource_module_unavailable(self):
        with patch.object(main, "resource", None):
            main.set_memory_limit()


if __name__ == "__main__":
    unittest.main()
