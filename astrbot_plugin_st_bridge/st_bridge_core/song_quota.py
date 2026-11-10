"""Daily song-request counts per requester, persisted in plugin_data/song_quota.json.

Keys are the affinity store's keys (the owner's bare key, group members under their own "group:" key),
so one person has one count everywhere. The count resets when the host's local date changes. A broken or
unreadable file means empty counts; nothing here may stop a song request. No web-framework dependency.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .schedule_service import _read_json, _write_json

QUOTA_FILE_NAME = "song_quota.json"
QUOTA_VERSION = 1


class SongQuota:
    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], datetime] = datetime.now,
        logger: Any = None,
    ) -> None:
        self._path = Path(data_dir) / QUOTA_FILE_NAME
        self._clock = clock
        self._logger = logger
        self._lock = threading.Lock()
        self._date, self._counts = self._load()

    def _log(self, message: str) -> None:
        try:
            if self._logger is not None:
                self._logger.warning(message)
        except Exception:
            pass

    def today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _load(self) -> tuple[str, dict[str, int]]:
        today = self.today()
        if not self._path.exists():
            return today, {}
        data = _read_json(self._path)
        counts = data.get("counts")
        if data.get("version") != QUOTA_VERSION or not isinstance(data.get("date"), str) or not isinstance(counts, dict):
            self._log("[STBridge] Song quota file unreadable; counting from zero")
            return today, {}
        if data["date"] != today:
            return today, {}
        clean = {
            key: value for key, value in counts.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
        return today, clean

    def _roll(self) -> None:
        today = self.today()
        if today != self._date:
            self._date, self._counts = today, {}

    def _save(self) -> None:
        try:
            _write_json(
                self._path,
                {"version": QUOTA_VERSION, "date": self._date, "counts": dict(self._counts)},
            )
        except Exception as error:
            self._log(f"[STBridge] Song quota not saved ({type(error).__name__})")

    def used(self, key: str) -> int:
        with self._lock:
            self._roll()
            return self._counts.get(key, 0)

    def try_consume(self, key: str, limit: int) -> bool:
        """Count one song for `key` unless it already reached `limit` today. A limit of 0 never counts."""
        if limit <= 0:
            return True
        with self._lock:
            self._roll()
            used = self._counts.get(key, 0)
            if used >= limit:
                return False
            self._counts[key] = used + 1
            self._save()
            return True

    def refund(self, key: str, date: str | None = None) -> None:
        """Give one back (never below 0). `date` is the day it was counted on: a refund after midnight is void."""
        with self._lock:
            self._roll()
            if date is not None and date != self._date:
                return
            used = self._counts.get(key, 0)
            if used <= 0:
                return
            if used == 1:
                del self._counts[key]
            else:
                self._counts[key] = used - 1
            self._save()
