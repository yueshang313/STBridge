"""What happened today (H2): replies sent and songs sung, for the console overview.

Counted by the host's local date; a new date starts from zero. Persisted in ``daily_counts.json`` so a restart goes
on counting. A broken or unreadable file means empty counts; nothing here may stop a reply. Only replies to people
count (an owner or group chat turn that got a usable reply): proactive chat messages and scheduled plans are not
replies, and test chats from the attack runner never get here. No web-framework dependency.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .schedule_service import _read_json, _write_json

COUNTS_FILE = "daily_counts.json"
COUNTS_VERSION = 1
REPLY_KINDS = ("private", "group")
SONG_KINDS = ("owner", "group")


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class DailyCounts:
    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], datetime] = datetime.now,
        logger: Any = None,
    ) -> None:
        self._path = Path(data_dir) / COUNTS_FILE
        self._clock = clock
        self._logger = logger
        self._lock = threading.Lock()
        self._date = self._today()
        self._replies = {kind: 0 for kind in REPLY_KINDS}
        self._songs = {kind: 0 for kind in SONG_KINDS}
        self._load()

    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _warn(self, message: str) -> None:
        try:
            if self._logger is not None:
                self._logger.warning(message)
        except Exception:
            pass

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = _read_json(self._path)
        replies, songs = data.get("replies"), data.get("songs")
        if data.get("version") != COUNTS_VERSION or not isinstance(data.get("date"), str) or not isinstance(replies, dict) or not isinstance(songs, dict):
            self._warn("[STBridge] Daily counts file unreadable; counting from zero")
            return
        if data["date"] != self._date:
            return
        self._replies = {kind: _count(replies.get(kind)) for kind in REPLY_KINDS}
        self._songs = {kind: _count(songs.get(kind)) for kind in SONG_KINDS}

    def _roll(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._replies = {kind: 0 for kind in REPLY_KINDS}
            self._songs = {kind: 0 for kind in SONG_KINDS}

    def _save(self) -> None:
        try:
            _write_json(
                self._path,
                {"version": COUNTS_VERSION, "date": self._date, "replies": dict(self._replies), "songs": dict(self._songs)},
            )
        except Exception as error:
            self._warn(f"[STBridge] Daily counts not saved ({type(error).__name__})")

    def add_reply(self, kind: str) -> None:
        """One reply went to a person: ``kind`` is "private" or "group"."""
        if kind not in REPLY_KINDS:
            return
        with self._lock:
            self._roll()
            self._replies[kind] += 1
            self._save()

    def add_song(self, kind: str) -> None:
        """One song was sung and sent: ``kind`` is "owner" or "group"."""
        if kind not in SONG_KINDS:
            return
        with self._lock:
            self._roll()
            self._songs[kind] += 1
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll()
            return {
                "date": self._date,
                "replies": {**self._replies, "total": sum(self._replies.values())},
                "songs": {**self._songs, "total": sum(self._songs.values())},
            }
