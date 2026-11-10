"""Reading the core's log files for the console: the latest lines, and a zip of every kept file.

Only files that the core's own daily rotation produces (``bridge.log`` and ``bridge.log.YYYY-MM-DD``) inside the
``logs`` directory of the data directory are ever read; nothing here takes a path from a request.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .log import LOG_DIR, LOG_FILE

LINE_CHOICES = (100, 300, 1000)
DEFAULT_LINES = 300
# Reading the tail never takes more than this from the end of the file.
TAIL_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 64 * 1024 * 1024
_ROTATED = re.compile(r"^" + re.escape(LOG_FILE) + r"\.\d{4}-\d{2}-\d{2}$")


def log_directory(data_dir: Path) -> Path:
    return Path(data_dir) / LOG_DIR


def log_files(data_dir: Path) -> list[Path]:
    """The log files, oldest first (rotated days, then today's ``bridge.log``)."""
    directory = log_directory(data_dir)
    try:
        entries = [item for item in directory.iterdir() if item.is_file() and not item.is_symlink()]
    except OSError:
        return []
    rotated = sorted(item for item in entries if _ROTATED.fullmatch(item.name))
    current = [item for item in entries if item.name == LOG_FILE]
    return rotated + current


def clean_line_count(value: object) -> int:
    """The number of lines asked for: one of the offered choices, else the default."""
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_LINES
    return number if number in LINE_CHOICES else DEFAULT_LINES


def tail_lines(data_dir: Path, lines: int) -> list[str]:
    """The last ``lines`` lines of today's log (older days are in the download)."""
    path = log_directory(data_dir) / LOG_FILE
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    result = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
    return result[-lines:]


def zip_logs(data_dir: Path) -> bytes:
    """Every log file in one zip (the newest files first when the size limit is reached)."""
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in reversed(log_files(data_dir)):
            try:
                size = path.stat().st_size
                if total + size > MAX_ZIP_BYTES:
                    continue
                archive.write(path, arcname=path.name)
                total += size
            except OSError:
                continue
    return buffer.getvalue()
