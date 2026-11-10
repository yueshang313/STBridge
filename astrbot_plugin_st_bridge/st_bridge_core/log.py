"""The core's logger: a thin proxy whose target the host sets (default: the standard logging module).

Run as its own process, the core logs to its standard output (one line per record, ``LEVEL<TAB>message``, which
the process that started it forwards to its own log) and to ``logs/bridge.log`` in the data directory, a new
file every day, seven days kept.
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = "logs"
LOG_FILE = "bridge.log"
LOG_KEEP_DAYS = 7


class _Logger:
    def __init__(self) -> None:
        self._target = logging.getLogger("st_bridge_core")

    def set_target(self, target) -> None:
        self._target = target

    def info(self, message) -> None:
        self._target.info(message)

    def warning(self, message) -> None:
        self._target.warning(message)

    def error(self, message) -> None:
        self._target.error(message)


logger = _Logger()


def set_logger(target) -> None:
    """Route every core log line to ``target`` (anything with info/warning/error)."""
    logger.set_target(target)


def configure_process_logging(data_dir: Path, *, stream=None) -> Path:
    """Log to stdout and to the daily rotated file of ``data_dir``; returns the log file path."""
    directory = Path(data_dir) / LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOG_FILE
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(stream if stream is not None else sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)s\t%(message)s"))
    root.addHandler(console)
    file_handler = TimedRotatingFileHandler(
        path, when="midnight", backupCount=LOG_KEEP_DAYS, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(file_handler)
    return path
