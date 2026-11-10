"""The character's recent moods (L1c): what each reply felt like, and the sentence ST reads about it.

Two lists live in ``mood.json``: ``global`` (every valid reply and scheduled message) and ``owner`` (only
replies to the owner's private chat). The text for the ``{{stbridge_mood}}`` / ``{{stbridge_owner_mood}}``
macros is worked out when asked, from the wall clock, so it keeps fading while nobody talks.
This module has no web-framework dependency.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .emotion_config import _replace_with_retry
from .log import logger


MOOD_FILE_NAME = "mood.json"
MAX_ENTRIES = 20
GLOBAL = "global"
OWNER = "owner"
TRACKS = (GLOBAL, OWNER)
EMPTY_TEXT = "还没有记录（按平常的状态）"
CALM_SUFFIX = "；现在心情平静"
MAX_TEXT_CHARS = 1000


def _age_text(seconds: float) -> str:
    if seconds < 120:
        return "刚才"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def _degree(seconds: float, window_minutes: int) -> str:
    minutes = seconds / 60
    if minutes < window_minutes / 3:
        return "还很明显"
    if minutes < window_minutes:
        return "淡了一些"
    return "已经平复"


def format_mood(entries: list[dict[str, Any]], now: float, settings: dict[str, Any]) -> str:
    """The macro text for one list: recent trend, how long ago, how strong, and whether it has passed."""
    window = int(settings["decay_minutes"])
    recent = int(settings["recent"])
    merged: list[list[Any]] = []  # [emotion, latest time, count], oldest first
    for entry in sorted(entries, key=lambda item: item["at"]):
        if merged and merged[-1][0] == entry["emotion"]:
            merged[-1][1] = entry["at"]
            merged[-1][2] += 1
        else:
            merged.append([entry["emotion"], entry["at"], 1])
    merged = merged[-recent:]
    if not merged:
        return EMPTY_TEXT
    parts: list[str] = []
    degrees: list[str] = []
    for emotion, at, count in merged:
        age = max(0.0, now - at)
        degree = _degree(age, window)
        details = [_age_text(age)]
        if count > 1:
            details.append(f"连续 {count} 次")
        details.append(degree)
        parts.append(f"{emotion}（{'，'.join(details)}）")
        degrees.append(degree)
    suffix = CALM_SUFFIX if degrees[-1] == "已经平复" else ""
    # Too long: the oldest items go first.
    while len(parts) > 1 and len(" → ".join(parts)) + len(suffix) > MAX_TEXT_CHARS:
        parts.pop(0)
    text = " → ".join(parts) + suffix
    return text[:MAX_TEXT_CHARS]


def _clean_list(value: Any) -> list[dict[str, Any]]:
    result = []
    if not isinstance(value, list):
        return result
    for item in value:
        if (
            isinstance(item, dict)
            and isinstance(item.get("emotion"), str)
            and item["emotion"]
            and isinstance(item.get("at"), (int, float))
            and not isinstance(item["at"], bool)
            and item["at"] == item["at"]
        ):
            result.append({"emotion": item["emotion"], "at": float(item["at"])})
    return result[-MAX_ENTRIES:]


class MoodStore:
    """The two mood lists, saved atomically; a broken file reads as empty and never stops a reply."""

    def __init__(self, data_dir: Path, *, clock: Callable[[], float] = time.time):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / MOOD_FILE_NAME
        self.clock = clock
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        empty = {GLOBAL: [], OWNER: []}
        if not self.path.exists():
            return empty
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            return {track: _clean_list(raw.get(track)) for track in TRACKS}
        except (OSError, ValueError):
            logger.warning("[STBridge] Mood data unreadable (treated as empty)")
            return empty

    def _write(self) -> None:
        fd, temporary = tempfile.mkstemp(prefix="mood.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"version": 1, **self.data}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save(self) -> bool:
        try:
            self._write()
            return True
        except Exception as error:
            logger.warning(f"[STBridge] Mood data not saved ({type(error).__name__})")
            return False

    def record(self, emotion: str, tracks: tuple[str, ...], at: float | None = None) -> None:
        """Remember one reply's emotion in the given lists (the time defaults to now)."""
        moment = self.clock() if at is None else at
        with self._lock:
            for track in tracks:
                if track in TRACKS:
                    self.data[track] = [*self.data[track], {"emotion": emotion, "at": float(moment)}][-MAX_ENTRIES:]
            self._save()

    def clear(self, track: str) -> None:
        if track not in TRACKS:
            raise ValueError("unknown mood list")
        with self._lock:
            self.data[track] = []
            self._save()

    def entries(self, track: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.data.get(track, [])]

    def text(self, track: str, settings: dict[str, Any]) -> str:
        return format_mood(self.entries(track), self.clock(), settings)

    def summary(self, track: str, settings: dict[str, Any]) -> dict[str, Any] | None:
        """The latest mood of a list for the console overview: the label, how long ago, how strong, whether passed."""
        entries = self.entries(track)
        if not entries:
            return None
        latest = max(entries, key=lambda item: item["at"])
        age = max(0.0, self.clock() - latest["at"])
        degree = _degree(age, int(settings["decay_minutes"]))
        return {"emotion": latest["emotion"], "age": _age_text(age), "degree": degree, "calm": degree == "已经平复"}
