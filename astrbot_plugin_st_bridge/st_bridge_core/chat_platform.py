"""Which chat platform works: AstrBot (through the connector) or NapCat (OneBot, built into the core).

R3: one section ``chat_platform`` in the console config. Only one platform is active at a time; the other one's
messages are ignored. Pure logic with no platform dependency.

  active          "astrbot" (the default: behavior as before R3) or "napcat"
  group_whitelist QQ group numbers NapCat answers in (empty: no group at all)
  napcat_owner    the owner's QQ number on NapCat (the AstrBot owner is the ``owner`` section)
"""

from __future__ import annotations

from typing import Any

from .emotion_config import EmotionConfigError

ASTRBOT = "astrbot"
NAPCAT = "napcat"
PLATFORMS = (ASTRBOT, NAPCAT)
# Fixed platform id of every NapCat identity: the affinity keys are "napcat:<QQ>", apart from AstrBot's.
NAPCAT_PLATFORM_ID = "napcat"
MAX_WHITELIST = 200
MAX_QQ_DIGITS = 20


def default_chat_platform_settings() -> dict[str, Any]:
    return {"active": ASTRBOT, "group_whitelist": [], "napcat_owner": ""}


def _qq(value: Any, label: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        value = str(value)
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip()
    if not text.isascii() or not text.isdigit() or len(text) > MAX_QQ_DIGITS or text.startswith("0"):
        raise EmotionConfigError(400, f"invalid {label}: digits only")
    return text


def validate_chat_platform_settings(settings: Any) -> dict[str, Any]:
    """Validate the section; the whitelist is de-duplicated and keeps its order."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "chat_platform settings must be an object")
    merged = {**default_chat_platform_settings(), **settings}
    active = merged["active"]
    if active not in PLATFORMS:
        raise EmotionConfigError(400, "invalid active platform")
    groups = merged["group_whitelist"]
    if not isinstance(groups, list) or len(groups) > MAX_WHITELIST:
        raise EmotionConfigError(400, f"group_whitelist must be a list of at most {MAX_WHITELIST} group numbers")
    whitelist: list[str] = []
    for item in groups:
        group = _qq(item, "group number")
        if group not in whitelist:
            whitelist.append(group)
    owner = merged["napcat_owner"]
    if owner is None or (isinstance(owner, str) and not owner.strip()):
        owner = ""
    else:
        owner = _qq(owner, "napcat_owner")
    return {"active": active, "group_whitelist": whitelist, "napcat_owner": owner}


def chat_platform_settings(config: Any) -> dict[str, Any]:
    """The section of a config; an unreadable one gives the defaults (AstrBot active)."""
    try:
        return validate_chat_platform_settings(config.get("chat_platform", {}))
    except (AttributeError, TypeError, EmotionConfigError):
        return default_chat_platform_settings()


# ---- NapCat conversations ---------------------------------------------------------------------
# The conversation id (also the reply ref) of a NapCat chat: "napcat:private:<QQ>" or "napcat:group:<group>".
_PRIVATE_PREFIX = f"{NAPCAT}:private:"
_GROUP_PREFIX = f"{NAPCAT}:group:"


def private_conversation(qq: str) -> str:
    return _PRIVATE_PREFIX + qq


def group_conversation(group: str) -> str:
    return _GROUP_PREFIX + group


def parse_conversation(conversation: Any) -> tuple[str, str] | None:
    """("private" | "group", number) of a NapCat conversation id; None for anything else."""
    if not isinstance(conversation, str):
        return None
    for prefix, kind in ((_PRIVATE_PREFIX, "private"), (_GROUP_PREFIX, "group")):
        if conversation.startswith(prefix):
            number = conversation[len(prefix):]
            if number.isascii() and number.isdigit():
                return kind, number
    return None


def conversation_platform(conversation: Any) -> str:
    """Which platform a conversation id belongs to."""
    return NAPCAT if parse_conversation(conversation) is not None else ASTRBOT
