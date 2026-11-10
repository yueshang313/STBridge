"""Network settings: the ports of ST (origin), the bridge, the console, the connector and NapCat (OneBot), read once when the core starts.

Pure logic with no AstrBot or web-framework dependency. The listen address is not a setting: everything
listens on 127.0.0.1 only. Changing a port takes effect when the core is restarted (reloading the plugin does that).
"""

from __future__ import annotations

import math
from typing import Any

from .emotion_config import EmotionConfigError

DEFAULT_ST_PORT = 11451
DEFAULT_BRIDGE_PORT = 11452
DEFAULT_CONSOLE_PORT = 11453
DEFAULT_CONNECTOR_PORT = 11454
DEFAULT_ONEBOT_PORT = 11455
PORT_RANGE = (1024, 65535)
# Ports of the default Irodori and GPT-SoVITS installs, kept off limits for the other services as before.
FIXED_RESERVED_PORTS = frozenset({8088, 9880})
PORT_FIELDS = ("st_port", "bridge_port", "console_port", "connector_port", "onebot_port")
PORT_LABELS = {
    "st_port": "ST",
    "bridge_port": "bridge",
    "console_port": "控制台",
    "connector_port": "连接器",
    "onebot_port": "NapCat",
}


def default_network_settings() -> dict[str, int]:
    return {
        "st_port": DEFAULT_ST_PORT,
        "bridge_port": DEFAULT_BRIDGE_PORT,
        "console_port": DEFAULT_CONSOLE_PORT,
        "connector_port": DEFAULT_CONNECTOR_PORT,
        "onebot_port": DEFAULT_ONEBOT_PORT,
    }


def _port(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value != int(value)
        or not PORT_RANGE[0] <= int(value) <= PORT_RANGE[1]
    ):
        raise EmotionConfigError(400, f"invalid {label}: {PORT_RANGE[0]}-{PORT_RANGE[1]}")
    return int(value)


def _other_ports(tts: Any, cover: Any) -> dict[int, str]:
    """Ports the TTS engines and the cover worker listen on, as far as the sections tell."""
    found: dict[int, str] = {}
    sections = tts if isinstance(tts, dict) else {}
    for engine, label in (("irodori", "Irodori"), ("gpt_sovits", "GPT-SoVITS")):
        section = sections.get(engine)
        port = section.get("port") if isinstance(section, dict) else None
        if isinstance(port, int) and not isinstance(port, bool):
            found.setdefault(port, f"{label} 的服务端口")
    port = cover.get("port") if isinstance(cover, dict) else None
    if isinstance(port, int) and not isinstance(port, bool):
        found.setdefault(port, "翻唱端口")
    return found


def validate_network_settings(settings: Any, *, tts: Any = None, cover: Any = None) -> dict[str, int]:
    """Validate the network section: 1024-65535, all different, and none used by a TTS engine or the cover worker."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "network settings must be an object")
    merged = {**default_network_settings(), **settings}
    result = {name: _port(merged.get(name), name) for name in PORT_FIELDS}
    if len(set(result.values())) != len(PORT_FIELDS):
        raise EmotionConfigError(400, "ports must be different from each other")
    others = _other_ports(tts, cover)
    for name, port in result.items():
        if port in others:
            raise EmotionConfigError(400, f"{name} {port} is used by {others[port]}")
        if port in FIXED_RESERVED_PORTS:
            raise EmotionConfigError(400, f"{name} {port} is reserved for Irodori / GPT-SoVITS")
    return result


def network_settings(config: Any) -> dict[str, int]:
    """Validated network ports from a config; an unreadable section gives the defaults."""
    try:
        section = config.get("network", {})
        merged = {**default_network_settings(), **section}
        return {name: _port(merged.get(name), name) for name in PORT_FIELDS}
    except (AttributeError, TypeError, EmotionConfigError):
        return default_network_settings()


def reserved_ports(config: Any) -> frozenset[int]:
    """Ports the cover worker and the TTS engines must not use: the four network ports and the fixed ones."""
    return FIXED_RESERVED_PORTS | frozenset(network_settings(config).values())
