"""
Simple bounded TTL cache utilities.
"""

from __future__ import annotations

from collections import OrderedDict
from time import monotonic
from typing import Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    def __init__(self, maxsize: int, ttl_seconds: float):
        self.maxsize = max(0, int(maxsize))
        self.ttl_seconds = float(ttl_seconds)
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._next_cleanup = 0.0

    def get(self, key: K) -> Optional[V]:
        if self.maxsize <= 0:
            return None
        now = monotonic()
        item = self._data.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at <= now:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        if self.maxsize <= 0 or self.ttl_seconds <= 0:
            return
        now = monotonic()
        expires_at = now + self.ttl_seconds
        self._data[key] = (expires_at, value)
        self._data.move_to_end(key)
        self._evict(now)

    def pop(self, key: K) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
        self._next_cleanup = 0.0

    def __len__(self) -> int:
        return len(self._data)

    def _evict(self, now: float) -> None:
        # Expiration is also enforced lazily by get(). A full expiration sweep
        # on every write makes a hot cache O(maxsize) per insertion, so sweep at
        # a bounded interval and keep the hard LRU size limit O(1).
        if self._data and now >= self._next_cleanup:
            expired_keys = [k for k, (exp, _) in self._data.items() if exp <= now]
            for key in expired_keys:
                self._data.pop(key, None)
            cleanup_interval = max(1.0, min(60.0, self.ttl_seconds / 2.0))
            self._next_cleanup = now + cleanup_interval
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
