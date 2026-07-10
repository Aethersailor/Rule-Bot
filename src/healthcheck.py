"""Docker health check backed by an event-loop heartbeat."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


HEALTH_PATH = Path(os.getenv("RULE_BOT_HEALTH_PATH", "/tmp/rule-bot-health"))
MAX_HEARTBEAT_AGE = int(os.getenv("RULE_BOT_HEALTH_MAX_AGE", "90"))


def is_healthy(now: float | None = None) -> bool:
    try:
        age = (now if now is not None else time.time()) - HEALTH_PATH.stat().st_mtime
        return 0 <= age <= MAX_HEARTBEAT_AGE
    except OSError:
        return False


def main() -> int:
    return 0 if is_healthy() else 1


if __name__ == "__main__":
    sys.exit(main())
