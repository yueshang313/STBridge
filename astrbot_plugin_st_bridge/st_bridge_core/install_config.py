"""The ``install`` section of the console config (R4b): where the programs the bridge works with live.

  st_dir       SillyTavern (its program folder or its data folder)
  astrbot_dir  AstrBot (its folder, or the ``plugins`` folder itself)
  napcat_path  NapCat's launcher (.exe/.bat); only used by the "start NapCat" button
  pip_mirror   package index for the extra-dependency buttons ("" = the official index)

The folders of GPT-SoVITS, Irodori and RVC stay where they always were (``tts.*.launch_dir``, ``tts.irodori.server_dir``,
``cover.rvc_root``): one value, one place. Every field may be empty ("not set up").
"""

from __future__ import annotations

from typing import Any

from .emotion_config import EmotionConfigError

DEFAULT_PIP_MIRROR = "https://mirrors.pku.edu.cn/pypi/simple"
MAX_PATH_CHARS = 500
PATH_FIELDS = ("st_dir", "astrbot_dir", "napcat_path")


def default_install_settings() -> dict[str, Any]:
    return {"st_dir": "", "astrbot_dir": "", "napcat_path": "", "pip_mirror": DEFAULT_PIP_MIRROR}


def clean_path_text(value: Any, label: str) -> str:
    """A path typed by the user: stripped, without surrounding quotes, no control characters; may be empty."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip().strip('"').strip()
    if len(text) > MAX_PATH_CHARS or any(ord(char) < 32 for char in text):
        raise EmotionConfigError(400, f"invalid {label}")
    return text


def _mirror(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EmotionConfigError(400, "invalid pip mirror")
    text = value.strip()
    if not text:
        return ""
    if len(text) > 300 or not text.lower().startswith(("https://", "http://")) or any(c.isspace() or ord(c) < 32 for c in text):
        raise EmotionConfigError(400, "invalid pip mirror: an http(s) address without spaces")
    return text


def validate_install_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "install settings must be an object")
    merged = {**default_install_settings(), **settings}
    result = {field: clean_path_text(merged.get(field), field) for field in PATH_FIELDS}
    result["pip_mirror"] = _mirror(merged.get("pip_mirror"))
    return result


def install_settings(config: Any) -> dict[str, Any]:
    """The section of a config; an unreadable one gives the defaults."""
    try:
        return validate_install_settings(config.get("install", {}))
    except (AttributeError, TypeError, EmotionConfigError):
        return default_install_settings()
