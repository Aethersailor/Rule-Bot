"""Bounded sharing of in-flight, side-effect-free async work."""

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Hashable, TypeVar

from .metrics import METRICS


T = TypeVar("T")


class InFlightLimitReached(RuntimeError):
    pass


@dataclass
class _Flight:
    task: asyncio.Task
    waiters: int = 0


class SingleFlight:
    def __init__(self, name: str, max_keys: int = 1024):
        self.name = name
        self.max_keys = max_keys
        self._flights: dict[Hashable, _Flight] = {}
        self._running: set[asyncio.Task] = set()

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        flight = self._flights.get(key)
        if flight is None or flight.task.done():
            if len(self._running) >= self.max_keys:
                raise InFlightLimitReached("in-flight work limit reached")
            flight = _Flight(asyncio.create_task(factory()))
            self._flights[key] = flight
            self._running.add(flight.task)

            def finished(task):
                self._running.discard(task)
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                # Retrieve errors even when every caller disconnected.
                if not task.cancelled():
                    task.exception()

            flight.task.add_done_callback(finished)
            METRICS.inc(self.name + ".started")
        else:
            METRICS.inc(self.name + ".shared")
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                flight.task.cancel()

    async def close(self):
        tasks = list(self._running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._flights.clear()
        self._running.clear()
