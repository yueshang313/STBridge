"""Owner private-chat proactive messages (L1d, 3a): when the character speaks first after a silence.

The scheduler decides *when*; the runner the plugin provides sends nothing of its own accord. Time is the
host's local wall clock (naive ``datetime``); the clock and the randomness are injectable for tests.

Silence is counted from the owner private session's last real chat (the owner's message, the character's
finished reply, a proactive chat message that went out; a scheduled plan is not chat). After every activity a target is drawn in
``[silence_min, silence_max]`` minutes (multiplied by ``followup_factor ** unanswered``); reaching it makes
the message due, then the rules below decide whether it may go out now. A failed attempt is retried 30
minutes later without drawing a new target.
"""

from __future__ import annotations

import asyncio
import difflib
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from .proactive_chat_config import in_quiet_hours, next_window_start, proactive_chat_settings, quiet_hours_end, window_state
from .schedule_config import schedule_section
from .schedule_service import CHECK_INTERVAL_SECONDS, ProactiveState, ScheduleTargets


RETRY_MINUTES = 30
WINDOW_DELAY_MAX_MINUTES = 15
# Reasons that are worth a log line when they start (every other status is silent).
LOGGED_REASONS = (
    "UnansweredLimit",
    "DailyLimit",
    "Retry",
    "QuietHours",
    "OutsideWindow",
    "Busy",
    "Conversing",
    "Gap",
    "T1Priority",
)
_PUNCTUATION = re.compile(r"[\W_]+")

ChatRunner = Callable[[bool, int, int], Awaitable[Any]]


def normalize_text(text: str) -> str:
    """The text without whitespace and punctuation, for comparing two messages."""
    return _PUNCTUATION.sub("", text or "")


def is_duplicate(new_text: str, previous_text: str, ratio: float) -> bool:
    """Is the new message too like the previous one (similarity >= ratio; 1.0 = identical only)?"""
    first, second = normalize_text(new_text), normalize_text(previous_text)
    if not first or not second:
        return False
    if first == second:
        return True
    if ratio >= 1.0:
        return False
    return difflib.SequenceMatcher(None, first, second).ratio() >= ratio


def delivered(result: Any) -> bool:
    """Did a proactive turn's result mean that a message reached the chat?"""
    return isinstance(result, str) and (result == "sent" or result == "fallback" or result.startswith("sent:"))


class ProactiveChatScheduler:
    """Starts the owner chat message when its silence target is reached and every rule allows it."""

    def __init__(
        self,
        *,
        get_config: Callable[[], Any],
        targets: ScheduleTargets,
        state: ProactiveState,
        runner: ChatRunner,
        clock: Callable[[], datetime] = datetime.now,
        rng: Callable[[], float] = random.random,
        logger: Any = None,
        busy_reason: Callable[[str], str | None] | None = None,
        t1_blocking: Callable[[], bool] | None = None,
    ) -> None:
        self._get_config = get_config
        self._targets = targets
        self._state = state
        self._runner = runner
        self._clock = clock
        self._rng = rng
        self._logger = logger
        self._busy_reason = busy_reason
        self._t1_blocking = t1_blocking
        self._started = clock()
        owner = targets.owner_conversation
        self._seen_seq = targets.user_seq(owner) if owner else 0
        self._base: datetime | None = None
        self._due: datetime | None = None
        self._retry_at: datetime | None = None
        self._window_ready: datetime | None = None
        self._running = False
        self._logged: str | None = None
        self._task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None

    # -- lifecycle

    @property
    def running(self) -> bool:
        return self._running

    def start(self, interval: float = CHECK_INTERVAL_SECONDS) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        task, self._loop_task = self._loop_task, None
        pending = [item for item in (task, self._task) if item is not None]
        for item in pending:
            item.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _loop(self, interval: float) -> None:
        while True:
            try:
                self.tick()
            except Exception as error:
                self._log("error", f"[STBridge] ProactiveChat check failed ({type(error).__name__})")
            await asyncio.sleep(interval)

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            try:
                getattr(self._logger, level)(message)
            except Exception:
                pass

    # -- decision

    def _evaluate(self, now: datetime) -> dict[str, Any]:
        """Work out where things stand: ``{"reason": code, "next_at": datetime | None, ...}`` (reason "Ready" = send now)."""
        section = schedule_section(self._get_config())
        cfg = section["proactive_chat"]
        settings = section["settings"]
        conversation = self._targets.owner_conversation
        unanswered = self._state.unanswered
        view: dict[str, Any] = {"next_at": None, "conversation": conversation, "unanswered": unanswered}
        if not cfg["enabled"]:
            return {**view, "reason": "Disabled"}
        if conversation is None:
            return {**view, "reason": "NoOwner"}
        sequence = self._targets.user_seq(conversation)
        if sequence != self._seen_seq:
            # The owner spoke: whatever was left unanswered is answered now.
            self._seen_seq = sequence
            self._state.clear_unanswered()
            self._retry_at = None
            unanswered = view["unanswered"] = self._state.unanswered
        if self._running:
            return {**view, "reason": "Running"}
        # The silence counts from real chat only (a plan that went out is not chat).
        last = self._targets.chat_base(conversation) or self._started
        if self._base != last or self._due is None:
            self._base = last
            span = cfg["silence_max"] - cfg["silence_min"]
            minutes = (cfg["silence_min"] + self._rng() * span) * cfg["followup_factor"] ** unanswered
            self._due = last + timedelta(minutes=minutes)
            self._window_ready = None
        due = self._due
        view["next_at"] = due
        if unanswered > cfg["max_unanswered"]:
            return {**view, "reason": "UnansweredLimit", "next_at": None}
        if self._state.chat_today(now) >= cfg["daily_limit"]:
            return {**view, "reason": "DailyLimit", "next_at": None}
        if now < due:
            return {**view, "reason": "Waiting"}
        if self._retry_at is not None and now < self._retry_at:
            return {**view, "reason": "Retry", "next_at": self._retry_at}
        minute = now.hour * 60 + now.minute
        if in_quiet_hours(cfg, minute):
            end = quiet_hours_end(cfg)
            midnight = datetime(now.year, now.month, now.day)
            at = midnight + timedelta(minutes=end)
            if at <= now:
                at += timedelta(days=1)
            return {**view, "reason": "QuietHours", "next_at": at}
        if self._window_ready is not None and now < self._window_ready:
            return {**view, "reason": "OutsideWindow", "next_at": self._window_ready}
        inside, _end = window_state(cfg, now.weekday(), minute)
        if not inside:
            ahead, start, length = next_window_start(cfg, now.weekday(), minute)
            begin = datetime(now.year, now.month, now.day) + timedelta(days=ahead, minutes=start)
            delay = self._rng() * min(WINDOW_DELAY_MAX_MINUTES, max(length - 1, 0))
            self._window_ready = begin + timedelta(minutes=delay)
            return {**view, "reason": "OutsideWindow", "next_at": self._window_ready}
        quiet = timedelta(minutes=settings["quiet_minutes"])
        # "Conversing" also counts a plan that just went out (any activity), so a chat message never cuts in.
        recent = self._targets.last_activity(conversation) or last
        if quiet and now - recent < quiet:
            return {**view, "reason": "Conversing", "next_at": recent + quiet}
        if self._busy_reason is not None and self._busy_reason(conversation):
            return {**view, "reason": "Busy"}
        remaining = self._state.gap_remaining(now, settings["min_gap_minutes"])
        if remaining:
            return {**view, "reason": "Gap", "next_at": now + remaining}
        if self._t1_blocking is not None and self._t1_blocking():
            return {**view, "reason": "T1Priority"}
        return {**view, "reason": "Ready"}

    def snapshot(self) -> dict[str, Any]:
        """The console view: reason code, expected time, today's count and the unanswered streak."""
        now = self._clock()
        cfg = proactive_chat_settings(self._get_config())
        view = self._evaluate(now)
        return {
            "reason": view["reason"],
            "next_at": view["next_at"].isoformat(timespec="minutes") if view["next_at"] is not None else None,
            "today": self._state.chat_today(now),
            "daily_limit": cfg["daily_limit"],
            "unanswered": self._state.unanswered,
            "max_unanswered": cfg["max_unanswered"],
            "owner_known": view["conversation"] is not None,
            "running": self._running,
        }

    # -- one check

    def tick(self) -> asyncio.Task | None:
        """Start the message when it is due and allowed; returns the started task."""
        now = self._clock()
        view = self._evaluate(now)
        reason = view["reason"]
        if reason in LOGGED_REASONS:
            if self._logged != reason:
                self._logged = reason
                self._log("info", f"[STBridge] ProactiveChat result=skip:{reason}")
        elif reason != "Running":
            self._logged = None
        if reason != "Ready":
            return None
        elapsed = max(0, int((now - (self._base or now)).total_seconds() // 60))
        unanswered = self._state.unanswered
        self._running = True
        self._logged = None
        task = asyncio.create_task(self._run(elapsed, unanswered))
        self._task = task
        return task

    async def _run(self, elapsed: int, unanswered: int) -> None:
        result: Any = None
        try:
            result = await self._runner(False, elapsed, unanswered)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log("error", f"[STBridge] ProactiveChat result=failed:{type(error).__name__}")
            result = {"result": f"failed:{type(error).__name__}"}
        finally:
            self._running = False
        try:
            self._finish(result)
        except Exception as error:
            self._log("error", f"[STBridge] ProactiveChat bookkeeping failed ({type(error).__name__})")

    def _finish(self, result: Any) -> None:
        now = self._clock()
        text = result.get("result") if isinstance(result, dict) else None
        if delivered(text):
            self._state.record_chat_sent(now)
            self._retry_at = None
            # Redraw the next target: the message itself is activity and the unanswered count went up.
            self._base = None
        elif text == "deferred:Requeued":
            # Somebody started talking before ST was asked: not a failure, the new activity redraws the target.
            pass
        else:
            self._retry_at = now + timedelta(minutes=RETRY_MINUTES)

    async def run_now(self) -> Any:
        """One message right now, ignoring time, daily limit and silence rules (the console test button).

        It still goes through the queue; it is not counted as today's message nor as an unanswered one."""
        if self._running:
            raise RuntimeError("proactive chat is already running")
        now = self._clock()
        conversation = self._targets.owner_conversation
        last = self._targets.chat_base(conversation) if conversation else None
        elapsed = max(0, int((now - (last or self._started)).total_seconds() // 60))
        self._running = True
        try:
            return await self._runner(True, elapsed, self._state.unanswered)
        finally:
            self._running = False
