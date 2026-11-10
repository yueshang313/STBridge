"""The owner: whose private chat is bridged (platform id + sender id), kept in the console config.

Since R2b the two values live here instead of in the AstrBot plugin's own settings. The first time a connector
says hello with values from its own settings they are imported once (``imported`` is then True); after that
only the console changes them. Pure logic with no platform dependency.
"""

from __future__ import annotations

from typing import Any

from .emotion_config import EmotionConfigError

MAX_ID_LENGTH = 200


def default_owner_settings() -> dict[str, Any]:
    return {"platform_id": "", "sender_id": "", "imported": False}


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip()
    if len(text) > MAX_ID_LENGTH or any(ord(char) < 32 for char in text):
        raise EmotionConfigError(400, f"invalid {label}")
    return text


def validate_owner_settings(settings: Any) -> dict[str, Any]:
    """Validate an owner section; ``imported`` is kept as stored (a boolean)."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "owner settings must be an object")
    merged = {**default_owner_settings(), **settings}
    if not isinstance(merged["imported"], bool):
        raise EmotionConfigError(400, "invalid imported")
    platform_id = _identifier(merged["platform_id"], "platform_id")
    sender_id = _identifier(merged["sender_id"], "sender_id")
    if bool(platform_id) != bool(sender_id):
        raise EmotionConfigError(400, "platform_id and sender_id must both be set or both be empty")
    return {"platform_id": platform_id, "sender_id": sender_id, "imported": merged["imported"]}


def owner_settings(config: Any) -> dict[str, Any]:
    """The owner section of a config; an unreadable one counts as "no owner"."""
    try:
        return validate_owner_settings(config.get("owner", {}))
    except (AttributeError, TypeError, EmotionConfigError):
        return default_owner_settings()
