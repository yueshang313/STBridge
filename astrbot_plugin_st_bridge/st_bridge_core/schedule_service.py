"""Scheduled proactive messages: target sessions, fired-state, time math and the scheduler loop.

The scheduler itself sends nothing: for every due occurrence it calls the ``runner`` the
plugin provides. Time is the host's local wall clock (naive ``datetime``); the clock, the
sleep and the randomness are injectable for tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .emotion_config import _replace_with_retry
from .schedule_config import parse_hhmm, schedule_section


CATCH_UP_MINUTES = 10
CHECK_INTERVAL_SECONDS = 25.0
STATE_KEEP_DAYS = 3
TARGETS_FILE = "schedule_targets.json"
STATE_FILE = "schedule_state.json"
PROACTIVE_STATE_FILE = "proactive_state.json"
MAX_LAST_TEXTS = 500
LAST_TEXT_CHARS = 400
MAX_GROUPS = 200
CHAT_BASE_SAVE_INTERVAL = timedelta(seconds=60)


def group_key_for(conversation: str) -> str:
    """Short stable key of a group session; the conversation itself is never shown or logged."""
    return hashlib.sha256(conversation.encode("utf-8")).hexdigest()[:8]


def _write_json(path: Path, data: Any) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class ScheduleTargets:
    """Sessions proactive messages can go to; remembered across restarts.

    The owner's private session and every group seen are persisted. The last activity time
    of each session (used by the quiet-time rule) is kept in memory only.

    The conversation ids are stored under the key "umo" in schedule_targets.json: that name comes from the
    first (AstrBot-only) version of the file and is kept so existing files keep working.
    """

    def __init__(self, data_dir: Path, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self._path = Path(data_dir) / TARGETS_FILE
        self._clock = clock
        data = _read_json(self._path)
        owner = data.get("owner")
        self._owner_conversation: str | None = (
            owner.get("umo") if isinstance(owner, dict) and isinstance(owner.get("umo"), str) else None
        )
        self._groups: dict[str, dict[str, Any]] = {}
        groups = data.get("groups")
        if isinstance(groups, dict):
            for key, item in groups.items():
                if (
                    isinstance(key, str)
                    and isinstance(item, dict)
                    and isinstance(item.get("umo"), str)
                    and group_key_for(item["umo"]) == key
                ):
                    self._groups[key] = {
                        "umo": item["umo"],
                        "last_seen": item.get("last_seen") if isinstance(item.get("last_seen"), str) else "",
                        "alias": item.get("alias") if isinstance(item.get("alias"), str) else "",
                    }
        self._activity: dict[str, datetime] = {}
        # Where the owner-chat silence is counted from (L1e): only real chat moves it (the owner's message, the
        # character's finished reply, a proactive chat message), never a plan. Persisted with the owner session.
        self._chat_base: datetime | None = None
        self._chat_base_saved: datetime | None = None
        base = data.get("owner_chat_base")
        if isinstance(base, dict) and self._owner_conversation is not None and base.get("umo") == self._owner_conversation:
            parsed = _parse_iso(base.get("at"))
            if parsed is not None and parsed <= clock():
                self._chat_base = parsed
                self._chat_base_saved = parsed
        # User messages seen per session (memory only): a changed count means someone spoke.
        self._user_seq: dict[str, int] = {}
        # The Chinese text of the character's last message per session (memory only).
        self._last_text: dict[str, str] = {}

    def _save(self) -> None:
        try:
            _write_json(
                self._path,
                {
                    "owner": {"umo": self._owner_conversation} if self._owner_conversation else None,
                    "groups": self._groups,
                    "owner_chat_base": (
                        {"umo": self._owner_conversation, "at": self._chat_base.isoformat(timespec="seconds")}
                        if self._owner_conversation and self._chat_base is not None
                        else None
                    ),
                },
            )
        except OSError:
            # Remembering targets is best effort; the in-memory copy keeps working.
            pass
        # Also after a failed write: the throttle must not turn every message into a retry.
        self._chat_base_saved = self._chat_base

    def record_owner(self, conversation: str) -> None:
        """Remember the owner's private session and count it as activity (and as chat)."""
        if not isinstance(conversation, str) or not conversation:
            return
        now = self._clock()
        self._activity[conversation] = now
        self._user_seq[conversation] = self._user_seq.get(conversation, 0) + 1
        if conversation != self._owner_conversation:
            self._owner_conversation = conversation
            self._chat_base = now
            self._save()
        else:
            self._set_chat_base(now)

    def _set_chat_base(self, now: datetime, *, force_save: bool = False) -> None:
        """Move the chat base; written to disk at most once a minute unless ``force_save``."""
        self._chat_base = now
        saved = self._chat_base_saved
        if force_save or saved is None or now - saved >= CHAT_BASE_SAVE_INTERVAL:
            self._save()

    def touch_chat(self, conversation: str, *, force_save: bool = False) -> None:
        """Real chat happened in the session (a finished reply, a delivered song, a proactive chat message).

        Only the owner's private session has a chat base; any other session is ignored."""
        if isinstance(conversation, str) and conversation and conversation == self._owner_conversation:
            self._set_chat_base(self._clock(), force_save=force_save)

    def chat_base(self, conversation: str | None) -> datetime | None:
        """When the owner-chat silence started to count (None: not the owner session or nothing known)."""
        if conversation and conversation == self._owner_conversation:
            return self._chat_base
        return None

    def record_group(self, conversation: str) -> str | None:
        """Remember a group session (persisted at most once per minute) and count activity."""
        if not isinstance(conversation, str) or not conversation:
            return None
        now = self._clock()
        self._activity[conversation] = now
        self._user_seq[conversation] = self._user_seq.get(conversation, 0) + 1
        key = group_key_for(conversation)
        entry = self._groups.get(key)
        if entry is None:
            if len(self._groups) >= MAX_GROUPS:
                return None
            self._groups[key] = {"umo": conversation, "last_seen": now.isoformat(timespec="seconds"), "alias": ""}
            self._save()
        else:
            previous = _parse_iso(entry["last_seen"])
            entry["last_seen"] = now.isoformat(timespec="seconds")
            if previous is None or now - previous >= timedelta(minutes=1):
                self._save()
        return key

    def touch(self, conversation: str) -> None:
        if isinstance(conversation, str) and conversation:
            self._activity[conversation] = self._clock()

    def last_activity(self, conversation: str) -> datetime | None:
        return self._activity.get(conversation)

    def user_seq(self, conversation: str) -> int:
        """How many user messages this session has received since start (a change = someone spoke)."""
        return self._user_seq.get(conversation, 0)

    def set_last_text(self, conversation: str, text: str) -> None:
        if not isinstance(conversation, str) or not conversation or not isinstance(text, str):
            return
        if conversation not in self._last_text and len(self._last_text) >= MAX_LAST_TEXTS:
            self._last_text.pop(next(iter(self._last_text)))
        self._last_text[conversation] = text[:LAST_TEXT_CHARS]

    def last_text(self, conversation: str) -> str:
        return self._last_text.get(conversation, "")

    @property
    def owner_conversation(self) -> str | None:
        return self._owner_conversation

    @property
    def has_owner(self) -> bool:
        return self._owner_conversation is not None

    def conversation_for(self, target: dict[str, Any]) -> str | None:
        if target.get("type") == "group":
            entry = self._groups.get(target.get("key"))
            return entry["umo"] if entry else None
        return self._owner_conversation

    def groups(self) -> list[dict[str, Any]]:
        """Console view: key, alias and last seen; never the conversation."""
        return [
            {
                "key": key,
                "alias": entry["alias"] or f"群 {key}",
                "custom_alias": entry["alias"],
                "last_seen": entry["last_seen"],
            }
            for key, entry in sorted(self._groups.items(), key=lambda item: item[1]["last_seen"], reverse=True)
        ]

    def has_group(self, key: str) -> bool:
        return key in self._groups

    def set_alias(self, key: str, alias: str) -> bool:
        entry = self._groups.get(key)
        if entry is None:
            return False
        entry["alias"] = alias
        self._save()
        return True


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class ScheduleState:
    """Occurrences that already fired, so a reload never sends the same one twice."""

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / STATE_FILE
        fired = _read_json(self._path).get("fired")
        self._fired: dict[str, str] = (
            {key: value for key, value in fired.items() if isinstance(key, str) and isinstance(value, str)}
            if isinstance(fired, dict)
            else {}
        )

    def fired(self, key: str) -> bool:
        return key in self._fired

    def unmark(self, key: str) -> None:
        """Take an occurrence back (a send that was given up before anything went out)."""
        if self._fired.pop(key, None) is None:
            return
        try:
            _write_json(self._path, {"fired": self._fired})
        except OSError:
            pass

    def mark(self, key: str, now: datetime) -> None:
        self._fired[key] = now.isoformat(timespec="seconds")
        cutoff = (now - timedelta(days=STATE_KEEP_DAYS)).isoformat(timespec="seconds")
        self._fired = {name: value for name, value in self._fired.items() if value >= cutoff}
        try:
            _write_json(self._path, {"fired": self._fired})
        except OSError:
            pass



def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class ProactiveState:
    """What outlives a restart about proactive messages: when the last one went out (any kind), and the
    owner-chat counters (messages sent today, messages in a row the owner has not answered)."""

    def __init__(self, data_dir: Path, *, logger: Any = None) -> None:
        self._path = Path(data_dir) / PROACTIVE_STATE_FILE
        data: dict[str, Any] = {}
        if self._path.exists():
            try:
                value = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                data = value
            except (OSError, ValueError):
                if logger is not None:
                    try:
                        logger.warning("[STBridge] Proactive state unreadable (starting empty)")
                    except Exception:
                        pass
        self._last_sent = _parse_iso(data.get("last_sent_at"))
        chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
        self._date = chat.get("date") if isinstance(chat.get("date"), str) else ""
        self._count = _count(chat.get("count"))
        self._unanswered = _count(chat.get("unanswered"))

    def _save(self) -> None:
        try:
            _write_json(
                self._path,
                {
                    "last_sent_at": self._last_sent.isoformat(timespec="seconds") if self._last_sent else None,
                    "chat": {"date": self._date, "count": self._count, "unanswered": self._unanswered},
                },
            )
        except OSError:
            pass

    @property
    def last_sent(self) -> datetime | None:
        return self._last_sent

    def gap_remaining(self, now: datetime, minutes: int) -> timedelta:
        """How much of the minimum gap since the last proactive message is still left (zero: free)."""
        if not minutes or self._last_sent is None:
            return timedelta(0)
        return max(timedelta(0), self._last_sent + timedelta(minutes=minutes) - now)

    def record_sent(self, now: datetime) -> None:
        """Any proactive message (a plan or a chat one) has gone out."""
        self._last_sent = now
        self._save()

    def chat_today(self, now: datetime) -> int:
        return self._count if self._date == now.date().isoformat() else 0

    @property
    def unanswered(self) -> int:
        return self._unanswered

    def record_chat_sent(self, now: datetime) -> None:
        today = now.date().isoformat()
        self._count = self._count + 1 if self._date == today else 1
        self._date = today
        self._unanswered += 1
        self._last_sent = now
        self._save()

    def clear_unanswered(self) -> None:
        if self._unanswered:
            self._unanswered = 0
            self._save()


# ---- time math ----------------------------------------------------------------------


def occurrence_key(plan_id: str, when: datetime) -> str:
    return f"{plan_id}@{when.strftime('%Y-%m-%dT%H:%M')}"


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def plan_occurrences(plan: dict[str, Any], day: date) -> list[datetime]:
    """Scheduled datetimes of the plan for one calendar day (daily jitter is seeded, not random)."""
    base = _midnight(day)
    if plan["kind"] == "daily":
        weekdays = plan.get("weekdays") or []
        if weekdays and day.weekday() not in weekdays:
            return []
        jitter = int(plan.get("jitter_minutes", 0))
        offset = 0
        if jitter:
            # Same date + plan id gives the same offset after a reload.
            offset = random.Random(f"{day.isoformat()}:{plan['id']}").randint(-jitter, jitter)
        return [base + timedelta(minutes=parse_hhmm(plan["time"], "time") + offset)]
    start = parse_hhmm(plan["start"], "start")
    end = parse_hhmm(plan["end"], "end")
    every = int(plan["every_minutes"])
    return [base + timedelta(minutes=minute) for minute in range(start, end + 1, every)]


def candidate_occurrences(plan: dict[str, Any], now: datetime, *, days_back: int = 1, days_ahead: int = 0) -> list[datetime]:
    """Occurrences of yesterday..today (daily jitter can cross midnight), sorted."""
    today = now.date()
    found: list[datetime] = []
    for offset in range(-days_back, days_ahead + 1):
        found.extend(plan_occurrences(plan, today + timedelta(days=offset)))
    return sorted(found)


def next_fire_time(plan: dict[str, Any], now: datetime, is_fired: Callable[[str], bool]) -> datetime | None:
    """The next occurrence not yet fired that is still ahead (or within the catch-up window)."""
    for when in candidate_occurrences(plan, now, days_back=1, days_ahead=8):
        if when < now - timedelta(minutes=CATCH_UP_MINUTES):
            continue
        if is_fired(occurrence_key(plan["id"], when)):
            continue
        return when
    return None


# ---- scheduler ----------------------------------------------------------------------

Runner = Callable[[dict[str, Any], bool], Awaitable[Any]]


class ScheduleScheduler:
    """Fires due plan occurrences through ``runner(plan, forced)``.

    Rules: one fire per occurrence (persisted); an occurrence still due within the catch-up
    window is sent late, anything older is skipped; activity in the target session within
    ``quiet_minutes``, a busy queue or a send in progress (``busy_reason``), or the minimum gap
    after the last proactive message (``proactive``) defers it until that is over or
    ``max_defer_minutes`` after the scheduled time, whichever comes first; a plan never runs twice at once.
    A run the runner gave up before anything went out (``deferred:Requeued``) is taken back and deferred.
    """

    def __init__(
        self,
        *,
        get_config: Callable[[], Any],
        targets: ScheduleTargets,
        state: ScheduleState,
        runner: Runner,
        clock: Callable[[], datetime] = datetime.now,
        logger: Any = None,
        busy_reason: Callable[[str], str | None] | None = None,
        proactive: "ProactiveState | None" = None,
    ) -> None:
        self._busy_reason = busy_reason
        self._proactive = proactive
        self._get_config = get_config
        self._targets = targets
        self._state = state
        self._runner = runner
        self._clock = clock
        self._logger = logger
        self._running: set[str] = set()
        self._deferring: set[str] = set()
        self._defer_private: set[str] = set()
        self._defer_reason: dict[str, str] = {}
        self._done: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    # -- lifecycle

    def start(self, interval: float = CHECK_INTERVAL_SECONDS) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        tasks = list(self._tasks)
        for item in tasks:
            item.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self, interval: float) -> None:
        while True:
            try:
                self.tick()
            except Exception as error:
                self._log("error", f"[STBridge] Schedule check failed ({type(error).__name__})")
            await asyncio.sleep(interval)

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            try:
                getattr(self._logger, level)(message)
            except Exception:
                pass

    # -- state used by the console

    def is_running(self, plan_id: str) -> bool:
        return plan_id in self._running

    def blocks_chat(self) -> bool:
        """Does a plan own the owner's chat right now (running, or deferring for the owner's private session)?"""
        return bool(self._running) or bool(self._defer_private)

    def _clear_defer(self, key: str) -> None:
        self._deferring.discard(key)
        self._defer_private.discard(key)
        self._defer_reason.pop(key, None)

    def next_fire(self, plan: dict[str, Any]) -> datetime | None:
        return next_fire_time(plan, self._clock(), self._state.fired)

    async def run_now(self, plan: dict[str, Any]) -> Any:
        """Run a plan once right now, ignoring time and quiet rules (the console "test" button)."""
        if plan["id"] in self._running:
            raise RuntimeError("plan is already running")
        self._running.add(plan["id"])
        try:
            return await self._runner(plan, True)
        finally:
            self._running.discard(plan["id"])

    # -- one check

    def tick(self) -> list[asyncio.Task]:
        """Start every occurrence that is due now; returns the started tasks."""
        config = self._get_config()
        section = schedule_section(config)
        settings = section["settings"]
        started: list[asyncio.Task] = []
        if not settings["enabled"]:
            return started
        now = self._clock()
        if len(self._done) > 500:
            # Old occurrence keys can no longer come back; keep the set small.
            self._done = {key for key in self._done if key.split("@", 1)[-1][:10] >= (now - timedelta(days=2)).strftime("%Y-%m-%d")}
        for plan in section["plans"]:
            if not plan["enabled"]:
                continue
            for when in candidate_occurrences(plan, now, days_back=1):
                task = self._consider(plan, when, now, settings)
                if task is not None:
                    started.append(task)
        return started

    def _consider(self, plan: dict[str, Any], when: datetime, now: datetime, settings: dict[str, Any]) -> asyncio.Task | None:
        key = occurrence_key(plan["id"], when)
        if key in self._done or self._state.fired(key) or now < when:
            return None
        late = now - when
        if key not in self._deferring and late > timedelta(minutes=CATCH_UP_MINUTES):
            self._done.add(key)
            # Yesterday's occurrences are only candidates for midnight jitter: no log noise for them.
            if late < timedelta(hours=12):
                self._log("info", f"[STBridge] Schedule plan={plan['id']} name={plan['name']} result=skip:Missed")
            return None
        if plan["id"] in self._running:
            return None
        conversation = self._targets.conversation_for(plan["target"])
        if conversation is None:
            self._done.add(key)
            reason = "NoOwnerSession" if plan["target"]["type"] == "private" else "UnknownGroup"
            self._log("info", f"[STBridge] Schedule plan={plan['id']} name={plan['name']} result=skip:{reason}")
            return None
        quiet = timedelta(minutes=settings["quiet_minutes"])
        last = self._targets.last_activity(conversation)
        reason = None
        if quiet and last is not None and now - last < quiet:
            reason = "Quiet"
        elif self._busy_reason is not None and self._busy_reason(conversation):
            reason = "Busy"
        elif self._proactive is not None and self._proactive.gap_remaining(now, settings["min_gap_minutes"]):
            reason = "Gap"
        if reason is not None:
            self._deferring.add(key)
            if plan["target"]["type"] == "private":
                self._defer_private.add(key)
            if late > timedelta(minutes=settings["max_defer_minutes"]):
                self._clear_defer(key)
                self._done.add(key)
                self._log(
                    "info",
                    f"[STBridge] Schedule plan={plan['id']} name={plan['name']} result=skip:DeferTimeout last={reason}",
                )
            elif reason != "Quiet" and self._defer_reason.get(key) != reason:
                # Logged when the reason changes only; the old quiet deferral stays silent as before.
                self._defer_reason[key] = reason
                self._log(
                    "info",
                    f"[STBridge] Schedule plan={plan['id']} name={plan['name']} result=defer:{reason}",
                )
            return None
        self._clear_defer(key)
        # Persist before sending: a crash or reload mid-send must not send it again.
        self._state.mark(key, now)
        self._done.add(key)
        self._running.add(plan["id"])
        task = asyncio.create_task(self._run(plan, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run(self, plan: dict[str, Any], key: str | None = None) -> None:
        try:
            result = await self._runner(plan, False)
            if key is not None and isinstance(result, dict) and result.get("result") == "deferred:Requeued":
                # Given up before ST was asked (someone started talking): not sent, so not "fired".
                self._state.unmark(key)
                self._done.discard(key)
                self._deferring.add(key)
                if plan["target"]["type"] == "private":
                    self._defer_private.add(key)
                self._defer_reason[key] = "Requeued"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log(
                "error",
                f"[STBridge] Schedule plan={plan['id']} name={plan['name']} "
                f"result=failed:{type(error).__name__}",
            )
        finally:
            self._running.discard(plan["id"])
