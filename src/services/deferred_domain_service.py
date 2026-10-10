"""Durable DNS deferrals shared by old and new Rule-Bot clients."""

import asyncio
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from loguru import logger

from ..utils.privacy import log_reference


class DeferredDomainService:
    MAX_PENDING = 10000
    INITIAL_DELAY = 300
    MAX_DELAY = 21600

    def __init__(self, path: Path, handler_manager):
        self.path = Path(path)
        self.handler_manager = handler_manager
        self._task = None
        self._wake = asyncio.Event()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS deferred_domains (
                    domain TEXT NOT NULL,
                    source TEXT NOT NULL,
                    next_attempt REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (domain, source)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS deferred_due ON deferred_domains(next_attempt)"
            )
            self._pending_keys = {tuple(row) for row in connection.execute("SELECT domain, source FROM deferred_domains")}
        self.path.chmod(0o600)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    async def enqueue(self, domain: str, source: str) -> bool:
        accepted = await asyncio.to_thread(self._enqueue, domain, source)
        if accepted:
            self._pending_keys.add((domain, source))
            self._wake.set()
        return accepted

    async def contains(self, domain: str, source: str) -> bool:
        # Ordinary submissions are not deferred: keep them off the executor and
        # disk. Positive hits still verify the durable row before acknowledging.
        key = (domain, source)
        if key not in self._pending_keys:
            return False
        def lookup():
            with closing(self._connect()) as connection:
                return connection.execute(
                    "SELECT 1 FROM deferred_domains WHERE domain=? AND source=?",
                    (domain, source),
                ).fetchone() is not None
        found = await asyncio.to_thread(lookup)
        if not found:
            self._pending_keys.discard(key)
        return found

    def _enqueue(self, domain, source):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM deferred_domains WHERE domain=? AND source=?",
                (domain, source),
            ).fetchone():
                return True
            if connection.execute("SELECT COUNT(*) FROM deferred_domains").fetchone()[0] >= self.MAX_PENDING:
                return False
            connection.execute(
                "INSERT INTO deferred_domains(domain, source, next_attempt) VALUES (?, ?, ?)",
                (domain, source, time.time() + self.INITIAL_DELAY),
            )
        return True

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="deferred-domains")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _next(self):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM deferred_domains ORDER BY next_attempt LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def _finish(self, row, terminal):
        with closing(self._connect()) as connection, connection:
            if terminal:
                connection.execute(
                    "DELETE FROM deferred_domains WHERE domain=? AND source=?",
                    (row["domain"], row["source"]),
                )
            else:
                delay = min(self.INITIAL_DELAY * 2 ** min(row["attempts"] + 1, 7), self.MAX_DELAY)
                connection.execute(
                    "UPDATE deferred_domains SET attempts=attempts+1, next_attempt=? WHERE domain=? AND source=?",
                    (time.time() + delay, row["domain"], row["source"]),
                )

    async def process(self, row):
        config = self.handler_manager.config
        limit_name = (
            "RULE_BOT_CLIENT_PRIVATE_API_RATE_LIMIT_PER_HOUR"
            if row["source"] == "rule_bot_client_private"
            else "RULE_BOT_CLIENT_COMMUNITY_API_RATE_LIMIT_PER_HOUR"
        )
        result = await asyncio.wait_for(
            self.handler_manager.check_and_add_domain_auto(
                row["domain"], "Rule-Bot Client",
                user_id=("deferred", row["source"]), source=row["source"],
                max_adds=getattr(config, limit_name),
            ), timeout=90,
        )
        terminal = result.get("action") in ("added", "exists", "rejected") or result.get("error_code") in ("nxdomain", "empty_dns")
        await asyncio.to_thread(self._finish, row, terminal)
        if terminal:
            self._pending_keys.discard((row["domain"], row["source"]))
        logger.info(
            "后台域名重试完成，domain_ref={}，terminal={}，action={}",
            log_reference(row["domain"]), terminal, result.get("action", "error"),
        )

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                row = await asyncio.to_thread(self._next)
                if row and row["next_attempt"] <= time.time():
                    try:
                        await self.process(row)
                    except Exception as error:
                        logger.warning("后台域名重试失败，error_type={}", type(error).__name__)
                        await asyncio.to_thread(self._finish, row, False)
                    continue
                wait = max(0.01, row["next_attempt"] - time.time()) if row else self.MAX_DELAY
            except Exception as error:
                logger.warning("后台重试队列读取失败，error_type={}", type(error).__name__)
                wait = 60
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except TimeoutError:
                pass
