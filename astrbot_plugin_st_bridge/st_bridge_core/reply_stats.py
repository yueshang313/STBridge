"""How the last requests to ST ended (H1): the console health chain shows whether the model still answers.

Only requests that reached ST count (an owner or group chat turn, a proactive chat message, a scheduled plan).
A request that never got to ST (ST not connected, queue full or timed out, protection on) says nothing about the
model, so it is not recorded here; the ST node and the queue already show those. Memory only: a restart empties it.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

MAX_RECORDS = 20
AVERAGE_OVER = 10
OK = "ok"
# What counts as "ST got the request but gave no usable reply".
FAILURE_CODES = ("StFailed", "Timeout", "EmptyReply", "InvalidReply")


class ReplyStats:
    """The last ``MAX_RECORDS`` results: ``(finished at, code, st_ms)``; ``code`` is "ok" or a failure code."""

    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self._clock = clock
        self._records: deque[tuple[datetime, str, int]] = deque(maxlen=MAX_RECORDS)
        self._lock = threading.Lock()

    def record(self, code: str, st_ms: int = 0) -> None:
        if code != OK and code not in FAILURE_CODES:
            return
        try:
            milliseconds = max(0, int(st_ms))
        except (TypeError, ValueError, OverflowError):
            milliseconds = 0
        with self._lock:
            self._records.append((self._clock(), code, milliseconds))

    def snapshot(self) -> dict[str, Any]:
        """The ``replies`` field of GET /api/status. ``recent`` lists the codes oldest first."""
        with self._lock:
            records = list(self._records)
        now = self._clock()
        successes = [item for item in records if item[1] == OK]
        failures = [item for item in records if item[1] != OK]
        last_ok = successes[-1] if successes else None
        last_fail = failures[-1] if failures else None
        timed = [item[2] for item in records[-AVERAGE_OVER:] if item[2] > 0]

        def age(item: tuple[datetime, str, int] | None) -> int | None:
            return max(0, int((now - item[0]).total_seconds())) if item is not None else None

        return {
            "count": len(records),
            "ok": len(successes),
            "recent": [item[1] for item in records],
            "last_ok_at": last_ok[0].isoformat(timespec="seconds") if last_ok else None,
            "last_ok_age": age(last_ok),
            "last_fail_at": last_fail[0].isoformat(timespec="seconds") if last_fail else None,
            "last_fail_age": age(last_fail),
            "last_fail_code": last_fail[1] if last_fail else None,
            "avg_ms": int(sum(timed) / len(timed)) if timed else None,
        }
