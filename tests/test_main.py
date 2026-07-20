import asyncio
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


class TestMemoryMonitor(unittest.IsolatedAsyncioTestCase):
    async def test_memory_monitor_stops_with_event(self):
        stop_event = asyncio.Event()
        with patch.object(main, "log_memory_usage") as log_memory_usage:
            task = asyncio.create_task(main._memory_monitor(stop_event, interval=0.01))
            await asyncio.sleep(0.04)
            stop_event.set()
            await asyncio.wait_for(task, timeout=0.2)

        self.assertGreaterEqual(log_memory_usage.call_count, 1)


if __name__ == "__main__":
    unittest.main()
