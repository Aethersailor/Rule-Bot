import os
import sys
import time
import unittest

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.utils.cache import TTLCache
from src.utils.metrics import Histogram, MetricsStore


class TestTTLCache(unittest.TestCase):
    def test_ttl_cache_eviction(self):
        cache = TTLCache(maxsize=2, ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)

    def test_ttl_cache_expire(self):
        cache = TTLCache(maxsize=2, ttl_seconds=0.01)
        cache.set("a", 1)
        time.sleep(0.05)
        self.assertIsNone(cache.get("a"))

    def test_ttl_cache_keeps_hard_bound_under_many_writes(self):
        cache = TTLCache(maxsize=64, ttl_seconds=60)
        for value in range(10_000):
            cache.set(value, value)

        self.assertEqual(len(cache), 64)
        self.assertIsNone(cache.get(0))
        self.assertEqual(cache.get(9_999), 9_999)

    def test_non_positive_ttl_does_not_retain_values(self):
        cache = TTLCache(maxsize=2, ttl_seconds=0)
        cache.set("a", 1)

        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.get("a"))


class TestMetricsStore(unittest.TestCase):
    def test_histogram_exact_boundary_stays_in_inclusive_bucket(self):
        histogram = Histogram((5, 10))

        histogram.observe(5)
        histogram.observe(10)

        self.assertEqual(histogram.snapshot().counts, (1, 1, 0))

    def test_metrics_snapshot(self):
        metrics = MetricsStore(enabled=True)
        metrics.inc("counter.test")
        metrics.observe("latency.test", 12.5)
        metrics.record_request("req.test", 5.0, success=True)

        snap = metrics.snapshot()
        self.assertIn("counters", snap)
        self.assertIn("histograms", snap)
        self.assertGreaterEqual(snap["counters"].get("counter.test", 0), 1)
        self.assertIn("req.test.count", snap["counters"])


if __name__ == '__main__':
    unittest.main()
