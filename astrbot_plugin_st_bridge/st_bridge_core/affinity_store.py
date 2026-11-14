"""Per-QQ-user affinity storage and MVU update-command parsing.

Two tracks live in MVU stat_data: the owner's 好感度 (0-100) and a separate
group-member variable (its own range). The bridge stores one value per QQ user,
hands the speaker's values to the ST client before each generation, and applies
the reply's MVU delta back to the speaker's own track.
This module has no web-framework dependency.
"""

from __future__ import annotations

import copy
import hashlib
import shutil
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .emotion_config import (
    AFFINITY_MAX,
    AFFINITY_MIN,
    CorruptDataFile,
    EmotionConfigError,
    _replace_with_retry,
    quarantine_file,
)


AFFINITY_FILE_NAME = "affinity.json"
MIGRATION_BACKUP_SUFFIX = ".bak-p6b"
KEY_MIGRATION_BACKUP_SUFFIX = ".bak-p6b1"
# 1: one shared 0-100 scale; 2: group members moved to their own track (reset once);
# 3: group members stored under a "group:" key (the bot gives one openid per person everywhere).
CURRENT_VERSION = 3
GROUP_KEY_PREFIX = "group:"
MAX_NICKNAME_CHARS = 40

_UPDATE_BLOCK_RE = re.compile(r"<UpdateVariable\b[^>]*>.*?</UpdateVariable\s*>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_UPDATE_RE = re.compile(r"<UpdateVariable\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<StatusPlaceHolderImpl\s*/>", re.IGNORECASE)
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_ARG = rf"""(?:(?P<q{{i}}>['"])\s*(?P<v{{i}}>{_NUMBER})\s*(?P=q{{i}})|(?P<n{{i}}>{_NUMBER})|(?P<o{{i}}>[^,)]+?))"""
_COMMAND_RE = re.compile(
    r"_\.(?P<op>add|set)\(\s*(?P<pq>['\"])(?P<path>[^'\"]+)(?P=pq)\s*,\s*"
    + _ARG.format(i=1)
    + r"\s*(?:,\s*"
    + _ARG.format(i=2)
    + r"\s*)?\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AffinityUpdate:
    """Commands for one character found in a reply, in order."""

    operations: tuple[tuple[str, int], ...]

    def resolve(self, current: int) -> int:
        """Return the delta these commands imply from the current value."""
        value = current
        for operation, number in self.operations:
            value = value + number if operation == "add" else number
        return value - current


def round_half_up(value: str) -> int:
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _arg_number(match: re.Match, index: int) -> str | None:
    quoted = match.group(f"v{index}")
    if quoted is not None:
        return quoted
    return match.group(f"n{index}")


def _matches_target(path: str, target: str) -> bool:
    normalized = re.sub(r"\s+", "", path)
    if normalized.startswith("stat_data."):
        normalized = normalized[len("stat_data."):]
    if normalized.endswith("[0]"):
        normalized = normalized[:-3]
    return normalized == target


def parse_affinity_commands(
    block_text: str, character: str, target: str | None = None
) -> list[tuple[str, int]]:
    """Commands for one variable: ``target`` (e.g. "群友好感度") or "<character>.好感度"."""
    wanted = target if target is not None else f"{character}.好感度"
    operations: list[tuple[str, int]] = []
    for match in _COMMAND_RE.finditer(block_text):
        if not _matches_target(match.group("path"), wanted):
            continue
        first = _arg_number(match, 1)
        second = _arg_number(match, 2)
        has_second = match.group("q2") is not None or match.group("n2") is not None or match.group("o2") is not None
        try:
            if match.group("op").lower() == "add":
                if first is None or has_second:
                    continue
                operations.append(("add", round_half_up(first)))
            else:
                # _.set(path, new) or _.set(path, old, new): the last argument is the new value.
                new_value = second if has_second else first
                if new_value is None:
                    continue
                operations.append(("set", round_half_up(new_value)))
        except (InvalidOperation, ValueError):
            continue
    return operations


def strip_update_blocks(
    text: str, character: str, target: str | None = None
) -> tuple[str, AffinityUpdate | None]:
    """Remove MVU <UpdateVariable> blocks and placeholders; parse the wanted variable's commands.

    An opening tag without a closing tag removes everything to the end of the text.
    """
    operations: list[tuple[str, int]] = []
    for block in _UPDATE_BLOCK_RE.findall(text):
        operations.extend(parse_affinity_commands(block, character, target))
    cleaned = _UPDATE_BLOCK_RE.sub("", text)
    unclosed = _UNCLOSED_UPDATE_RE.search(cleaned)
    if unclosed is not None:
        operations.extend(parse_affinity_commands(unclosed.group(0), character, target))
        cleaned = cleaned[: unclosed.start()]
    cleaned = _PLACEHOLDER_RE.sub("", cleaned)
    return cleaned, (AffinityUpdate(tuple(operations)) if operations else None)


def user_key(platform_id: Any, sender_id: Any) -> str:
    return f"{platform_id}:{sender_id}"


def group_key(key: str) -> str:
    """Storage key of a group member; the same person shares one entry across all groups."""
    return key if key.startswith(GROUP_KEY_PREFIX) else GROUP_KEY_PREFIX + key


def user_hash(key: str) -> str:
    """Short stable identifier for logs and the console; never the raw openid."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def clamp_affinity(value: int, low: int = AFFINITY_MIN, high: int = AFFINITY_MAX) -> int:
    return max(low, min(high, value))


def _clean_nickname(nickname: Any) -> str:
    if not isinstance(nickname, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", nickname).strip()[:MAX_NICKNAME_CHARS]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AffinityChange:
    before: int
    applied: int
    after: int


class AffinityStore:
    """Own affinity.json: {"version": 3, "users": {key: {value, nickname, updated_at}}}.

    Range limits are passed per call by track (owner 0-100, group members their
    own range). Group members are stored under ``group:<user key>``; any other
    key is the owner's entry. ``owner_key`` is only needed to migrate old files.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        owner_key: str | None = None,
        group_initial_value: int = 0,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / AFFINITY_FILE_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._frozen = False
        self.owner_key = owner_key
        self._group_initial_value = group_initial_value
        # True when an older file could not be migrated yet (owner unknown).
        self.migration_pending = False
        self.migrated = False
        # {"used": "backup" | "empty", "broken": [renamed file names]} after an unusable file was set aside.
        self.recovery: dict[str, Any] | None = None
        self.data = self._load_or_create()

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(AFFINITY_FILE_NAME + ".bak")

    @staticmethod
    def is_owner(key: str) -> bool:
        return not key.startswith(GROUP_KEY_PREFIX)

    def _load_or_create(self) -> dict[str, Any]:
        if not self.path.exists():
            data = {"version": CURRENT_VERSION, "users": {}}
            self._atomic_write(data)
            return data
        try:
            data = self._read_checked(self.path)
        except OSError as exc:
            # A locked share or missing permission is not a broken file: leave everything alone.
            raise RuntimeError("cannot read affinity data") from exc
        except CorruptDataFile:
            data = self._recover()
        if data["version"] < CURRENT_VERSION:
            data = self._migrate(data)
        return data

    @staticmethod
    def _read_checked(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8-sig") as handle:
            try:
                data = json.load(handle)
            except ValueError as exc:
                raise CorruptDataFile("affinity data is not valid JSON") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") not in (1, 2, CURRENT_VERSION)
            or not isinstance(data.get("users"), dict)
        ):
            raise CorruptDataFile("affinity data has an invalid structure")
        for entry in data["users"].values():
            if not isinstance(entry, dict) or isinstance(entry.get("value"), bool) or not isinstance(entry.get("value"), int):
                raise CorruptDataFile("affinity data has an invalid entry")
        return data

    def _recover(self) -> dict[str, Any]:
        """affinity.json is unusable: set it aside, then use affinity.json.bak, else start empty."""
        broken = [quarantine_file(self.path)]
        if broken[0] is None:
            raise RuntimeError("cannot set aside the broken affinity data")
        data: dict[str, Any] | None = None
        used = "empty"
        backup = self.backup_path
        if backup.exists():
            try:
                data = self._read_checked(backup)
            except CorruptDataFile:
                name = quarantine_file(backup)
                if name is not None:
                    broken.append(name)
            except OSError:
                pass
            else:
                shutil.copy2(backup, self.path)  # the .bak itself is not touched
                used = "backup"
        if data is None:
            data = {"version": CURRENT_VERSION, "users": {}}
            self._atomic_write(data)  # no affinity.json exists now, so no .bak is replaced
        self.recovery = {"used": used, "broken": broken}
        return data

    def _backup(self, suffix: str) -> None:
        backup = self.path.with_name(AFFINITY_FILE_NAME + suffix)
        if not backup.exists():
            shutil.copyfile(self.path, backup)

    def _migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Bring an older file to version 3, once; deferred while the owner is unknown.

        1 -> 2: every non-owner value is reset to the group initial value.
        2 -> 3: every record that is neither the owner's nor already ``group:``-prefixed
        moves under the ``group:`` key (an existing target wins); the owner stays as is.
        """
        if self.owner_key is None:
            self.migration_pending = True
            return data
        migrated = copy.deepcopy(data)
        if migrated["version"] == 1:
            self._backup(MIGRATION_BACKUP_SUFFIX)
            for key, entry in migrated["users"].items():
                if key != self.owner_key:
                    entry["value"] = self._group_initial_value
        self._backup(KEY_MIGRATION_BACKUP_SUFFIX)
        users = {}
        moved = []
        for key, entry in migrated["users"].items():
            if key == self.owner_key or key.startswith(GROUP_KEY_PREFIX):
                users[key] = entry
            else:
                moved.append((key, entry))
        for key, entry in moved:
            users.setdefault(GROUP_KEY_PREFIX + key, entry)
        migrated["users"] = users
        migrated["version"] = CURRENT_VERSION
        self._atomic_write(migrated)
        self.migrated = True
        return migrated

    def _atomic_write(self, data: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix="affinity.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                try:
                    shutil.copy2(self.path, self.backup_path)  # the last good file, for a later recovery
                except OSError:
                    pass
            _replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def freeze(self) -> None:
        """Refuse every later change (an import just replaced affinity.json; the core restarts to read it)."""
        self._frozen = True

    def _commit(self, key: str, value: int, nickname: Any, low: int, high: int) -> None:
        if self._frozen:
            raise EmotionConfigError(409, "核心正在重启，请稍后再试")
        candidate = copy.deepcopy(self.data)
        entry = dict(candidate["users"].get(key, {}))
        entry["value"] = clamp_affinity(value, low, high)
        cleaned = _clean_nickname(nickname)
        if cleaned:
            entry["nickname"] = cleaned
        entry.setdefault("nickname", "")
        entry["updated_at"] = _now()
        candidate["users"][key] = entry
        self._atomic_write(candidate)
        self.data = candidate

    def get(
        self,
        key: str,
        initial_value: int,
        low: int = AFFINITY_MIN,
        high: int = AFFINITY_MAX,
    ) -> int:
        entry = self.data["users"].get(key)
        return (
            clamp_affinity(entry["value"], low, high)
            if entry
            else clamp_affinity(initial_value, low, high)
        )

    def apply_delta(
        self,
        key: str,
        delta: int,
        *,
        max_delta: int,
        initial_value: int,
        nickname: Any = "",
        low: int = AFFINITY_MIN,
        high: int = AFFINITY_MAX,
    ) -> AffinityChange:
        with self._lock:
            before = self.get(key, initial_value, low, high)
            limited = max(-max_delta, min(max_delta, delta))
            after = clamp_affinity(before + limited, low, high)
            self._commit(key, after, nickname, low, high)
            return AffinityChange(before, after - before, after)

    def set_value(
        self,
        key: str,
        value: int,
        nickname: Any = "",
        *,
        low: int = AFFINITY_MIN,
        high: int = AFFINITY_MAX,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise EmotionConfigError(400, "invalid affinity value")
        with self._lock:
            self._commit(key, value, nickname, low, high)
            return value

    def set_value_by_hash(
        self,
        hashed: str,
        value: int,
        track_ranges: dict[str, tuple[int, int]] | None = None,
    ) -> str:
        """Set a user's value, validated against the range of the track the user belongs to."""
        with self._lock:
            matches = [key for key in self.data["users"] if user_hash(key) == hashed]
        if len(matches) != 1:
            raise EmotionConfigError(404, "affinity user not found")
        low, high = self._range_of(matches[0], track_ranges)
        self.set_value(matches[0], value, low=low, high=high)
        return matches[0]

    def _range_of(
        self, key: str, track_ranges: dict[str, tuple[int, int]] | None
    ) -> tuple[int, int]:
        ranges = track_ranges or {}
        track = "owner" if self.is_owner(key) else "group"
        return ranges.get(track, (AFFINITY_MIN, AFFINITY_MAX))

    def users(
        self, track_ranges: dict[str, tuple[int, int]] | None = None
    ) -> list[dict[str, Any]]:
        result = []
        for key, entry in self.data["users"].items():
            bare = key[len(GROUP_KEY_PREFIX):] if key.startswith(GROUP_KEY_PREFIX) else key
            platform = bare.split(":", 1)[0] if ":" in bare else ""
            low, high = self._range_of(key, track_ranges)
            result.append(
                {
                    "id": user_hash(key),
                    "platform": platform,
                    "track": "owner" if self.is_owner(key) else "group",
                    "nickname": entry.get("nickname", ""),
                    "value": clamp_affinity(entry["value"], low, high),
                    "updated_at": entry.get("updated_at", ""),
                }
            )
        result.sort(key=lambda item: item["updated_at"], reverse=True)
        return result
