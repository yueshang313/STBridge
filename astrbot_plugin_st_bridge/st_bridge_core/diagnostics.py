"""The console's 诊断包 (U5-2): one zip a user can send when something is wrong.

It holds the recent logs, a snapshot of the states the console shows, the versions, and a *redacted* summary of the
settings. Redaction is on purpose blunt: only numbers, booleans and a short list of "kind of thing" words
(``ENUM_KEYS``) are kept as they are; every other text becomes "已设置" / "未设置"; lists and dictionaries of
names or numbers become counts. On top of that every value that looks like a secret, a path or an id is collected
from the settings (``sensitive_values``) and erased from every text that goes into the zip (the logs included).

Not in the zip, ever: chat text, affinity data, anything else from the data directory.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .log_files import MAX_ZIP_BYTES, log_files
from .version import CORE_VERSION, compute_build_id

# String settings that say a kind of thing (not a name, a path, an id or a secret): kept as they are.
ENUM_KEYS = frozenset({
    "engine", "active", "platform", "output_mode", "delivery_mode", "kind", "f0_method", "role", "placement",
    "target", "type", "state", "mode", "sort", "language", "text_lang", "prompt_text_lang", "default_emotion",
    "pip_mirror_kind",
})
_SECRET_KEY = re.compile(r"(key|token|secret|passw|auth)", re.I)
_ID_KEY = re.compile(r"(?:^|_)(?:id|qq|uin|openid|owner)(?:_|$)|whitelist|group", re.I)
_BIG_DICT_KEYS = frozenset({"characters", "models", "emotions", "presets"})
_LONG_DIGITS = re.compile(r"\d{5,}")
_PATH_LIKE = re.compile(r"^[A-Za-z]:[\\/]|\\|^/|.{3,}/.{2,}")
HIDDEN = "«已隐藏»"
SET = "已设置"
UNSET = "未设置"
MIN_SENSITIVE_LENGTH = 4


def _is_id_key(key: str) -> bool:
    return bool(_ID_KEY.search(key))


def redact_config(value: Any, key: str = "") -> Any:
    """A summary of a config (or any part of it) that no secret, path, name or id can be read from."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return SET if _is_id_key(key) or _SECRET_KEY.search(key) else value
    if isinstance(value, str):
        if not value.strip():
            return UNSET
        if key in ENUM_KEYS and len(value) <= 40 and not _SECRET_KEY.search(key):
            return value
        return SET
    if isinstance(value, dict):
        if key in _BIG_DICT_KEYS or any(_LONG_DIGITS.search(str(name)) for name in value):
            return {"数量": len(value)}
        return {str(name): redact_config(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return {"数量": len(value)}
    return SET


def sensitive_values(config: Any) -> list[str]:
    """Every text of the settings that could be a secret, a path, a name or an id, longest first."""
    found: set[str] = set()

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, str):
            text = node.strip()
            if len(text) < 3 or (key in ENUM_KEYS and not _SECRET_KEY.search(key)):
                return
            # Only what can be a secret, an id or a path: plain words and times ("00:00") would blank the logs.
            if _SECRET_KEY.search(key) or _is_id_key(key) or _PATH_LIKE.search(text) or _LONG_DIGITS.fullmatch(text):
                found.add(text)
        elif isinstance(node, dict):
            for name, item in node.items():
                if _LONG_DIGITS.search(str(name)) and len(str(name)) >= 3:
                    found.add(str(name))
                walk(item, str(name))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key)
        elif isinstance(node, int) and not isinstance(node, bool) and _is_id_key(key) and len(str(node)) >= 5:
            found.add(str(node))

    walk(config)
    return sorted(found, key=len, reverse=True)


# A user name inside a home path is what people leak by accident (stack traces, the core's own folders): the drive and
# "Users" stay so the line is still readable, the name goes.
_USER_PATH = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"']+", re.I)
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")
# Bare names that say nothing about anybody (and would blank ordinary words in the logs).
_GENERIC_NAMES = frozenset({"user", "users", "admin", "administrator", "root", "public", "default", "guest", "system"})


def environment_values(data_dir: Path | str | None = None) -> list[str]:
    """Things about this computer that must never be in a zip that gets posted: the core's own folders, the data
    folder, the home folder, the Windows user and computer name (only values of three or more characters)."""
    found: set[str] = set()
    here = Path(__file__).resolve()
    candidates: list[object] = [here.parent, here.parent.parent, here.parent.parent.parent]
    if data_dir is not None:
        candidates.append(Path(data_dir))
        try:
            candidates.append(Path(data_dir).resolve())
        except OSError:
            pass
    try:
        candidates.append(Path.home())
    except (RuntimeError, KeyError):
        pass
    for name in ("USERNAME", "USERPROFILE", "COMPUTERNAME", "HOMEPATH", "USER", "LOGNAME", "HOSTNAME"):
        candidates.append(os.environ.get(name, ""))
    for item in candidates:
        text = str(item).strip()
        if len(text) < 3 or text.lower() in _GENERIC_NAMES:
            continue
        if len(text) <= 3 and ("/" in text or "\\" in text):
            continue  # a drive root
        found.add(text)
    return sorted(found, key=len, reverse=True)


def scrub_text(text: str, values: Iterable[str]) -> str:
    """The text with every sensitive value replaced (also in its "/" and "\\" spellings, for paths), then every
    user name inside a home path."""
    for value in values:
        if not value:
            continue
        for variant in {value, value.replace("\\", "/"), value.replace("/", "\\"), value.replace("\\", "\\\\")}:
            if variant and variant in text:
                text = text.replace(variant, HIDDEN)
    text = _USER_PATH.sub(lambda match: match.group(1) + HIDDEN, text)
    return _UNIX_HOME.sub(lambda match: match.group(1) + HIDDEN, text)


_SERVICE_KEYS = ("state", "engine", "message", "busy", "waiting", "cooldown_seconds", "service_name", "enabled")


def service_summary(status: Any, values: Iterable[str]) -> Any:
    """What a service status says about its state: no folders, no log lines."""
    if not isinstance(status, dict):
        return None
    picked = {name: status[name] for name in _SERVICE_KEYS if name in status}
    return json.loads(scrub_text(json.dumps(picked, ensure_ascii=False), list(values)))


def status_summary(status: Any, values: Iterable[str]) -> Any:
    """The /api/status snapshot without the QQ account of NapCat."""
    if not isinstance(status, dict):
        return None
    copy = json.loads(json.dumps(status, ensure_ascii=False))
    onebot = copy.get("onebot")
    if isinstance(onebot, dict):
        onebot.pop("self_id", None)
    return json.loads(scrub_text(json.dumps(copy, ensure_ascii=False), list(values)))


def install_summary(overview: Any) -> Any:
    """Only the states of the install page (path checks, plugin, extension), never a path."""
    if not isinstance(overview, dict):
        return None
    checks = overview.get("checks") if isinstance(overview.get("checks"), dict) else {}
    return {
        "路径检查": {name: (item.get("state") if isinstance(item, dict) else None) for name, item in checks.items()},
        "ST 扩展": (overview.get("extension") or {}).get("state") if isinstance(overview.get("extension"), dict) else None,
        "AstrBot 插件": (overview.get("plugin") or {}).get("state") if isinstance(overview.get("plugin"), dict) else None,
    }


README = """ST Bridge 诊断包
生成时间：{when}

里面有什么
- 版本信息.json：核心版本、构建号、Python、系统、当前语音引擎和聊天平台。
- 状态快照.json：控制台总览用的状态（桥接、回复统计、今天的数字、连接器、NapCat 连接状态）。
- 服务状态.json：语音、翻唱服务的运行状态，安装页的检查结果（只有“有效/无效/未填”，没有路径）。
- 配置摘要.json：全部设置的摘要。只保留开关、数字和“种类”词；所有密钥、令牌、文件夹路径、QQ 号、openid、群号、名字、文案都不在里面，只写“已设置”或“未设置”，列表只写数量。
- 日志/：核心最近的日志文件（和“下载全部日志”是同一批）；写进日志的设置里的路径、ID 等已被替换成 {hidden}。

里面没有什么
- 聊天内容、好感度数据、心情记录、歌库和表情包、data 目录里的其他任何文件。
- 密钥、令牌、路径、QQ 号和群号。

把这个 zip 发给帮你排查的人之前，可以自己解压看一遍。
"""


def build_diagnostics_zip(
    *,
    data_dir: Path,
    config: dict[str, Any],
    status: Any = None,
    services: dict[str, Any] | None = None,
    install: Any = None,
    now: datetime | None = None,
) -> bytes:
    """The diagnostics zip as bytes. `status`, `services` and `install` are the console's own snapshots."""
    moment = now or datetime.now()
    values = sorted({*sensitive_values(config), *environment_values(data_dir)}, key=len, reverse=True)
    tts = config.get("tts") if isinstance(config.get("tts"), dict) else {}
    chat_platform = config.get("chat_platform") if isinstance(config.get("chat_platform"), dict) else {}
    versions = {
        "核心版本": CORE_VERSION,
        "构建号": compute_build_id(),
        "Python": sys.version.split()[0],
        "系统": platform.platform(),
        "语音引擎": tts.get("engine") if isinstance(tts.get("engine"), str) else None,
        "聊天平台": chat_platform.get("active") if isinstance(chat_platform.get("active"), str) else None,
    }
    service_states = {name: service_summary(item, values) for name, item in (services or {}).items()}
    service_states["安装页"] = install_summary(install)

    def dump(data: Any) -> str:
        # Everything that goes in passes through the scrubber, whatever it was built from.
        return scrub_text(json.dumps(data, ensure_ascii=False, indent=2), values)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("说明.txt", README.format(when=moment.strftime("%Y-%m-%d %H:%M:%S"), hidden=HIDDEN))
        archive.writestr("版本信息.json", dump(versions))
        archive.writestr("状态快照.json", dump(status_summary(status, values)))
        archive.writestr("服务状态.json", dump(service_states))
        archive.writestr("配置摘要.json", dump(redact_config(config)))
        total = 0
        for path in reversed(log_files(data_dir)):
            try:
                size = path.stat().st_size
                if total + size > MAX_ZIP_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            archive.writestr(f"日志/{path.name}", scrub_text(text, values))
            total += size
    return buffer.getvalue()
