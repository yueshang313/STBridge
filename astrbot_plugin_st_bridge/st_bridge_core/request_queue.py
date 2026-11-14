"""Global FIFO queue that lets concurrent QQ messages take turns on the single ST chat."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable


QUEUE_POLL_INTERVAL_SECONDS = 0.1


class QueueFullError(Exception):
    """Raised when the configured number of waiting entries is reached."""


class QueueTimeoutError(Exception):
    """Raised when an entry waited longer than the configured limit."""


class QueueUnavailableError(Exception):
    """Raised when the bridge became unavailable or the queue was closed."""


@dataclass(eq=False)
class QueueEntry:
    """One waiting conversation turn; texts grow while the same user keeps sending."""

    speaker_key: str | None
    texts: list[str]
    enqueued_at: float
    # True when someone was being served or waiting ahead at enqueue time.
    waited: bool
    # True once the entry could not be dispatched on its first attempt.
    blocked: bool = False
    dispatched_at: float | None = None
    # A proactive (scheduled / chat) turn: it gives way to people and is not counted as one waiting.
    proactive: bool = False


class BridgeQueue:
    """Serve entries strictly in arrival order, one dispatched entry at a time.

    An entry leaves the waiting list only when ``dispatch`` succeeds; the
    dispatched entry keeps the slot until ``release`` so that its reply is
    delivered before the next entry starts. A slot held longer than
    ``active_hold_limit`` is freed by the next waiter, because AstrBot may never
    resume (and so never release) a handler whose event was stopped.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = QUEUE_POLL_INTERVAL_SECONDS,
        # Seconds, or a function returning them (read at every check, so a console change applies at once).
        active_hold_limit: float | Callable[[], float] | None = None,
        on_force_release: Callable[[QueueEntry, float], None] | None = None,
    ) -> None:
        self._clock = clock
        self._poll_interval = poll_interval
        self._active_hold_limit = active_hold_limit
        self._on_force_release = on_force_release
        self._waiting: list[QueueEntry] = []
        self._active: QueueEntry | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    @property
    def busy(self) -> bool:
        """Is a turn being served or waiting (the single ST chat is in use)?

        A slot held past the hold limit is stuck, not in use: with nobody waiting it does not count (read-only;
        the next entry's ``wait_turn`` frees the slot).
        """
        if self._waiting:
            return True
        return self._active is not None and self._held_too_long() is None

    @property
    def user_waiting(self) -> bool:
        """Is any waiting entry a person's (not a proactive one)?"""
        return any(not entry.proactive for entry in self._waiting)

    def waiting_entry(self, speaker_key: str | None) -> QueueEntry | None:
        """Return the not-yet-dispatched entry of this speaker, if any."""
        if speaker_key is None:
            return None
        return next(
            (entry for entry in self._waiting if entry.speaker_key == speaker_key),
            None,
        )

    def enqueue(self, speaker_key: str | None, text: str, *, max_waiting: int, proactive: bool = False) -> QueueEntry:
        if self._closed:
            raise QueueUnavailableError("queue is closed")
        if len(self._waiting) >= max_waiting:
            raise QueueFullError("too many waiting entries")
        entry = QueueEntry(
            speaker_key=speaker_key,
            texts=[text],
            enqueued_at=self._clock(),
            waited=self._active is not None or bool(self._waiting),
            proactive=proactive,
        )
        self._waiting.append(entry)
        return entry

    def position(self, entry: QueueEntry) -> int:
        """1-based position among waiting entries; 0 once dispatched or removed."""
        try:
            return self._waiting.index(entry) + 1
        except ValueError:
            return 0

    def waited_seconds(self, entry: QueueEntry) -> float:
        end = entry.dispatched_at if entry.dispatched_at is not None else self._clock()
        return max(0.0, end - entry.enqueued_at)

    async def wait_turn(
        self,
        entry: QueueEntry,
        *,
        max_wait_seconds: float,
        dispatch: Callable[[QueueEntry], Any],
        available: Callable[[], bool],
    ) -> Any:
        """Wait until ``entry`` is first and the slot is free, then dispatch it.

        ``dispatch`` returns None while the bridge is still busy (the entry keeps
        waiting at the head) or a result, which is returned. Any exception,
        including cancellation, removes the entry from the queue.
        """
        try:
            while True:
                if self._closed or not available():
                    raise QueueUnavailableError("bridge unavailable")
                if self._clock() - entry.enqueued_at > max_wait_seconds:
                    raise QueueTimeoutError("waited too long")
                self._free_stale_slot()
                if self._active is None and self._waiting and self._waiting[0] is entry:
                    result = dispatch(entry)
                    if result is not None:
                        self._waiting.remove(entry)
                        entry.dispatched_at = self._clock()
                        self._active = entry
                        return result
                entry.blocked = True
                await asyncio.sleep(self._poll_interval)
        except BaseException:
            self._discard(entry)
            raise

    def release(self, entry: QueueEntry) -> None:
        """Free the slot after the dispatched entry's reply has been handled."""
        # A force-released entry must not clear the slot of a newer entry.
        if self._active is entry:
            self._active = None
        self._discard(entry)

    def _held_too_long(self) -> float | None:
        """Seconds the active slot has been held when that is past the limit (a stuck slot), else None. No side effects."""
        active = self._active
        limit = self._active_hold_limit
        if callable(limit):
            try:
                limit = float(limit())
            except Exception:
                limit = None
        if active is None or limit is None or active.dispatched_at is None:
            return None
        held = self._clock() - active.dispatched_at
        return held if held > limit else None

    def _free_stale_slot(self) -> None:
        active = self._active
        held = self._held_too_long()
        if held is None:
            return
        self._active = None
        if self._on_force_release is not None:
            try:
                self._on_force_release(active, held)
            except Exception:
                # Logging must never keep the queue stuck.
                pass

    def _discard(self, entry: QueueEntry) -> None:
        if entry in self._waiting:
            self._waiting.remove(entry)

    def close(self) -> None:
        """Make every waiter finish on its next poll; used when the plugin stops."""
        self._closed = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self._active is not None,
            "waiting": [
                {
                    "speaker_key": entry.speaker_key,
                    "waited_seconds": round(self.waited_seconds(entry), 1),
                    "messages": len(entry.texts),
                }
                for entry in self._waiting
            ],
        }
