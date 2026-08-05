"""
Lightweight in-process metrics with optional periodic export.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from loguru import logger


DEFAULT_LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class HistogramSnapshot:
    buckets: Iterable[int]
    counts: Iterable[int]
    count: int
    total: float


class Histogram:
    def __init__(self, buckets_ms: Iterable[int] = DEFAULT_LATENCY_BUCKETS_MS):
        self.buckets = tuple(sorted(buckets_ms))
        self.counts = [0] * (len(self.buckets) + 1)
        self.count = 0
        self.total = 0.0

    def observe(self, value_ms: float) -> None:
        idx = bisect_left(self.buckets, value_ms)
        self.counts[idx] += 1
        self.count += 1
        self.total += value_ms

    def snapshot(self) -> HistogramSnapshot:
        return HistogramSnapshot(
            buckets=self.buckets,
            counts=tuple(self.counts),
            count=self.count,
            total=self.total,
        )


class MetricsStore:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._start_time = time.monotonic()

    def inc(self, name: str, value: int = 1) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def observe(self, name: str, value_ms: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            hist = self._histograms.get(name)
            if not hist:
                hist = Histogram()
                self._histograms[name] = hist
            hist.observe(value_ms)

    def record_request(self, name: str, duration_ms: float, success: bool = True) -> None:
        if not self.enabled:
            return
        status = "ok" if success else "fail"
        self.inc(f"{name}.count")
        self.inc(f"{name}.{status}")
        self.observe(f"{name}.latency_ms", duration_ms)

    def snapshot(self, reset: bool = False) -> Dict[str, object]:
        if not self.enabled:
            return {}
        with self._lock:
            counters = dict(self._counters)
            histograms = {
                key: hist.snapshot().__dict__ for key, hist in self._histograms.items()
            }
            uptime = time.monotonic() - self._start_time
            if reset:
                self._counters.clear()
                self._histograms.clear()
        return {
            "counters": counters,
            "histograms": histograms,
            "uptime_s": round(uptime, 3),
        }


def _atomic_write_json(path: Path, data: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


class MetricsExporter:
    def __init__(self, metrics: MetricsStore, path: Path, interval: int, reset: bool = False):
        self.metrics = metrics
        self.path = path
        self.interval = max(1, int(interval))
        self.reset = reset
        self._task: Optional[asyncio.Task] = None

    def start(self) -> Optional[asyncio.Task]:
        if not self.metrics.enabled:
            return None
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # stop() 主动取消任务，CancelledError 属于预期。
                logger.debug("metrics 导出任务在停止时被取消")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            snapshot = self.metrics.snapshot(reset=self.reset)
            if not snapshot:
                continue
            snapshot["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            snapshot["asyncio_task_count"] = len(asyncio.all_tasks())
            try:
                _atomic_write_json(self.path, snapshot)
            except Exception as e:
                logger.debug(f"写入 metrics 失败: {e}")


METRICS_ENABLED = _env_bool("METRICS_ENABLED", False)
METRICS_EXPORT_PATH = Path(os.getenv("METRICS_EXPORT_PATH", "/tmp/rule-bot-metrics.json"))
METRICS_EXPORT_INTERVAL = _env_int("METRICS_EXPORT_INTERVAL", 30)
METRICS_RESET_ON_EXPORT = _env_bool("METRICS_RESET_ON_EXPORT", False)

METRICS = MetricsStore(enabled=METRICS_ENABLED)
EXPORTER = MetricsExporter(
    METRICS, METRICS_EXPORT_PATH, METRICS_EXPORT_INTERVAL, METRICS_RESET_ON_EXPORT
)
