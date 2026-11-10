"""Versions of the core and of the connector protocol, and the build id of the code on disk."""

import hashlib
from functools import lru_cache
from pathlib import Path

# Bumped whenever the core's behavior changes in a way a connector has to match (checked in the hello).
CORE_VERSION = "1.0.0"
PROTOCOL_VERSION = 1
# The exit code of a core that stops because the console asked for a restart; start.bat starts it again on this code.
RESTART_EXIT_CODE = 75

_SKIPPED_DIRECTORIES = {"__pycache__", "tests"}


def compute_build_id(root: Path | None = None) -> str:
    """A hash of the core's code on disk: every .py file of the package and every console front-end file
    (tests and caches left out), by relative path and content.

    The version number is not bumped on every deployment, so a running core and the plugin that wants to use it
    compare this instead: the same value means the same code.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if _SKIPPED_DIRECTORIES & set(relative.parts) or not path.is_file():
            continue
        if path.suffix == ".pyc" or (relative.parts[0] != "console_frontend" and path.suffix != ".py"):
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


@lru_cache(maxsize=1)
def running_build_id() -> str:
    """The build id of the code this process loaded (computed once; the files do not change under a running core)."""
    return compute_build_id()
