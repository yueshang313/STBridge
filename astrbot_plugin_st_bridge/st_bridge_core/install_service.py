"""R4b: what the "安装与路径" page does on the computer.

* check the paths the user typed (SillyTavern, AstrBot, NapCat, GPT-SoVITS, Irodori, RVC),
* install / reinstall / uninstall the AstrBot plugin and the SillyTavern extension from the release package
  (``addons/``), always backing up what is replaced or removed,
* import the data of an older plugin (``plugin_data``), read-only on the source, with a backup of what it replaces,
* install extra dependencies into other programs' Pythons (pedalboard for RVC) as one background job,
* start NapCat's launcher.

Everything that deletes or replaces something works on a fixed folder name inside a folder this module worked out
itself (``<plugins>/astrbot_plugin_st_bridge``, ``<extensions>/st_bridge_client``); a request never names a path to
delete. Files of ST, AstrBot and NapCat are not read except ST's ``config.yaml`` (for its ``dataRoot``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .emotion_config import EmotionConfigError
from .version import CORE_VERSION, compute_build_id

PLUGIN_DIR_NAME = "astrbot_plugin_st_bridge"
EXTENSION_DIR_NAME = "st_bridge_client"
LINK_FILE = "core_link.json"
BACKUPS_DIR = "backups"
LAUNCHER_SUFFIXES = (".exe", ".bat", ".cmd")
# Never imported from an old data folder: logs, caches and backups.
IMPORT_SKIPPED_NAMES = frozenset({"logs", "temp_audio", "cover_stems", BACKUPS_DIR, "__pycache__"})
IMPORT_SKIPPED_SUFFIXES = (".log", ".log.1")
_CREATE_NEW_CONSOLE = 0x00000010
_CREATE_NO_WINDOW = 0x08000000
INSTALL_TIMEOUT_SECONDS = 600
CHECK_TIMEOUT_SECONDS = 30
MAX_OUTPUT_LINES = 20

EMPTY, VALID, INVALID, CHOOSE = "empty", "valid", "invalid", "choose"


def _result(state: str, message: str = "", **extra: Any) -> dict[str, Any]:
    return {"state": state, "message": message, **extra}


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _timestamp(clock: Callable[[], datetime]) -> str:
    return clock().strftime("%Y%m%d-%H%M%S")


# ---- the paths ---------------------------------------------------------------------------------------
_DATA_ROOT_RE = re.compile(r"^\s*dataRoot\s*:\s*(.+?)\s*$", re.MULTILINE)


def _data_root_from_config(st_dir: Path) -> Path | None:
    """ST's ``dataRoot`` from its config.yaml (the only ST file read), resolved against the ST folder."""
    try:
        text = (st_dir / "config.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _DATA_ROOT_RE.search(text)
    if not match:
        return None
    value = match.group(1).split(" #")[0].strip().strip("'\"")
    if not value:
        return None
    root = Path(value)
    return root if root.is_absolute() else (st_dir / root)


def find_st_user_dir(path: Path) -> Path | None:
    """``default-user`` of the ST data folder, from the ST program folder or the data folder."""
    candidates = [path / "data" / "default-user", path / "default-user"]
    root = _data_root_from_config(path)
    if root is not None:
        candidates.append(root / "default-user")
    candidates += [path / "st_data" / "default-user", path / "data" / "st_data" / "default-user"]
    candidates.append(path.parent / "st_data" / "default-user")  # the launcher keeps the data beside the program
    for candidate in candidates:
        if _is_dir(candidate):
            return candidate
    return None


def check_st(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    path = Path(text)
    if not _is_dir(path):
        return _result(INVALID, "目录不存在")
    user_dir = find_st_user_dir(path)
    if user_dir is None:
        return _result(INVALID, "请选包含 default-user 的 ST 数据目录")
    return _result(VALID, f"扩展目录：{user_dir / 'extensions'}", extensions_dir=str(user_dir / "extensions"))


def find_plugins_dir(path: Path) -> Path | None:
    if path.name.lower() == "plugins" and _is_dir(path):
        return path
    for candidate in (path / "data" / "plugins", path / "core" / "data" / "plugins"):
        if _is_dir(candidate):
            return candidate
    return None


def check_astrbot(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    path = Path(text)
    if not _is_dir(path):
        return _result(INVALID, "目录不存在")
    plugins = find_plugins_dir(path)
    if plugins is None:
        return _result(INVALID, "没找到 data\\plugins 或 core\\data\\plugins")
    return _result(
        VALID, f"插件目录：{plugins}", plugins_dir=str(plugins),
        plugin_data_dir=str(plugins.parent / "plugin_data" / PLUGIN_DIR_NAME),
    )


def list_launchers(directory: Path) -> list[str]:
    """Names of the .exe/.bat files directly inside a folder (file names only; nothing is opened)."""
    try:
        with os.scandir(directory) as entries:
            names = [entry.name for entry in entries if entry.name.lower().endswith(LAUNCHER_SUFFIXES) and entry.is_file()]
    except OSError:
        return []
    return sorted(names, key=str.casefold)


def check_napcat(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    path = Path(text)
    if _is_file(path):
        if path.name.lower().endswith(LAUNCHER_SUFFIXES):
            return _result(VALID, f"启动文件：{path.name}", launcher=str(path))
        return _result(INVALID, "请选 .exe 或 .bat 启动文件")
    if _is_dir(path):
        names = list_launchers(path)
        if not names:
            return _result(INVALID, "这个目录里没有 .exe / .bat 启动文件")
        return _result(CHOOSE, "请选一个启动文件", candidates=[str(path / name) for name in names])
    return _result(INVALID, "路径不存在")


def check_gpt_sovits(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    root = Path(text)
    if not _is_dir(root):
        return _result(INVALID, "目录不存在")
    if not _is_file(root / "runtime" / "python.exe"):
        return _result(INVALID, "找不到 runtime\\python.exe")
    if not _is_file(root / "api.py"):
        return _result(INVALID, "找不到 api.py")
    return _result(VALID, "runtime\\python.exe 和 api.py 都在")


def check_irodori(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    root = Path(text)
    if not _is_dir(root):
        return _result(INVALID, "目录不存在")
    if not _is_file(root / ".venv" / "Scripts" / "python.exe"):
        return _result(INVALID, "找不到 .venv\\Scripts\\python.exe")
    return _result(VALID, ".venv\\Scripts\\python.exe 在")


def check_irodori_server(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    root = Path(text)
    if not _is_dir(root):
        return _result(INVALID, "目录不存在")
    if not _is_file(root / "src" / "irodori_openai_tts" / "__main__.py"):
        return _result(INVALID, "找不到 src\\irodori_openai_tts\\__main__.py")
    return _result(VALID, "服务端文件在")


def check_rvc(text: str) -> dict[str, Any]:
    if not text:
        return _result(EMPTY)
    root = Path(text)
    if not _is_dir(root):
        return _result(INVALID, "目录不存在")
    if not _is_file(root / "runtime" / "python.exe"):
        return _result(INVALID, "找不到 runtime\\python.exe")
    if not _is_dir(root / "assets" / "weights"):
        return _result(INVALID, "找不到 assets\\weights（RVC 2026 整合包的声音模型目录）")
    return _result(VALID, "runtime\\python.exe 和 assets\\weights 都在")


PATH_CHECKS: dict[str, Callable[[str], dict[str, Any]]] = {
    "st": check_st,
    "astrbot": check_astrbot,
    "napcat": check_napcat,
    "gpt_sovits": check_gpt_sovits,
    "irodori": check_irodori,
    "irodori_server": check_irodori_server,
    "rvc": check_rvc,
}


def check_paths(values: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The state of every path: ``values`` has st, astrbot, napcat, gpt_sovits, irodori, irodori_server, rvc."""
    return {name: check(values.get(name, "") or "") for name, check in PATH_CHECKS.items()}


# ---- copying ------------------------------------------------------------------------------------------
def _ignore_junk(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def tree_digest(root: Path) -> str:
    """A hash of the files of a folder (relative names and content; caches and tests left out)."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root)
        if {"__pycache__", "tests"} & set(relative.parts) or not path.is_file() or path.suffix in (".pyc", ".pyo"):
            continue
        if relative.as_posix() == LINK_FILE:
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


class InstallError(EmotionConfigError):
    """A refusal with a message for the page (HTTP 400/404/409/500)."""


# ---- the service ---------------------------------------------------------------------------------------
@dataclass
class Dependency:
    """An extra package installed into another program's Python."""

    name: str
    label: str
    python: Callable[[dict[str, Any]], str]       # the python.exe it goes into, from the console config
    check_args: list[str]                         # run with that python; exit code 0 = installed
    install_args: Callable[[str], list[str]]      # given the pip mirror
    needs: str = ""                               # what must be set up first (shown when the python is unknown)
    note: str = ""


def _pedalboard_python(config: dict[str, Any]) -> str:
    cover = config.get("cover") if isinstance(config.get("cover"), dict) else {}
    root = str(cover.get("rvc_root") or "").strip()
    return str(Path(root) / "runtime" / "python.exe") if root else ""


def _pip_install(package: str) -> Callable[[str], list[str]]:
    def build(mirror: str) -> list[str]:
        args = ["-m", "pip", "install", "--no-deps", package]
        if mirror:
            args += ["-i", mirror]
        return args

    return build


DEFAULT_DEPENDENCIES = (
    Dependency(
        name="pedalboard",
        label="pedalboard（伴奏变调）",
        python=_pedalboard_python,
        check_args=["-c", "import pedalboard"],
        install_args=_pip_install("pedalboard"),
        needs="请先设置 RVC 目录",
        note="装进 RVC 自带的 Python（<RVC>\\runtime\\python.exe）",
    ),
)


@dataclass
class DepJob:
    name: str
    state: str = "running"     # running | success | failed
    lines: list[str] = field(default_factory=list)
    returncode: int | None = None
    started: float = 0.0
    message: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "state": self.state, "lines": list(self.lines),
            "returncode": self.returncode, "message": self.message,
        }


class InstallService:
    def __init__(
        self,
        data_dir: Path,
        *,
        addons_dir: Path | None = None,
        clock: Callable[[], datetime] = datetime.now,
        popen: Callable[..., Any] = subprocess.Popen,
        startfile: Callable[..., Any] | None = getattr(os, "startfile", None),
        dependencies: tuple[Dependency, ...] = DEFAULT_DEPENDENCIES,
        logger: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.addons_dir = Path(addons_dir) if addons_dir is not None else self.default_addons_dir()
        self._clock = clock
        self._popen = popen
        self._startfile = startfile
        self.dependencies = {item.name: item for item in dependencies}
        self._logger = logger
        self._job: DepJob | None = None
        self._job_task: asyncio.Task | None = None
        self._last_jobs: dict[str, DepJob] = {}

    @staticmethod
    def default_addons_dir() -> Path | None:
        """``<release>/addons`` when this core runs from a release folder; None in the development layout."""
        package = Path(__file__).resolve().parent
        if package.parent.name == "core":
            candidate = package.parent.parent / "addons"
            if _is_dir(candidate):
                return candidate
        return None

    # ---- the package -----------------------------------------------------------------------------
    @property
    def plugin_source(self) -> Path | None:
        return self.addons_dir / PLUGIN_DIR_NAME if self.addons_dir else None

    @property
    def extension_source(self) -> Path | None:
        return self.addons_dir / EXTENSION_DIR_NAME if self.addons_dir else None

    def package_info(self) -> dict[str, Any]:
        plugin_ok = self.plugin_source is not None and _is_file(self.plugin_source / "metadata.yaml") and _is_file(self.plugin_source / "main.py")
        extension_ok = self.extension_source is not None and _is_file(self.extension_source / "manifest.json")
        return {"available": bool(plugin_ok and extension_ok), "plugin": plugin_ok, "extension": extension_ok, "version": CORE_VERSION}

    def _require_source(self, kind: str) -> Path:
        info = self.package_info()
        if self.addons_dir is None or not info[kind]:
            raise InstallError(409, "开发环境，没有可安装的文件" if self.addons_dir is None else "发布包里的安装文件不完整")
        return self.plugin_source if kind == "plugin" else self.extension_source  # type: ignore[return-value]

    # ---- backups ---------------------------------------------------------------------------------
    def _backup_dir(self, label: str) -> Path:
        base = self.data_dir / BACKUPS_DIR
        target = base / f"{label}-{_timestamp(self._clock)}"
        counter = 1
        while target.exists():
            counter += 1
            target = base / f"{label}-{_timestamp(self._clock)}-{counter}"
        return target

    @staticmethod
    def _copy_folder(source: Path, target: Path) -> None:
        shutil.copytree(source, target, ignore=_ignore_junk)

    # ---- plugin / extension (shared) -------------------------------------------------------------
    def _place(self, source: Path, target: Path, backup_label: str, after: Callable[[Path], None] | None = None) -> dict[str, Any]:
        """Replace ``target`` (a fixed-name folder inside the folder worked out from the saved path) by ``source``."""
        backup: Path | None = None
        if target.exists():
            backup = self._backup_dir(backup_label)
            backup.parent.mkdir(parents=True, exist_ok=True)
            self._copy_folder(target, backup)
            self._remove_tree(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._copy_folder(source, target)
            if after is not None:
                after(target)
        except Exception as error:
            # Put back what was there.
            if target.exists():
                self._remove_tree(target)
            if backup is not None:
                self._copy_folder(backup, target)
            raise InstallError(500, f"安装失败（{type(error).__name__}）{'，已恢复原来的文件' if backup else ''}") from error
        return {"backup": str(backup) if backup else None, "target": str(target)}

    @staticmethod
    def _remove_tree(target: Path) -> None:
        if target.is_symlink():
            target.unlink()
        else:
            shutil.rmtree(target)

    def _remove_with_backup(self, target: Path, backup_label: str) -> dict[str, Any]:
        if not target.exists():
            raise InstallError(404, "没有安装")
        backup = self._backup_dir(backup_label)
        backup.parent.mkdir(parents=True, exist_ok=True)
        self._copy_folder(target, backup)
        self._remove_tree(target)
        return {"backup": str(backup), "target": str(target)}

    # ---- the AstrBot plugin ----------------------------------------------------------------------
    def plugin_target(self, astrbot_check: dict[str, Any]) -> Path:
        if astrbot_check.get("state") != VALID:
            raise InstallError(400, "先在上面填好 AstrBot 路径并保存")
        return Path(astrbot_check["plugins_dir"]) / PLUGIN_DIR_NAME

    def plugin_status(self, astrbot_check: dict[str, Any]) -> dict[str, Any]:
        if astrbot_check.get("state") != VALID:
            return {"state": "unknown", "message": "还没有设置 AstrBot 路径"}
        target = Path(astrbot_check["plugins_dir"]) / PLUGIN_DIR_NAME
        if not _is_file(target / "main.py"):
            return {"state": "missing", "message": "未安装", "target": str(target)}
        info: dict[str, Any] = {"target": str(target)}
        link = self._read_link(target)
        info["linked"] = link is not None and Path(link).resolve() == self.data_dir if link else False
        source = self.plugin_source
        outdated = False
        if source is not None and _is_dir(source / "st_bridge_core") and _is_dir(target / "st_bridge_core"):
            try:
                outdated = compute_build_id(source / "st_bridge_core") != compute_build_id(target / "st_bridge_core")
            except OSError:
                outdated = True
        if outdated:
            return {"state": "outdated", "message": "已安装，但和这个发布包不是同一版，建议重装", **info}
        if not info["linked"]:
            return {"state": "unlinked", "message": "已安装，但没有指向当前的数据目录，建议重装", **info}
        return {"state": "installed", "message": "已安装", **info}

    @staticmethod
    def _read_link(plugin_dir: Path) -> str | None:
        try:
            value = json.loads((plugin_dir / LINK_FILE).read_text(encoding="utf-8")).get("data_dir")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and value else None

    def install_plugin(self, astrbot_check: dict[str, Any]) -> dict[str, Any]:
        source = self._require_source("plugin")
        target = self.plugin_target(astrbot_check)
        data_dir = self.data_dir

        def write_link(placed: Path) -> None:
            (placed / LINK_FILE).write_text(json.dumps({"data_dir": str(data_dir)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        result = self._place(source, target, "astrbot-plugin", write_link)
        result["message"] = "已安装。请在 AstrBot 里重载插件或重启 AstrBot"
        return result

    def uninstall_plugin(self, astrbot_check: dict[str, Any]) -> dict[str, Any]:
        result = self._remove_with_backup(self.plugin_target(astrbot_check), "astrbot-plugin")
        result["message"] = "已卸载（原文件在备份里）。请在 AstrBot 里重启或刷新插件列表"
        return result

    # ---- the SillyTavern extension ---------------------------------------------------------------
    def extension_target(self, st_check: dict[str, Any]) -> Path:
        if st_check.get("state") != VALID:
            raise InstallError(400, "先在上面填好 ST 路径并保存")
        return Path(st_check["extensions_dir"]) / EXTENSION_DIR_NAME

    def extension_status(self, st_check: dict[str, Any]) -> dict[str, Any]:
        if st_check.get("state") != VALID:
            return {"state": "unknown", "message": "还没有设置 ST 路径"}
        target = Path(st_check["extensions_dir"]) / EXTENSION_DIR_NAME
        if not _is_file(target / "manifest.json"):
            return {"state": "missing", "message": "未安装", "target": str(target)}
        source = self.extension_source
        if source is not None and _is_dir(source):
            try:
                if tree_digest(source) != tree_digest(target):
                    return {"state": "outdated", "message": "已安装，但和这个发布包不是同一版，建议重装", "target": str(target)}
            except OSError:
                pass
        return {"state": "installed", "message": "已安装", "target": str(target)}

    def install_extension(self, st_check: dict[str, Any]) -> dict[str, Any]:
        source = self._require_source("extension")
        result = self._place(source, self.extension_target(st_check), "st-extension")
        result["message"] = "已安装。请刷新 ST 页面"
        return result

    def uninstall_extension(self, st_check: dict[str, Any]) -> dict[str, Any]:
        result = self._remove_with_backup(self.extension_target(st_check), "st-extension")
        result["message"] = "已卸载（原文件在备份里）。请刷新 ST 页面"
        return result

    # ---- importing an older plugin's data --------------------------------------------------------
    @staticmethod
    def _skipped(name: str) -> bool:
        return name in IMPORT_SKIPPED_NAMES or name.endswith(IMPORT_SKIPPED_SUFFIXES)

    def _check_source(self, source_text: str) -> Path:
        if not isinstance(source_text, str) or not source_text.strip():
            raise InstallError(400, "请选择旧插件的数据目录")
        source = Path(source_text.strip().strip('"'))
        if not _is_dir(source):
            raise InstallError(404, "目录不存在")
        resolved, own = source.resolve(), self.data_dir
        if resolved == own or own in resolved.parents or resolved in own.parents:
            raise InstallError(400, "来源不能是当前数据目录（或它的上级、下级）")
        if not any(_is_file(source / name) for name in ("config.json", "affinity.json", "connector_token")):
            raise InstallError(400, "这不像旧插件的数据目录（没有 config.json）")
        return source

    @staticmethod
    def _size_of(path: Path) -> tuple[int, int]:
        if path.is_file():
            return path.stat().st_size, 1
        total = count = 0
        for sub in path.rglob("*"):
            if sub.is_file() and not (InstallService._skipped(sub.name) and sub.parent == path):
                try:
                    total += sub.stat().st_size
                    count += 1
                except OSError:
                    pass
        return total, count

    def default_import_source(self, astrbot_check: dict[str, Any]) -> str:
        return astrbot_check.get("plugin_data_dir", "") if astrbot_check.get("state") == VALID else ""

    def import_preview(self, source_text: str) -> dict[str, Any]:
        source = self._check_source(source_text)
        items = []
        with os.scandir(source) as entries:
            for entry in sorted(entries, key=lambda e: e.name.casefold()):
                if self._skipped(entry.name):
                    continue
                path = Path(entry.path)
                size, count = self._size_of(path)
                items.append({
                    "name": entry.name, "kind": "dir" if entry.is_dir() else "file", "size": size, "files": count,
                    "exists": (self.data_dir / entry.name).exists(),
                })
        skipped = sorted(entry for entry in os.listdir(source) if self._skipped(entry))
        rewrites = self._count_path_references(source)
        return {
            "source": str(source), "items": items, "skipped": skipped,
            "total_size": sum(item["size"] for item in items),
            "replaces": [item["name"] for item in items if item["exists"]],
            "path_references": rewrites,
        }

    @staticmethod
    def _walk_strings(value: Any, visit: Callable[[str], str]) -> Any:
        if isinstance(value, str):
            return visit(value)
        if isinstance(value, list):
            return [InstallService._walk_strings(item, visit) for item in value]
        if isinstance(value, dict):
            return {key: InstallService._walk_strings(item, visit) for key, item in value.items()}
        return value

    @staticmethod
    def _prefix_matcher(source: Path):
        """A function that moves a path under ``source`` to the same place under the new data folder (or None)."""
        base = os.path.normcase(os.path.normpath(str(source)))

        def relative(text: str) -> str | None:
            if not text or "\n" in text or len(text) > 1000:
                return None
            normal = os.path.normcase(os.path.normpath(text))
            if normal == base:
                return ""
            if normal.startswith(base + os.sep):
                return os.path.normpath(text)[len(base) + 1:]
            return None

        return relative

    def _count_path_references(self, source: Path) -> int:
        try:
            config = json.loads((source / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        relative = self._prefix_matcher(source)
        found = 0

        def visit(text: str) -> str:
            nonlocal found
            if relative(text) is not None:
                found += 1
            return text

        self._walk_strings(config, visit)
        return found

    def import_data(self, source_text: str) -> dict[str, Any]:
        source = self._check_source(source_text)
        names = [entry for entry in sorted(os.listdir(source)) if not self._skipped(entry)]
        if not names:
            raise InstallError(400, "没有可导入的内容")
        local_install = self._local_install_section()
        backup_root = self._backup_dir("pre-import")
        backed_up: list[str] = []
        for name in names:
            existing = self.data_dir / name
            if existing.exists():
                backup_root.mkdir(parents=True, exist_ok=True)
                if existing.is_dir():
                    shutil.copytree(existing, backup_root / name)
                else:
                    shutil.copy2(existing, backup_root / name)
                backed_up.append(name)
        copied: list[str] = []
        for name in names:
            origin, target = source / name, self.data_dir / name
            if origin.is_dir():
                shutil.copytree(origin, target, dirs_exist_ok=True, ignore=self._import_ignore)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, target)
            copied.append(name)
        rewritten, kept_install = (0, False)
        if "config.json" in copied:
            rewritten, kept_install = self._finish_config(source, local_install)
        return {
            "copied": copied, "backup": str(backup_root) if backed_up else None, "backed_up": backed_up,
            "rewritten_paths": rewritten, "kept_install": kept_install, "restart_required": True,
        }

    @staticmethod
    def _import_ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if InstallService._skipped(name) or name.endswith((".pyc", ".pyo"))}

    def _local_install_section(self) -> dict[str, Any] | None:
        """This machine's ST / AstrBot / NapCat paths from the current config.json, when any is set."""
        try:
            section = json.loads((self.data_dir / "config.json").read_text(encoding="utf-8")).get("install")
        except (OSError, ValueError, AttributeError):
            return None
        keys = ("st_dir", "astrbot_dir", "napcat_path")
        if isinstance(section, dict) and any(isinstance(section.get(key), str) and section[key].strip() for key in keys):
            return section
        return None

    def _finish_config(self, source: Path, local_install: dict[str, Any] | None) -> tuple[int, bool]:
        """After copying config.json: keep this machine's install paths, then fix paths into the old data folder."""
        path = self.data_dir / "config.json"
        kept = False
        if local_install is not None:
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(config, dict):
                    config["install"] = local_install
                    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                    kept = True
            except (OSError, ValueError):
                pass
        return self._rewrite_config_paths(source), kept

    def _rewrite_config_paths(self, source: Path) -> int:
        """Point absolute paths into the old data folder at the new one (reference audio and the like)."""
        path = self.data_dir / "config.json"
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        relative = self._prefix_matcher(source)
        changed = 0

        def visit(text: str) -> str:
            nonlocal changed
            tail = relative(text)
            if tail is None:
                return text
            changed += 1
            return str(self.data_dir / tail) if tail else str(self.data_dir)

        rewritten = self._walk_strings(config, visit)
        if changed:
            path.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

    # ---- extra dependencies ----------------------------------------------------------------------
    def dependency_rows(self, config: dict[str, Any], mirror: str, *, check: bool = True) -> list[dict[str, Any]]:
        rows = []
        for item in self.dependencies.values():
            python = item.python(config)
            row: dict[str, Any] = {
                "name": item.name, "label": item.label, "note": item.note, "python": python,
                "needs": "" if python else item.needs,
                "command": " ".join([f'"{python}"' if python else "<python>", *item.install_args(mirror)]),
            }
            if not python:
                row["state"] = "blocked"
            elif not _is_file(Path(python)):
                row["state"], row["needs"] = "blocked", f"找不到 {python}"
            elif check:
                row["state"] = self._dependency_state(item, python)
            else:
                row["state"] = "unknown"
            rows.append(row)
        return rows

    def _dependency_state(self, item: Dependency, python: str) -> str:
        try:
            done = subprocess.run(
                [python, *item.check_args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=CHECK_TIMEOUT_SECONDS, creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return "installed" if done.returncode == 0 else "missing"

    def job_status(self) -> dict[str, Any] | None:
        return self._job.public() if self._job is not None else None

    @property
    def job_running(self) -> bool:
        return self._job is not None and self._job.state == "running"

    def start_dependency(self, name: str, config: dict[str, Any], mirror: str) -> dict[str, Any]:
        item = self.dependencies.get(name)
        if item is None:
            raise InstallError(404, "unknown dependency")
        if self.job_running:
            raise InstallError(409, "已经有一个安装任务在进行")
        python = item.python(config)
        if not python:
            raise InstallError(400, item.needs or "还不能安装")
        if not _is_file(Path(python)):
            raise InstallError(400, f"找不到 {python}")
        job = DepJob(name=name, started=time.monotonic())
        self._job = job
        self._job_task = asyncio.create_task(self._run_job(job, [python, *item.install_args(mirror)]))
        return job.public()

    async def _run_job(self, job: DepJob, command: list[str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as error:
            job.state, job.message = "failed", f"无法启动（{type(error).__name__}）"
            return

        async def pump() -> None:
            assert process.stdout is not None
            while True:
                try:
                    raw = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    continue
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    job.lines.append(line[:300])
                    del job.lines[:-MAX_OUTPUT_LINES]

        try:
            await asyncio.wait_for(asyncio.gather(pump(), process.wait()), INSTALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            job.state, job.message, job.returncode = "failed", f"超过 {INSTALL_TIMEOUT_SECONDS} 秒，已中止", process.returncode
            return
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            job.state, job.message = "failed", "已取消"
            raise
        job.returncode = process.returncode
        job.state = "success" if process.returncode == 0 else "failed"
        job.message = "安装完成" if job.state == "success" else f"安装失败（退出码 {process.returncode}）"

    async def wait_job(self) -> None:
        task = self._job_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def terminate(self) -> None:
        task, self._job_task = self._job_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._job is not None and self._job.state == "running":  # cancelled before it could say so itself
            self._job.state, self._job.message = "failed", "已取消"

    # ---- NapCat ----------------------------------------------------------------------------------
    def start_napcat(self, napcat_check: dict[str, Any]) -> dict[str, Any]:
        if napcat_check.get("state") != VALID:
            raise InstallError(400, "先选好 NapCat 的启动文件并保存")
        launcher = Path(napcat_check["launcher"])
        try:
            if sys.platform == "win32" and self._startfile is not None:
                # Same as double-clicking it: the Windows shell opens its own console with real standard handles.
                # (Redirecting them to NUL swallows what NapCat's launcher relays from its log pipe.)
                self._startfile(str(launcher), cwd=str(launcher.parent))
            else:
                self._popen([str(launcher)], cwd=str(launcher.parent), close_fds=True, creationflags=0)
        except OSError as error:
            raise InstallError(500, f"无法启动 NapCat（{type(error).__name__}）") from error
        return {"message": f"已启动 {launcher.name}"}
