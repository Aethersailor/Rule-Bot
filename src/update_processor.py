"""Bounded Telegram update concurrency with per-user ordering."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable

from telegram.ext import BaseUpdateProcessor


class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Process different users concurrently while preserving each user's order."""

    def __init__(self, max_concurrent_updates: int = 8, lock_ttl: int = 3600):
        # BaseUpdateProcessor acquires its semaphore before per-key logic. Keep
        # that outer queue roomy so a burst from one user cannot occupy every
        # slot while waiting on the same lock; the inner semaphore is the real
        # active-work bound.
        super().__init__(max(256, max_concurrent_updates * 32))
        self._work_semaphore = asyncio.Semaphore(max_concurrent_updates)
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._last_used: dict[tuple[str, int], float] = {}
        self._lock_ttl = max(60, lock_ttl)
        self._last_cleanup = 0.0

    @staticmethod
    def _key(update: object) -> tuple[str, int]:
        user = getattr(update, "effective_user", None)
        if user and getattr(user, "id", None) is not None:
            return ("user", int(user.id))
        chat = getattr(update, "effective_chat", None)
        if chat and getattr(chat, "id", None) is not None:
            return ("chat", int(chat.id))
        return ("update", id(update))

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 600:
            return
        cutoff = now - self._lock_ttl
        for key, used_at in list(self._last_used.items()):
            lock = self._locks.get(key)
            if used_at < cutoff and lock is not None and not lock.locked():
                self._last_used.pop(key, None)
                self._locks.pop(key, None)
        self._last_cleanup = now

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        now = time.monotonic()
        self._cleanup(now)
        key = self._key(update)
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._last_used[key] = now
        started = False
        try:
            async with lock:
                async with self._work_semaphore:
                    started = True
                    _ = await coroutine
        finally:
            if not started:
                close = getattr(coroutine, "close", None)
                if close:
                    close()
            self._last_used[key] = time.monotonic()

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        self._locks.clear()
        self._last_used.clear()
