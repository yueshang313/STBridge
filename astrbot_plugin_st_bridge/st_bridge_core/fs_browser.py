"""Read-only directory listing for the console's path picker.

Lists names only; file contents are never read. Errors carry an HTTP-style
status through EmotionConfigError, like the rest of the console backend.
"""

from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Any, Iterable

from .emotion_config import MEDIA_TYPES, EmotionConfigError


BROWSE_MODES: dict[str, frozenset[str] | None] = {
    "dir": None,
    "ckpt": frozenset({".ckpt"}),
    "pth": frozenset({".pth"}),
    "audio": frozenset(MEDIA_TYPES),
    "launcher": frozenset({".exe", ".bat", ".cmd"}),
}
GPT_SOVITS_MARKER = "api.py"
_MAX_ROOT_SEARCH_DEPTH = 8


def list_drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.isdir(drive):
                drives.append(drive)
        except OSError:
            continue
    return drives


def find_gpt_sovits_root(paths: Iterable[Any]) -> Path | None:
    """Walk up from each model path; the first directory holding api.py wins."""
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            current = Path(value.strip()).expanduser()
            for directory in list(current.parents)[:_MAX_ROOT_SEARCH_DEPTH]:
                if (directory / GPT_SOVITS_MARKER).is_file():
                    return directory
        except (OSError, ValueError):
            continue
    return None


def start_directory(config: dict[str, Any]) -> Path | None:
    """Configured gpt_sovits_root, else the root inferred from model paths."""
    configured = config.get("gpt_sovits_root")
    if isinstance(configured, str) and configured.strip():
        try:
            path = Path(configured.strip()).expanduser()
            if path.is_dir():
                return path
        except (OSError, ValueError):
            pass
    model_paths = []
    models = config.get("models", {})
    if isinstance(models, dict):
        for model in models.values():
            if isinstance(model, dict):
                model_paths.extend((model.get("gpt_model_path"), model.get("sovits_model_path")))
    return find_gpt_sovits_root(model_paths)


def browse(path: Any, mode: Any, *, start: Path | None = None) -> dict[str, Any]:
    """List subdirectories and files matching mode; a file path lists its folder."""
    if not isinstance(mode, str) or mode not in BROWSE_MODES:
        raise EmotionConfigError(400, "invalid mode")
    suffixes = BROWSE_MODES[mode]
    drives = list_drives()
    if not isinstance(path, str) or not path.strip():
        if start is None:
            return {"path": "", "parent": None, "entries": [], "drives": drives, "selected": None}
        path = str(start)
    if "\x00" in path:
        raise EmotionConfigError(400, "invalid path")

    target = Path(os.path.normpath(os.path.expanduser(path.strip())))
    if not target.is_absolute():
        raise EmotionConfigError(400, "path must be absolute")
    selected = None
    try:
        if target.is_file():
            selected = str(target)
            target = target.parent
        elif not target.is_dir():
            raise EmotionConfigError(404, "directory not found")
        with os.scandir(target) as iterator:
            raw_entries = list(iterator)
    except EmotionConfigError:
        raise
    except PermissionError as exc:
        raise EmotionConfigError(403, "permission denied") from exc
    except OSError as exc:
        raise EmotionConfigError(404, "directory not readable") from exc

    entries = []
    for entry in raw_entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir and (
            suffixes is None or os.path.splitext(entry.name)[1].lower() not in suffixes
        ):
            continue
        entries.append({"name": entry.name, "path": str(target / entry.name), "is_dir": is_dir})
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    parent = target.parent
    return {
        "path": str(target),
        "parent": None if parent == target else str(parent),
        "entries": entries,
        "drives": drives,
        "selected": selected,
    }
