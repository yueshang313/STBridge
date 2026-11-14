"""Claiming the owner by a one-time code: the console shows 6 digits, the user sends them to the bot in a private chat.

Pure logic with no platform dependency. The code lives in memory only: it works once, expires after
``CLAIM_TTL_SECONDS`` and is gone when the core restarts; asking for a new one voids the old one.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

CLAIM_TTL_SECONDS = 300
CODE_DIGITS = 6
_CODE_RE = re.compile(rf"\d{{{CODE_DIGITS}}}")

IDLE = "idle"
PENDING = "pending"
CLAIMED = "claimed"
EXPIRED = "expired"
FAILED = "failed"


def mask_id(value: str) -> str:
    """An id with its middle hidden: the first 2 and last 2 characters stay (shorter ids show 1 + 1)."""
    value = value.strip()
    if len(value) <= 2:
        return "*" * len(value)
    keep = 2 if len(value) >= 8 else 1
    return f"{value[:keep]}{'*' * min(len(value) - 2 * keep, 6)}{value[-keep:]}"


def claim_candidate(text: str | None, prefixes: list[str] | None) -> str | None:
    """The 6 digits when the whole message (minus blanks, a leading "/" and a wake word / nickname) is exactly that."""
    if not isinstance(text, str):
        return None
    rest = text.strip()
    wake = sorted({item for item in (prefixes or []) if isinstance(item, str) and item.strip()}, key=len, reverse=True)
    for _ in range(3):  # a "/" and a wake word may both be there ("/琴音 123456")
        for prefix in wake:
            if rest.startswith(prefix):
                rest = rest[len(prefix):].lstrip()
                break
        else:
            if not rest.startswith("/"):
                break
            rest = rest[1:].lstrip()
    return rest if _CODE_RE.fullmatch(rest) and rest.isascii() else None


class OwnerClaim:
    """The one pending claim code and what became of the last claim."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._code: str | None = None
        self._deadline = 0.0
        self._result: dict[str, Any] | None = None  # {"state": CLAIMED | EXPIRED | FAILED, ...}

    def start(self) -> dict[str, Any]:
        """Make a new code (the old one stops working)."""
        self._code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        self._deadline = self._clock() + CLAIM_TTL_SECONDS
        self._result = None
        return {"code": self._code, "expires_in": CLAIM_TTL_SECONDS}

    def _expire_if_due(self) -> None:
        if self._code is not None and self._clock() >= self._deadline:
            self._code = None
            self._result = {"state": EXPIRED}

    def active(self) -> bool:
        self._expire_if_due()
        return self._code is not None

    def consume(self, candidate: str | None) -> bool:
        """True (once) when ``candidate`` is the live code; the code is spent right away."""
        self._expire_if_due()
        if self._code is None or candidate is None:
            return False
        if not hmac.compare_digest(candidate.encode("ascii"), self._code.encode("ascii")):
            return False
        self._code = None
        return True

    def finish(self, state: str, **details: Any) -> None:
        """Record how the claim ended (claimed / failed) for the console to see."""
        self._code = None
        self._result = {"state": state, **details}

    def status(self) -> dict[str, Any]:
        self._expire_if_due()
        if self._code is not None:
            return {"state": PENDING, "code": self._code, "expires_in": max(0, int(self._deadline - self._clock() + 0.999))}
        return dict(self._result) if self._result is not None else {"state": IDLE}
