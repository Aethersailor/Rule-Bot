"""MatchScope community token issuance and verification."""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Optional


TOKEN_VERSION = 1
TOKEN_SCOPE = "matchscope:submit"
PRIVACY_NOTICE_VERSION = 1


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class MatchScopeTokenService:
    """Issue self-contained HMAC tokens with server-side revocation state."""

    def __init__(self, database_path: Path, signing_key: str, ttl_days: int):
        self.database_path = Path(database_path)
        self.signing_key = signing_key.encode("utf-8")
        self.ttl_seconds = ttl_days * 86400
        self._lock = asyncio.Lock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS matchscope_tokens (
                    user_id INTEGER PRIMARY KEY,
                    subject TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_used_at INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS matchscope_privacy_consents (
                    user_id INTEGER PRIMARY KEY,
                    notice_version INTEGER NOT NULL,
                    consented_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                UPDATE matchscope_tokens
                SET last_used_at = NULL
                WHERE last_used_at IS NOT NULL
                """
            )
        self.database_path.chmod(0o600)

    async def has_current_consent(self, user_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._has_current_consent, user_id)

    def _has_current_consent(self, user_id: int) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT notice_version FROM matchscope_privacy_consents
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            return bool(
                row and int(row["notice_version"]) >= PRIVACY_NOTICE_VERSION
            )

    async def consent(self, user_id: int) -> int:
        now = int(time.time())
        async with self._lock:
            await asyncio.to_thread(self._consent, user_id, now)
        return now

    def _consent(self, user_id: int, now: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO matchscope_privacy_consents
                    (user_id, notice_version, consented_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notice_version = excluded.notice_version,
                    consented_at = excluded.consented_at
                """,
                (user_id, PRIVACY_NOTICE_VERSION, now),
            )
            connection.execute(
                "UPDATE matchscope_tokens SET last_used_at = NULL WHERE user_id = ?",
                (user_id,),
            )

    async def withdraw_consent(self, user_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._withdraw_consent, user_id)

    def _withdraw_consent(self, user_id: int) -> bool:
        with closing(self._connect()) as connection, connection:
            token_cursor = connection.execute(
                """
                UPDATE matchscope_tokens
                SET enabled = 0, last_used_at = NULL
                WHERE user_id = ?
                """,
                (user_id,),
            )
            consent_cursor = connection.execute(
                "DELETE FROM matchscope_privacy_consents WHERE user_id = ?",
                (user_id,),
            )
            return token_cursor.rowcount > 0 or consent_cursor.rowcount > 0

    async def issue(self, user_id: int) -> dict:
        if not await self.has_current_consent(user_id):
            raise PermissionError("current MatchScope privacy notice is not accepted")
        now = int(time.time())
        expires_at = now + self.ttl_seconds
        subject = secrets.token_urlsafe(12)
        async with self._lock:
            version = await asyncio.to_thread(
                self._upsert_issue, user_id, subject, now, expires_at
            )

        payload = {
            "exp": expires_at,
            "iat": now,
            "scope": TOKEN_SCOPE,
            "sub": subject,
            "v": TOKEN_VERSION,
            "ver": version,
        }
        encoded_payload = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(
                self.signing_key,
                f"rbt1.{encoded_payload}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return {
            "token": f"rbt1.{encoded_payload}.{signature}",
            "issued_at": now,
            "expires_at": expires_at,
            "version": version,
        }

    def _upsert_issue(
        self, user_id: int, subject: str, now: int, expires_at: int
    ) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT version FROM matchscope_tokens WHERE user_id = ?", (user_id,)
            ).fetchone()
            version = (int(row["version"]) + 1) if row else 1
            connection.execute(
                """
                INSERT INTO matchscope_tokens
                    (user_id, subject, version, enabled, issued_at, expires_at, last_used_at)
                VALUES (?, ?, ?, 1, ?, ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    subject = excluded.subject,
                    version = excluded.version,
                    enabled = 1,
                    issued_at = excluded.issued_at,
                    expires_at = excluded.expires_at,
                    last_used_at = NULL
                """,
                (user_id, subject, version, now, expires_at),
            )
            return version

    async def revoke(self, user_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._revoke, user_id)

    def _revoke(self, user_id: int) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE matchscope_tokens SET enabled = 0 WHERE user_id = ?",
                (user_id,),
            )
            return cursor.rowcount > 0

    async def verify(self, token: str) -> Optional[str]:
        payload = self._verify_signature_and_payload(token)
        if payload is None:
            return None

        async with self._lock:
            subject = await asyncio.to_thread(
                self._verify_state,
                payload["sub"],
                int(payload["ver"]),
                int(payload["exp"]),
            )
        return subject

    def _verify_signature_and_payload(self, token: str) -> Optional[dict]:
        try:
            prefix, encoded_payload, signature = token.split(".", 2)
            if prefix != "rbt1" or len(token) > 512:
                return None
            expected = _b64encode(
                hmac.new(
                    self.signing_key,
                    f"rbt1.{encoded_payload}".encode("ascii"),
                    hashlib.sha256,
                ).digest()
            )
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(_b64decode(encoded_payload))
            if (
                payload.get("v") != TOKEN_VERSION
                or payload.get("scope") != TOKEN_SCOPE
                or not isinstance(payload.get("sub"), str)
                or not 8 <= len(payload["sub"]) <= 64
                or not isinstance(payload.get("ver"), int)
                or not isinstance(payload.get("iat"), int)
                or not isinstance(payload.get("exp"), int)
                or payload["exp"] <= int(time.time())
                or payload["iat"] > int(time.time()) + 60
            ):
                return None
            return payload
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _verify_state(
        self, subject: str, version: int, expires_at: int
    ) -> Optional[str]:
        now = int(time.time())
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT t.version, t.enabled, t.expires_at, c.notice_version
                FROM matchscope_tokens AS t
                LEFT JOIN matchscope_privacy_consents AS c
                    ON c.user_id = t.user_id
                WHERE t.subject = ?
                """,
                (subject,),
            ).fetchone()
            if (
                row is None
                or not row["enabled"]
                or int(row["version"]) != version
                or int(row["expires_at"]) != expires_at
                or int(row["expires_at"]) <= now
                or row["notice_version"] is None
                or int(row["notice_version"]) < PRIVACY_NOTICE_VERSION
            ):
                return None
            return subject

    async def status(self, user_id: int) -> Optional[dict]:
        async with self._lock:
            return await asyncio.to_thread(self._status, user_id)

    def _status(self, user_id: int) -> Optional[dict]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT version, enabled, issued_at, expires_at
                FROM matchscope_tokens WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row else None
