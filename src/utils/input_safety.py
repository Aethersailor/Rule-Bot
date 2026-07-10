"""Validation helpers for text that is persisted outside Telegram."""

from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"[ \t]+")


def contains_line_or_control_characters(value: str) -> bool:
    """Return whether text can escape a single-line comment or log field."""
    return any(char in "\r\n" or unicodedata.category(char) == "Cc" for char in value)


def validate_single_line_text(value: str, max_length: int) -> str:
    """Validate user supplied single-line text and return its trimmed form."""
    normalized = (value or "").strip()
    if contains_line_or_control_characters(normalized):
        raise ValueError("内容不能包含换行符或控制字符")
    if len(normalized) > max_length:
        raise ValueError(f"内容不能超过 {max_length} 个字符")
    return normalized


def sanitize_identity(value: str, fallback: str = "Telegram user", max_length: int = 64) -> str:
    """Collapse untrusted display names into a safe, single-line identity."""
    cleaned = "".join(" " if char in "\r\n" or unicodedata.category(char) == "Cc" else char for char in value or "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return (cleaned or fallback)[:max_length]
