import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src import main


class TestMain(unittest.TestCase):
    def test_set_memory_limit_skips_when_resource_module_unavailable(self):
        with patch.object(main, "resource", None):
            main.set_memory_limit()

    def test_large_rss_is_warned_instead_of_discarded_as_invalid(self):
        main.log_memory_usage._initialized = True
        main.log_memory_usage.last_warning_time = 0
        main.log_memory_usage.last_warning_level = 0
        main.log_memory_usage.last_normal_log = 0
        process = SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=1500 * 1024 * 1024)
        )

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(main.psutil, "Process", return_value=process):
                with patch.object(main.logger, "warning") as warning:
                    main.log_memory_usage()

        messages = [str(call.args[0]) for call in warning.call_args_list]
        self.assertTrue(any("内存使用较高" in message for message in messages))
        self.assertFalse(any("内存值异常" in message for message in messages))


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
