"""Storage cleanup: removes the temporary files left behind by sending voices and pictures.

Four targets, all plain temp files that nothing reads again:
- AstrBot's own temp directory (qq_official leaves one media_* file per voice / picture),
- the bridge's `temp_audio` (normally deleted after 60 s; only a restart in between leaves files),
- `stbridge_cover_*` working directories of the cover worker in the system temp directory,
- `cover_stems` (the kept intermediate files of covers; its per-cover folders go once they are empty).

Only regular files older than the keep time are removed, never directories (except the cover worker's own
working directories and the emptied `cover_stems` folders), and never anything modified within the last hour.
The cover cache is only measured here.
No web-framework dependency.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .emotion_config import EmotionConfigError
from .schedule_service import _read_json, _write_json

MIN_AGE_SECONDS = 3600.0  # nothing younger than this is ever touched, manual or automatic
DAY_SECONDS = 86400.0
AUTO_INTERVAL_SECONDS = DAY_SECONDS
CHECK_SECONDS = 3600.0
COVER_TEMP_PREFIX = "stbridge_cover_"
STATE_FILE = "cleanup_state.json"
TARGETS = ("astrbot_temp", "bridge_temp", "cover_temp", "cover_stems")


def default_cleanup_settings() -> dict[str, Any]:
    return {"enabled": True, "keep_days": 3}


def validate_cleanup_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "cleanup settings must be an object")
    merged = {**default_cleanup_settings(), **settings}
    if not isinstance(merged["enabled"], bool):
        raise EmotionConfigError(400, "invalid cleanup enabled")
    days = merged["keep_days"]
    if (
        isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days)
        or days != int(days) or not 1 <= days <= 365
    ):
        raise EmotionConfigError(400, "invalid keep days")
    return {"enabled": merged["enabled"], "keep_days": int(days)}


def cleanup_settings(config: Any) -> dict[str, Any]:
    try:
        return validate_cleanup_settings(config.get("cleanup", {}))
    except (AttributeError, EmotionConfigError):
        return default_cleanup_settings()


def cutoff_time(now: float, keep_days: int) -> float:
    """Files modified after this moment are kept."""
    return now - max(float(keep_days) * DAY_SECONDS, MIN_AGE_SECONDS)


def _walk_files(root: Path):
    """Every regular file below root (links are neither followed nor returned); [] when root is missing."""
    try:
        if not root.is_dir() or root.is_symlink():
            return
    except OSError:
        return
    for directory, subdirs, names in os.walk(root, followlinks=False):
        subdirs[:] = [name for name in subdirs if not os.path.islink(os.path.join(directory, name))]
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                yield path


def _stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def measure_dir(root: Path) -> tuple[int, int]:
    """(files, bytes) of everything below root."""
    count = size = 0
    for path in _walk_files(root):
        info = _stat(path)
        if info is not None:
            count += 1
            size += info.st_size
    return count, size


def clean_dir(root: Path, cutoff: float) -> tuple[int, int, int]:
    """Delete regular files below root modified before `cutoff`: (removed, bytes, skipped)."""
    removed = freed = skipped = 0
    for path in _walk_files(root):
        info = _stat(path)
        if info is None or info.st_mtime >= cutoff:
            continue
        try:
            os.remove(path)
        except OSError:
            skipped += 1
            continue
        removed += 1
        freed += info.st_size
    return removed, freed, skipped


def prune_empty_subdirs(root: Path) -> None:
    """Remove the directories below root that hold nothing (root itself stays)."""
    try:
        if not root.is_dir() or root.is_symlink():
            return
    except OSError:
        return
    for directory, _subdirs, _names in os.walk(root, topdown=False, followlinks=False):
        if directory == str(root) or os.path.islink(directory):
            continue
        try:
            os.rmdir(directory)  # fails (and is ignored) while anything is left inside
        except OSError:
            pass


def _cover_temp_dirs(temp_root: Path) -> list[Path]:
    """`stbridge_cover_*` directories directly inside the system temp directory (nothing deeper)."""
    try:
        entries = list(os.scandir(temp_root))
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            if entry.name.startswith(COVER_TEMP_PREFIX) and entry.is_dir(follow_symlinks=False):
                found.append(Path(entry.path))
        except OSError:
            continue
    return found


def measure_cover_temp(temp_root: Path) -> tuple[int, int]:
    count = size = 0
    for directory in _cover_temp_dirs(temp_root):
        files, nbytes = measure_dir(directory)
        count += files
        size += nbytes
    return count, size


def clean_cover_temp(temp_root: Path, now: float) -> tuple[int, int, int]:
    """Remove worker directories untouched for an hour: (files removed, bytes, directories skipped)."""
    removed = freed = skipped = 0
    for directory in _cover_temp_dirs(temp_root):
        try:
            if directory.stat().st_mtime >= now - MIN_AGE_SECONDS:
                continue
        except OSError:
            continue
        files, nbytes = measure_dir(directory)
        try:
            shutil.rmtree(directory)
        except OSError:
            skipped += 1
            continue
        removed += files
        freed += nbytes
    return removed, freed, skipped


class CleanupService:
    def __init__(
        self,
        *,
        data_dir: Path,
        get_settings: Callable[[], dict[str, Any]],
        logger: Any,
        astrbot_temp: Callable[[], Path | None],
        bridge_temp: Callable[[], Path | None],
        cover_stems: Callable[[], Path | None] = lambda: None,
        system_temp: Callable[[], Path] = lambda: Path(tempfile.gettempdir()),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_path = Path(data_dir) / STATE_FILE
        self._get_settings = get_settings
        self._logger = logger
        self._astrbot_temp = astrbot_temp
        self._bridge_temp = bridge_temp
        self._cover_stems = cover_stems
        self._system_temp = system_temp
        self._clock = clock
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ---- state

    def last_auto(self) -> dict[str, Any]:
        """{time, removed, bytes, skipped} of the last successful automatic run, or {} when there was none."""
        state = _read_json(self._state_path).get("last_auto")
        if not isinstance(state, dict) or isinstance(state.get("time"), bool) or not isinstance(state.get("time"), (int, float)):
            return {}
        return {key: state.get(key, 0) for key in ("time", "removed", "bytes", "skipped")}

    def _save_auto(self, result: dict[str, Any]) -> None:
        _write_json(self._state_path, {"last_auto": {
            "time": self._clock(), "removed": result["removed"], "bytes": result["bytes"], "skipped": result["skipped"],
        }})

    def auto_due(self) -> bool:
        if not self._get_settings()["enabled"]:
            return False
        last = self.last_auto()
        return not last or self._clock() - float(last["time"]) >= AUTO_INTERVAL_SECONDS or float(last["time"]) > self._clock()

    # ---- measuring and cleaning (blocking; callers use a thread)

    def _paths(self) -> dict[str, Path | None]:
        return {
            "astrbot_temp": self._astrbot_temp(), "bridge_temp": self._bridge_temp(),
            "cover_stems": self._cover_stems(),
        }

    def stats(self) -> dict[str, dict[str, int]]:
        result = {}
        for name, path in self._paths().items():
            files, nbytes = measure_dir(path) if path is not None else (0, 0)
            result[name] = {"files": files, "bytes": nbytes}
        files, nbytes = measure_cover_temp(Path(self._system_temp()))
        result["cover_temp"] = {"files": files, "bytes": nbytes}
        return result

    def clean(self) -> dict[str, Any]:
        """One pass over every target with the configured keep time."""
        now = self._clock()
        cutoff = cutoff_time(now, self._get_settings()["keep_days"])
        removed = freed = skipped = 0
        by_target = {}
        for name, path in self._paths().items():
            counts = clean_dir(path, cutoff) if path is not None else (0, 0, 0)
            if name == "cover_stems" and path is not None:
                prune_empty_subdirs(path)
            by_target[name] = {"removed": counts[0], "bytes": counts[1], "skipped": counts[2]}
            removed, freed, skipped = removed + counts[0], freed + counts[1], skipped + counts[2]
        counts = clean_cover_temp(Path(self._system_temp()), now)
        by_target["cover_temp"] = {"removed": counts[0], "bytes": counts[1], "skipped": counts[2]}
        return {
            "removed": removed + counts[0], "bytes": freed + counts[1], "skipped": skipped + counts[2],
            "targets": by_target,
        }

    async def run(self, source: str) -> dict[str, Any]:
        """Clean now. `source` is "auto" or "manual"; only a successful auto run is remembered."""
        async with self._run_lock:
            result = await asyncio.to_thread(self.clean)
            if source == "auto":
                await asyncio.to_thread(self._save_auto, result)
        self._log(f"[STBridge] Cleanup {source} removed={result['removed']} bytes={result['bytes']} skipped={result['skipped']}")
        return result

    async def overview(self) -> dict[str, Any]:
        return {"stats": await asyncio.to_thread(self.stats), "last_auto": await asyncio.to_thread(self.last_auto)}

    # ---- automatic loop

    def start(self, interval: float = CHECK_SECONDS) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def check_once(self) -> bool:
        """Run the automatic cleanup when it is due; True if it ran."""
        if not await asyncio.to_thread(self.auto_due):
            return False
        await self.run("auto")
        return True

    async def _loop(self, interval: float) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log(f"[STBridge] Cleanup auto failed ({type(error).__name__})", "error")
            await asyncio.sleep(interval)

    def _log(self, message: str, level: str = "info") -> None:
        try:
            getattr(self._logger, level)(message)
        except Exception:
            pass
