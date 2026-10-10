import asyncio
import unittest

from src.utils.singleflight import SingleFlight, InFlightLimitReached


class TestSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_shared_work_is_not_cancelled_by_one_caller(self):
        group = SingleFlight("test")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def work():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return 42

        callers = [asyncio.create_task(group.run("key", work)) for _ in range(100)]
        await entered.wait()
        callers[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await callers[0]
        release.set()
        self.assertEqual(await asyncio.gather(*callers[1:]), [42] * 99)
        self.assertEqual(calls, 1)
        self.assertFalse(group._flights)

    async def test_all_callers_cancelled_reclaims_work_and_key(self):
        group = SingleFlight("test", 1)
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def work():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        first = asyncio.create_task(group.run("key", work))
        await entered.wait()
        with self.assertRaises(InFlightLimitReached):
            await group.run("other", work)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertFalse(group._flights)
        await group.close()

    async def test_failed_work_can_be_retried(self):
        group = SingleFlight("test")
        calls = 0

        async def fail():
            nonlocal calls
            calls += 1
            await asyncio.sleep(.01)
            raise ValueError("upstream failed")

        results = await asyncio.gather(*(group.run("key", fail) for _ in range(20)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, ValueError) for result in results))
        self.assertEqual(calls, 1)
        with self.assertRaises(ValueError):
            await group.run("key", fail)
        self.assertEqual(calls, 2)
