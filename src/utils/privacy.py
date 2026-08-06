"""Privacy-preserving helpers for operational logs."""

import hashlib
import hmac
import secrets


_LOG_REFERENCE_KEY = secrets.token_bytes(32)


def log_reference(value: str) -> str:
    """Return a process-local, non-reversible reference for sensitive text."""
    normalized = (value or "").strip().lower().encode("utf-8", errors="replace")
    return hmac.new(_LOG_REFERENCE_KEY, normalized, hashlib.sha256).hexdigest()[:12]
