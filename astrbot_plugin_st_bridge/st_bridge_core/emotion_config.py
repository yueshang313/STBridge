"""Emotion profile storage and validation shared by the console and the bridge.

This module has no web-framework dependency. Errors carry an HTTP-style status
so the console can map them to responses directly.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import random
import re
import shutil
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


NAME_RE = re.compile(r"^[^\\/:*?\"<>|\x00\r\n]{1,80}$")
EMOTION_RE = re.compile(r"^[A-Za-z0-9_\-一-鿿]{1,40}$")
LANGUAGES = {"Chinese", "Japanese", "English", "default"}
MEDIA_TYPES = {".wav", ".mp3", ".ogg", ".flac"}
WAV_MIN_SECONDS = 3.0
WAV_MAX_SECONDS = 10.0
MAX_AUDIO_STEM_CHARS = 120
_INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
# Upload de-duplication suffix; it is not part of the reference text.
_DUPLICATE_SUFFIX_RE = re.compile(r" \((\d+)\)$")
_RESERVED_FILE_STEMS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
# os.replace retries for transient Windows sharing locks; about 0.9 s in total.
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.25, 0.5)
# Strip every tag-shaped marker, even ones whose name is invalid, so none reach QQ or TTS.
EMOTION_TAG_RE = re.compile(r"\[情绪[:：]([^\]\r\n]{0,80})\]")
GPT_SOVITS_LANGUAGES = {
    "中文",
    "粤语",
    "英文",
    "日文",
    "韩文",
    "中英混合",
    "粤英混合",
    "日英混合",
    "韩英混合",
    "多语种混合",
    "多语种混合(粤语)",
    "all_zh",
    "all_yue",
    "en",
    "all_ja",
    "all_ko",
    "zh",
    "yue",
    "ja",
    "ko",
    "auto",
    "auto_yue",
}


class EmotionConfigError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def safe_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value.strip()):
        raise EmotionConfigError(400, f"invalid {label}")
    return value.strip()


# One MVU stat_data path segment: letters, digits, underscore and CJK only.
PATH_SEGMENT_RE = re.compile(r"^[0-9A-Za-z_\u3400-\u9fff]{1,40}$")
FORBIDDEN_PATH_SEGMENTS = frozenset({"__proto__", "constructor", "prototype"})


def is_safe_path_segment(value: Any) -> bool:
    return (
        isinstance(value, str)
        and PATH_SEGMENT_RE.fullmatch(value) is not None
        and value not in FORBIDDEN_PATH_SEGMENTS
    )


def safe_path_segment(value: Any, label: str) -> str:
    if not isinstance(value, str) or not is_safe_path_segment(value.strip()):
        raise EmotionConfigError(400, f"invalid {label}")
    return value.strip()


def safe_emotion(value: Any) -> str:
    if not isinstance(value, str) or not EMOTION_RE.fullmatch(value.strip()):
        raise EmotionConfigError(400, "invalid emotion")
    return value.strip()


def clean_path(value: Any, label: str, allow_empty: bool = True) -> str:
    if value is None:
        value = ""
    if label in {"prompt_text_lang", "text_lang"}:
        return _language(value, label)
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    value = value.strip()
    if not value and allow_empty:
        return ""
    if not value or "\x00" in value:
        raise EmotionConfigError(400, f"invalid {label}")
    return value


def _language(value: Any, label: str) -> str:
    if not isinstance(value, str) or value.strip() not in GPT_SOVITS_LANGUAGES:
        raise EmotionConfigError(400, f"unsupported {label}")
    return value.strip()


def _number(value: Any, label: str, minimum: float, maximum: float, integer: bool = False):
    if isinstance(value, bool):
        raise EmotionConfigError(400, f"invalid {label}")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:  # OverflowError: int(float("inf"))
        raise EmotionConfigError(400, f"invalid {label}") from exc
    if not minimum <= parsed <= maximum:
        raise EmotionConfigError(400, f"invalid {label}")
    return parsed


def default_emotion() -> dict[str, Any]:
    return {
        "ref_audio_path": "",
        "prompt_text": "",
        "prompt_text_lang": "日文",
        "text_lang": "日文",
        "top_k": 10,
        "top_p": 1.0,
        "temperature": 1.0,
        "speed_factor": 1.0,
        "split_sentence": True,
        # Irodori style controls (GPT-SoVITS ignores them).
        "caption": "",
        "emoji": "",
    }


AFFINITY_MIN = 0
AFFINITY_MAX = 100
MAX_AFFINITY_TIERS = 10


GROUP_AFFINITY_ABSOLUTE_MIN = -100
OWNER_TRACK = "owner"
GROUP_TRACK = "group"


ROLE_TAG_MAX_CHARS = 10
DEFAULT_ROLE_TAG = "【群友】"


TIER_DESCRIPTION_MAX_CHARS = 500

OWNER_TIER_DESCRIPTIONS = (
    "{{char}}把对方视为普通的合作对象，维持着礼貌但有距离感的态度，交流以正事为主，不会主动透露私人情绪。",
    "{{char}}认可对方，开始在对方面前放下部分伪装，会吐槽、分享生活琐事，被夸奖时会发自内心地高兴。",
    "{{char}}把对方视为可以示弱的避风港，不再掩饰疲惫和自卑，累了会主动撒娇、求夸奖，真心期待对方的认可。",
    "{{char}}对对方抱有明确的恋慕之情，看到对方关注别人会吃醋，想方设法制造独处机会，会接受对方的告白。",
    "{{char}}已认定对方是共度一生的伴侣，在对方面前完全不需要伪装，会主动表达爱意。",
)
GROUP_TIER_DESCRIPTIONS = (
    "{{char}}将该群友视为黑粉或带有敌意的陌生人。面对阴阳怪气、恶意贬低或挑衅时，会感到不满、委屈或生气，但仍会尽量维持基本礼貌，不会刻意讨好对方。",
    "{{char}}将该群友视为普通陌生网友，保持礼貌、自然的态度，会正常聊天，但不会主动透露太多私人生活。",
    "{{char}}对该群友印象不错，认为对方是友善、有趣的网友。聊天时更放松，会分享日常琐事、开玩笑、吐槽，被夸奖时会明显开心。",
    "{{char}}已经熟悉并喜欢这位支持者，记得对方的名字和聊天习惯，愿意主动回应对方的话题、分享近况，面对真诚的鼓励会自然地喜悦和感激。",
    "{{char}}将该群友视为值得珍惜的忠实粉丝，有明显的亲切感和信任感，愿意展现更真实、俏皮的一面。",
    "{{char}}将该群友视为非常重要的支持者，热情、亲切、高度感谢，会聊起自己的目标和烦恼，但双方仍然只是朋友（粉丝）关系，不会越界。",
)


def default_group_affinity_settings() -> dict[str, Any]:
    return {
        "variable": "群友好感度",
        "role_tag": DEFAULT_ROLE_TAG,
        "min": -50,
        "max": 100,
        "initial_value": 0,
        "tiers": [
            {"min": -50, "max": -21, "name": "黑粉", "description": GROUP_TIER_DESCRIPTIONS[0]},
            {"min": -20, "max": 20, "name": "普通网友", "description": GROUP_TIER_DESCRIPTIONS[1]},
            {"min": 21, "max": 40, "name": "友善网友", "description": GROUP_TIER_DESCRIPTIONS[2]},
            {"min": 41, "max": 60, "name": "熟悉的支持者", "description": GROUP_TIER_DESCRIPTIONS[3]},
            {"min": 61, "max": 80, "name": "忠实粉丝", "description": GROUP_TIER_DESCRIPTIONS[4]},
            {"min": 81, "max": 100, "name": "狂热粉丝", "description": GROUP_TIER_DESCRIPTIONS[5]},
        ],
    }


def default_affinity_settings() -> dict[str, Any]:
    return {
        "character": "藤田琴音",
        "display_name": "琴音",
        "initial_value": 0,
        "max_delta": 5,
        "tiers": [
            {"min": 0, "max": 10, "name": "陌生人", "description": OWNER_TIER_DESCRIPTIONS[0]},
            {"min": 11, "max": 40, "name": "有点用的制作人", "description": OWNER_TIER_DESCRIPTIONS[1]},
            {"min": 41, "max": 60, "name": "避风港", "description": OWNER_TIER_DESCRIPTIONS[2]},
            {"min": 61, "max": 80, "name": "恋慕", "description": OWNER_TIER_DESCRIPTIONS[3]},
            {"min": 81, "max": 100, "name": "伴侣", "description": OWNER_TIER_DESCRIPTIONS[4]},
        ],
        "group": default_group_affinity_settings(),
    }


def fill_tier_descriptions(affinity: Any) -> None:
    """Give a config / preset written before R5 the default tier descriptions, in place.

    Only tiers that never had a `description` key, in a list whose ranges are the default ones, get the
    default text; a list the user reshaped keeps empty descriptions.
    """
    if not isinstance(affinity, dict):
        return
    defaults = default_affinity_settings()
    for holder, fresh in ((affinity, defaults), (affinity.get("group"), defaults["group"])):
        tiers = holder.get("tiers") if isinstance(holder, dict) else None
        if not isinstance(tiers, list) or not all(isinstance(tier, dict) for tier in tiers):
            continue
        ranges = [(tier.get("min"), tier.get("max")) for tier in tiers]
        if ranges != [(tier["min"], tier["max"]) for tier in fresh["tiers"]]:
            continue
        for tier, default in zip(tiers, fresh["tiers"]):
            tier.setdefault("description", default["description"])


def stored_affinity_defaults() -> dict[str, Any]:
    """Affinity settings as stored in config.json: the character names live on the characters now."""
    settings = default_affinity_settings()
    del settings["character"], settings["display_name"]
    return settings


def _affinity_int(
    value: Any,
    label: str,
    low: int = AFFINITY_MIN,
    high: int = AFFINITY_MAX,
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise EmotionConfigError(400, f"invalid {label}")
    parsed = int(value)
    if not low <= parsed <= high:
        raise EmotionConfigError(400, f"invalid {label}")
    return parsed


def _validate_tiers(tiers_value: Any, low: int, high: int) -> list[dict[str, Any]]:
    """Tiers inside [low, high], sorted by min, never overlapping."""
    if not isinstance(tiers_value, list) or not 1 <= len(tiers_value) <= MAX_AFFINITY_TIERS:
        raise EmotionConfigError(400, "invalid affinity tiers")
    tiers = []
    for tier in tiers_value:
        if not isinstance(tier, dict):
            raise EmotionConfigError(400, "invalid affinity tiers")
        name = tier.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 20:
            raise EmotionConfigError(400, "invalid affinity tier name")
        tier_low = _affinity_int(tier.get("min"), "affinity tier range", low, high)
        tier_high = _affinity_int(tier.get("max"), "affinity tier range", low, high)
        if tier_low > tier_high:
            raise EmotionConfigError(400, "invalid affinity tier range")
        description = tier.get("description", "")
        if (
            not isinstance(description, str)
            or len(description) > TIER_DESCRIPTION_MAX_CHARS
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", description)
        ):
            raise EmotionConfigError(400, "invalid affinity tier description")
        tiers.append({"min": tier_low, "max": tier_high, "name": name.strip(), "description": description.strip()})
    tiers.sort(key=lambda item: item["min"])
    for previous, current in zip(tiers, tiers[1:]):
        if current["min"] <= previous["max"]:
            raise EmotionConfigError(400, "affinity tiers overlap")
    return tiers


def _validate_group_affinity(group: Any) -> dict[str, Any]:
    if not isinstance(group, dict):
        raise EmotionConfigError(400, "group affinity settings must be an object")
    merged = {**default_group_affinity_settings(), **group}
    variable = safe_path_segment(merged.get("variable"), "group affinity variable")
    low = _affinity_int(merged.get("min"), "group affinity min", GROUP_AFFINITY_ABSOLUTE_MIN, AFFINITY_MAX)
    high = _affinity_int(merged.get("max"), "group affinity max", GROUP_AFFINITY_ABSOLUTE_MIN, AFFINITY_MAX)
    if low >= high:
        raise EmotionConfigError(400, "invalid group affinity range")
    role_tag = merged.get("role_tag")
    if (
        not isinstance(role_tag, str)
        or not 1 <= len(role_tag.strip()) <= ROLE_TAG_MAX_CHARS
        or re.search(r"[\r\n]", role_tag)
    ):
        raise EmotionConfigError(400, "invalid group affinity role tag")
    return {
        "variable": variable,
        "role_tag": role_tag.strip(),
        "min": low,
        "max": high,
        "initial_value": _affinity_int(
            merged.get("initial_value"), "group affinity initial value", low, high
        ),
        "tiers": _validate_tiers(merged.get("tiers"), low, high),
    }


def validate_affinity_settings(settings: Any) -> dict[str, Any]:
    """Validate the affinity settings of both tracks; tiers are sorted and must not overlap."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "affinity settings must be an object")
    merged = {**default_affinity_settings(), **settings}
    character = safe_path_segment(merged.get("character"), "affinity character")
    display_name = safe_name(merged.get("display_name"), "affinity display name")
    return {
        "character": character,
        "display_name": display_name,
        "initial_value": _affinity_int(merged.get("initial_value"), "affinity initial value"),
        "max_delta": _affinity_int(merged.get("max_delta"), "affinity max delta"),
        "tiers": _validate_tiers(merged.get("tiers"), AFFINITY_MIN, AFFINITY_MAX),
        "group": _validate_group_affinity(merged.get("group")),
    }


NICKNAME_MAX_CHARS = 20
_NICKNAME_FORBIDDEN_RE = re.compile(r"[\s{}]")


def validate_nickname(value: Any) -> str:
    """The nickname doubles as the wake word and is put into reply texts: 1-20 characters, no whitespace or braces."""
    if not isinstance(value, str):
        raise EmotionConfigError(400, "invalid nickname")
    text = value.strip()
    if not 1 <= len(text) <= NICKNAME_MAX_CHARS or _NICKNAME_FORBIDDEN_RE.search(text):
        raise EmotionConfigError(400, "invalid nickname")
    return text


def validate_st_name(value: Any) -> str:
    """The character name inside ST's stat_data (one MVU path segment)."""
    return safe_path_segment(value, "st_name")


def default_nickname(name: str) -> str:
    """A nickname for a character that has none yet: its own name without whitespace and braces."""
    return re.sub(r"[\s{}]", "", name)[:NICKNAME_MAX_CHARS] or "角色"


def default_st_name(name: str) -> str:
    """An ST name for a character that has none yet: its own name reduced to what one stat_data key may hold."""
    return re.sub(r"[^0-9A-Za-z_\u3400-\u9fff]", "", name)[:40] or "角色"


def active_identity(config: Any) -> dict[str, str]:
    """{"st_name", "nickname"} of the character in use: the default character, else the first one.

    With no character at all the legacy affinity names, else the built-in defaults, are used.
    """
    try:
        characters = config.get("characters", {})
        name = config.get("default_character", "")
        character = characters.get(name) if isinstance(characters, dict) else None
        if character is None and isinstance(characters, dict) and characters:
            name, character = next(iter(characters.items()))
        if isinstance(character, dict):
            return {
                "st_name": character.get("st_name") or default_st_name(name),
                "nickname": character.get("nickname") or default_nickname(name),
            }
        legacy = config.get("affinity", {})
    except AttributeError:
        legacy = {}
    defaults = default_affinity_settings()
    legacy = legacy if isinstance(legacy, dict) else {}
    return {
        "st_name": legacy.get("character") or defaults["character"],
        "nickname": legacy.get("display_name") or defaults["display_name"],
    }


def affinity_settings(config: Any) -> dict[str, Any]:
    """Validated affinity settings from a config, falling back to the defaults.

    `character` and `display_name` are not stored any more: they come from the active character.
    """
    try:
        settings = validate_affinity_settings(config.get("affinity", {}))
    except (AttributeError, EmotionConfigError):
        settings = default_affinity_settings()
    identity = active_identity(config)
    settings["character"] = identity["st_name"]
    settings["display_name"] = identity["nickname"]
    return settings


def affinity_track(settings: dict[str, Any], track: str) -> dict[str, Any]:
    """Range, initial value, tiers and MVU variable path of one track.

    The owner track is the 0-100 stat_data.<角色>.好感度; the group track is the
    separate stat_data.<variable> with its own range. max_delta is shared.
    """
    if track == GROUP_TRACK:
        group = settings["group"]
        return {
            "track": GROUP_TRACK,
            "min": group["min"],
            "max": group["max"],
            "initial_value": group["initial_value"],
            "tiers": group["tiers"],
            "path": [group["variable"]],
            "max_delta": settings["max_delta"],
        }
    return {
        "track": OWNER_TRACK,
        "min": AFFINITY_MIN,
        "max": AFFINITY_MAX,
        "initial_value": settings["initial_value"],
        "tiers": settings["tiers"],
        "path": [settings["character"], "好感度"],
        "max_delta": settings["max_delta"],
    }


def affinity_tier_name(settings: dict[str, Any], value: int) -> str:
    for tier in settings.get("tiers", []):
        if tier["min"] <= value <= tier["max"]:
            return tier["name"]
    return "未分档"


QUEUE_MAX_WAITING_RANGE = (1, 20)
QUEUE_MAX_WAIT_SECONDS_RANGE = (30, 600)


def default_queue_settings() -> dict[str, Any]:
    return {"max_waiting": 5, "max_wait_seconds": 180}


def _queue_int(value: Any, label: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise EmotionConfigError(400, f"invalid {label}")
    parsed = int(value)
    if not bounds[0] <= parsed <= bounds[1]:
        raise EmotionConfigError(400, f"invalid {label}")
    return parsed


def validate_queue_settings(settings: Any) -> dict[str, Any]:
    """Validate the global request queue limits."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "queue settings must be an object")
    merged = {**default_queue_settings(), **settings}
    return {
        "max_waiting": _queue_int(merged.get("max_waiting"), "queue max waiting", QUEUE_MAX_WAITING_RANGE),
        "max_wait_seconds": _queue_int(
            merged.get("max_wait_seconds"), "queue max wait seconds", QUEUE_MAX_WAIT_SECONDS_RANGE
        ),
    }


def queue_settings(config: Any) -> dict[str, Any]:
    """Validated queue settings from a config, falling back to the defaults."""
    try:
        return validate_queue_settings(config.get("queue", {}))
    except (AttributeError, EmotionConfigError):
        return default_queue_settings()


MOOD_DECAY_RANGE = (5, 1440)
MOOD_RECENT_RANGE = (1, 10)


def default_mood_settings() -> dict[str, Any]:
    return {"decay_minutes": 60, "recent": 3}


def validate_mood_settings(settings: Any) -> dict[str, Any]:
    """Validate the mood fade window (minutes) and how many recent moods the macros show."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "mood settings must be an object")
    merged = {**default_mood_settings(), **settings}
    return {
        "decay_minutes": _queue_int(merged.get("decay_minutes"), "mood decay minutes", MOOD_DECAY_RANGE),
        "recent": _queue_int(merged.get("recent"), "mood recent", MOOD_RECENT_RANGE),
    }


def mood_settings(config: Any) -> dict[str, Any]:
    """Validated mood settings from a config, falling back to the defaults."""
    try:
        return validate_mood_settings(config.get("mood", {}))
    except (AttributeError, EmotionConfigError):
        return default_mood_settings()


STICKER_PROB_RANGE = (0, 100)
STICKER_COOLDOWN_RANGE = (0, 3600)


def default_sticker_settings() -> dict[str, Any]:
    return {"enabled": True, "prob": 30, "cooldown": 60}


def validate_sticker_settings(settings: Any) -> dict[str, Any]:
    """Validate the emotion sticker switch, send probability (%) and per-session cooldown (s)."""
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "sticker settings must be an object")
    merged = {**default_sticker_settings(), **settings}
    if not isinstance(merged.get("enabled"), bool):
        raise EmotionConfigError(400, "invalid sticker enabled")
    return {
        "enabled": merged["enabled"],
        "prob": _queue_int(merged.get("prob"), "sticker prob", STICKER_PROB_RANGE),
        "cooldown": _queue_int(merged.get("cooldown"), "sticker cooldown", STICKER_COOLDOWN_RANGE),
    }


def sticker_settings(config: Any) -> dict[str, Any]:
    """Validated sticker settings from a config, falling back to the defaults."""
    try:
        return validate_sticker_settings(config.get("stickers", {}))
    except (AttributeError, EmotionConfigError):
        return default_sticker_settings()


TTS_ENGINES = ("irodori", "gpt_sovits")
# GPT-SoVITS api.py labels plus the Chinese aliases the old plugin config offered.
TTS_LANGUAGES = GPT_SOVITS_LANGUAGES | {"英语", "日语", "韩语"}


def default_tts_settings() -> dict[str, Any]:
    return {
        "engine": "irodori",
        "common": {
            "auto_start": True,
            "stop_on_exit": True,
            "start_timeout": 180,
            "tts_timeout_minutes": 5,
            "prob": 100.0,
            "text_limit": 0,
            "cooldown": 0,
            "send_text_with_audio": False,
            "split_messages": False,
            "split_interval": 1.5,
            "split_max": 3,
        },
        "irodori": {
            "host": "127.0.0.1",
            "port": 8088,
            "launch_dir": "",
            "server_dir": "",
            "hf_checkpoint": "Aratako/Irodori-TTS-v4.1-Small",
            "lora_adapter": "",
            "num_steps": 0,
            "cfg_scale_text": 3.0,
            "cfg_scale_caption": 4.0,
            "default_caption": "",
            "output_mode": "qq_24k",
        },
        "gpt_sovits": {
            "host": "127.0.0.1",
            "port": 9880,
            "launch_dir": "",
            "gpt_model_path": "",
            "sovits_model_path": "",
            "ref_audio_path": "",
            "prompt_text": "",
            "prompt_text_lang": "中文",
            "text_lang": "中英混合",
            "top_k": 10,
            "top_p": 1.0,
            "temperature": 1.0,
            "speed_factor": 1.0,
            "split_sentence": True,
        },
    }


def _tts_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise EmotionConfigError(400, f"invalid {label}")
    return value


def _tts_text(value: Any, label: str, limit: int, default: str | None = None) -> str:
    """A stripped single-line string; blank falls back to ``default`` when one is given."""
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip()
    if len(text) > limit or "\n" in text or "\r" in text or "\x00" in text:
        raise EmotionConfigError(400, f"invalid {label}")
    return text if text or default is None else default


def _tts_host(value: Any, label: str) -> str:
    host = _tts_text(value, label, 255, "127.0.0.1")
    if any(char in host for char in "/?#@") or " " in host:
        raise EmotionConfigError(400, f"invalid {label}")
    return host


def _tts_language(value: Any, label: str) -> str:
    if not isinstance(value, str) or value.strip() not in TTS_LANGUAGES:
        raise EmotionConfigError(400, f"unsupported {label}")
    return value.strip()


def _validate_tts_common(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise EmotionConfigError(400, "tts common settings must be an object")
    merged = {**default_tts_settings()["common"], **section}
    return {
        "auto_start": _tts_bool(merged.get("auto_start"), "auto_start"),
        "stop_on_exit": _tts_bool(merged.get("stop_on_exit"), "stop_on_exit"),
        "start_timeout": _number(merged.get("start_timeout"), "start_timeout", 30, 600, True),
        "tts_timeout_minutes": _number(
            merged.get("tts_timeout_minutes"), "tts_timeout_minutes", 3, 15, True
        ),
        "prob": _number(merged.get("prob"), "prob", 0.0, 100.0),
        "text_limit": _number(merged.get("text_limit"), "text_limit", 0, 10000, True),
        "cooldown": _number(merged.get("cooldown"), "cooldown", 0, 3600, True),
        "send_text_with_audio": _tts_bool(merged.get("send_text_with_audio"), "send_text_with_audio"),
        "split_messages": _tts_bool(merged.get("split_messages"), "split_messages"),
        "split_interval": round(_number(merged.get("split_interval"), "split_interval", 0.0, 10.0), 1),
        "split_max": _number(merged.get("split_max"), "split_max", 1, 10, True),
    }


IRODORI_OUTPUT_MODES = ("qq_24k", "native")  # 24 kHz transcode before sending (AstrBot keeps the WAV's rate, QQ desktop can't play 48 kHz silk) / the engine's own 48 kHz


def _output_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in IRODORI_OUTPUT_MODES:
        raise EmotionConfigError(400, "invalid irodori output_mode")
    return value


def _validate_tts_irodori(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise EmotionConfigError(400, "irodori settings must be an object")
    defaults = default_tts_settings()["irodori"]
    merged = {**defaults, **section}
    return {
        "host": _tts_host(merged.get("host"), "irodori host"),
        "port": _number(merged.get("port"), "irodori port", 1, 65535, True),
        "launch_dir": _tts_text(merged.get("launch_dir"), "irodori launch_dir", 500),
        "server_dir": _tts_text(merged.get("server_dir"), "irodori server_dir", 500),
        "hf_checkpoint": _tts_text(
            merged.get("hf_checkpoint"), "irodori hf_checkpoint", 200, defaults["hf_checkpoint"]
        ),
        "lora_adapter": _tts_text(merged.get("lora_adapter"), "irodori lora_adapter", 500),
        "num_steps": _number(merged.get("num_steps"), "irodori num_steps", 0, 200, True),
        "cfg_scale_text": _number(merged.get("cfg_scale_text"), "irodori cfg_scale_text", 0.0, 20.0),
        "cfg_scale_caption": _number(merged.get("cfg_scale_caption"), "irodori cfg_scale_caption", 0.0, 20.0),
        "default_caption": _style_text(merged.get("default_caption"), "default_caption", MAX_CAPTION_LENGTH, False),
        "output_mode": _output_mode(merged.get("output_mode")),
    }


def _validate_tts_gpt_sovits(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise EmotionConfigError(400, "gpt_sovits settings must be an object")
    defaults = default_tts_settings()["gpt_sovits"]
    merged = {**defaults, **section}
    split_sentence = merged.get("split_sentence")
    if not isinstance(split_sentence, bool):
        raise EmotionConfigError(400, "invalid split_sentence")
    gpt = _tts_text(merged.get("gpt_model_path"), "gpt_model_path", 500)
    sovits = _tts_text(merged.get("sovits_model_path"), "sovits_model_path", 500)
    if bool(gpt) != bool(sovits):
        raise EmotionConfigError(400, "gpt_model_path and sovits_model_path must be set together")
    prompt_text = merged.get("prompt_text")
    if not isinstance(prompt_text, str) or len(prompt_text) > 2000:
        raise EmotionConfigError(400, "invalid prompt_text")
    return {
        "host": _tts_host(merged.get("host"), "gpt_sovits host"),
        "port": _number(merged.get("port"), "gpt_sovits port", 1, 65535, True),
        "launch_dir": _tts_text(merged.get("launch_dir"), "gpt_sovits launch_dir", 500),
        "gpt_model_path": gpt,
        "sovits_model_path": sovits,
        "ref_audio_path": _tts_text(merged.get("ref_audio_path"), "ref_audio_path", 500),
        "prompt_text": prompt_text,
        "prompt_text_lang": _tts_language(merged.get("prompt_text_lang"), "prompt_text_lang"),
        "text_lang": _tts_language(merged.get("text_lang"), "text_lang"),
        "top_k": _number(merged.get("top_k"), "top_k", 1, 100, True),
        "top_p": _number(merged.get("top_p"), "top_p", 0.0, 1.0),
        "temperature": _number(merged.get("temperature"), "temperature", 0.0, 2.0),
        "speed_factor": _number(merged.get("speed_factor"), "speed_factor", 0.25, 2.0),
        "split_sentence": split_sentence,
    }


def validate_tts_settings(settings: Any, network: Any = None) -> dict[str, Any]:
    """Validate the whole ``tts`` section; missing keys take their defaults.

    With the network settings given, a service port that one of ST / bridge / console listens on is rejected.
    """
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "tts settings must be an object")
    defaults = default_tts_settings()
    merged = {**defaults, **settings}
    engine = merged.get("engine")
    if engine not in TTS_ENGINES:
        raise EmotionConfigError(400, "unsupported tts engine")
    result = {
        "engine": engine,
        "common": _validate_tts_common(merged.get("common")),
        "irodori": _validate_tts_irodori(merged.get("irodori")),
        "gpt_sovits": _validate_tts_gpt_sovits(merged.get("gpt_sovits")),
    }
    if network is not None:
        taken = set(_network_module().network_settings({"network": network}).values())
        for name in ("irodori", "gpt_sovits"):
            if result[name]["port"] in taken:
                raise EmotionConfigError(400, f"{name} port is used by ST, the bridge or the console")
    return result


def tts_settings(config: Any) -> dict[str, Any]:
    """Validated tts settings from a config, falling back to the defaults."""
    try:
        return validate_tts_settings(config.get("tts", {}), network=_network_module().network_settings(config))
    except (AttributeError, EmotionConfigError):
        return default_tts_settings()


def _schedule_module():
    # schedule_config imports this module, so it is loaded lazily.
    from . import schedule_config

    return schedule_config


def _cover_module():
    from . import cover_config

    return cover_config


def _cleanup_module():
    from . import storage_cleanup

    return storage_cleanup


def _song_request_module():
    from . import song_request

    return song_request


def _texts_module():
    from . import reply_texts

    return reply_texts


def _network_module():
    from . import network_config

    return network_config


def _owner_module():
    from . import owner_config

    return owner_config


def _chat_platform_module():
    from . import chat_platform

    return chat_platform


def _install_module():
    from . import install_config

    return install_config


def _injection_module():
    from . import st_injection

    return st_injection


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "default_character": "",
        "default_emotion": "neutral",
        "models": {},
        "characters": {},
        "affinity": stored_affinity_defaults(),
        "queue": default_queue_settings(),
        "stickers": default_sticker_settings(),
        "mood": default_mood_settings(),
        "schedule": _schedule_module().default_schedule_section(),
        "cover": _cover_module().default_cover_settings(),
        "cleanup": _cleanup_module().default_cleanup_settings(),
        "song_request": _song_request_module().stored_song_request_defaults(),
        "texts": _texts_module().default_texts(),
        "network": _network_module().default_network_settings(),
        "owner": _owner_module().default_owner_settings(),
        "chat_platform": _chat_platform_module().default_chat_platform_settings(),
        "install": _install_module().default_install_settings(),
        "st_injection": _injection_module().default_injection_settings(),
        "tts": default_tts_settings(),
    }


MAX_CAPTION_LENGTH = 300
MAX_EMOJI_LENGTH = 16


def _style_text(value: Any, label: str, limit: int, single_line: bool) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip()
    if len(text) > limit or (single_line and ("\n" in text or "\r" in text)):
        raise EmotionConfigError(400, f"invalid {label}")
    return text


def validate_emotion_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise EmotionConfigError(400, "emotion profile must be an object")
    merged = {**default_emotion(), **profile}
    split_sentence = merged.get("split_sentence")
    if not isinstance(split_sentence, bool):
        raise EmotionConfigError(400, "invalid split_sentence")
    result = {
        "ref_audio_path": clean_path(merged.get("ref_audio_path"), "ref_audio_path"),
        "prompt_text": merged.get("prompt_text", ""),
        "prompt_text_lang": clean_path(merged.get("prompt_text_lang"), "prompt_text_lang", False),
        "text_lang": clean_path(merged.get("text_lang"), "text_lang", False),
        "top_k": _number(merged.get("top_k"), "top_k", 1, 100, True),
        "top_p": _number(merged.get("top_p"), "top_p", 0.0, 1.0),
        "temperature": _number(merged.get("temperature"), "temperature", 0.0, 2.0),
        "speed_factor": _number(merged.get("speed_factor"), "speed_factor", 0.25, 2.0),
        "split_sentence": split_sentence,
        "caption": _style_text(merged.get("caption"), "caption", MAX_CAPTION_LENGTH, False),
        "emoji": _style_text(merged.get("emoji"), "emoji", MAX_EMOJI_LENGTH, True),
    }
    if not isinstance(result["prompt_text"], str) or len(result["prompt_text"]) > 2000:
        raise EmotionConfigError(400, "invalid prompt_text")
    return result


def validate_config(config: Any) -> None:
    if not isinstance(config, dict) or config.get("version") != 1:
        raise RuntimeError("unsupported or invalid console config version")
    if not isinstance(config.get("models"), dict) or not isinstance(config.get("characters"), dict):
        raise RuntimeError("console config has invalid collections")
    # Optional; configs written before the path picker do not have it.
    clean_path(config.get("gpt_sovits_root", ""), "gpt_sovits_root")
    # Optional; configs written before P6 get the defaults when loaded.
    if "affinity" in config:
        validate_affinity_settings(config["affinity"])
    # Optional; configs written before Q1 get the defaults when loaded.
    if "queue" in config:
        validate_queue_settings(config["queue"])
    # Optional; configs written before K1 get the defaults when loaded.
    if "stickers" in config:
        validate_sticker_settings(config["stickers"])
    # Optional; configs written before L1c get the defaults when loaded.
    if "mood" in config:
        validate_mood_settings(config["mood"])
    # Optional; configs written before T1 get the (disabled) preset plans when loaded.
    if "schedule" in config:
        _schedule_module().validate_schedule_section(config["schedule"])
    # Optional; configs written before P7 get the defaults when loaded.
    if "cover" in config:
        _cover_module().validate_cover_settings(config["cover"], reserved=_network_module().reserved_ports(config))
    # Optional; configs written before C1 get the defaults when loaded.
    if "cleanup" in config:
        _cleanup_module().validate_cleanup_settings(config["cleanup"])
    # Optional; configs written before P7c get the defaults when loaded.
    if "song_request" in config:
        _song_request_module().validate_song_request_settings(config["song_request"])
    # Reply texts are never a reason to refuse a config: a text that is not valid falls back to its default when read.
    # Optional; configs written before G1 get the defaults when loaded.
    if "network" in config:
        _network_module().validate_network_settings(config["network"], tts=config.get("tts"), cover=config.get("cover"))
    # Optional; configs written before R2b get the (empty) owner when loaded.
    if "owner" in config:
        _owner_module().validate_owner_settings(config["owner"])
    # Optional; configs written before R3 get AstrBot as the active platform.
    if "chat_platform" in config:
        _chat_platform_module().validate_chat_platform_settings(config["chat_platform"])
    # Optional; configs written before R4b get empty paths when loaded.
    if "install" in config:
        _install_module().validate_install_settings(config["install"])
    # Optional; configs written before R5 get the built-in entries when loaded.
    if "st_injection" in config:
        _injection_module().validate_injection_settings(config["st_injection"])
    # Optional; configs written before M1 get the defaults when loaded.
    if "tts" in config:
        validate_tts_settings(config["tts"], network=config.get("network"))
    for model_name, model in config["models"].items():
        safe_name(model_name, "model name")
        if not isinstance(model, dict):
            raise RuntimeError("invalid model entry")
    for character_name, character in config["characters"].items():
        safe_name(character_name, "character name")
        if not isinstance(character, dict) or not isinstance(character.get("emotions", {}), dict):
            raise RuntimeError("invalid character entry")
        # Optional; characters written before G1 get both names from the migration.
        if "nickname" in character:
            validate_nickname(character["nickname"])
        if "st_name" in character:
            validate_st_name(character["st_name"])
        for emotion_name, profile in character.get("emotions", {}).items():
            safe_emotion(emotion_name)
            validate_emotion_profile(profile)


def strip_emotion_tags(text: str) -> tuple[str, list[str]]:
    """Remove all emotion tags and return the cleaned text plus raw tag names."""
    names = [match.group(1).strip() for match in EMOTION_TAG_RE.finditer(text)]
    return EMOTION_TAG_RE.sub("", text), names


def parse_emotion_name(raw: str) -> str | None:
    try:
        return safe_emotion(raw)
    except EmotionConfigError:
        return None


def _audio_exists(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return Path(value.strip()).is_file()
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class TtsSelection:
    profile: dict[str, Any] | None
    emotion: str | None
    reason: str | None
    source: str | None = None
    candidates: int = 0
    # 1-based position among usable files sorted by file name; 1 for manual profiles.
    pick: int = 0


def safe_audio_stem(text: Any) -> str:
    """Turn reference text into a Windows-safe file stem."""
    stem = _INVALID_FILENAME_RE.sub("", text if isinstance(text, str) else "")
    stem = stem.strip().rstrip(". ")
    stem = stem[:MAX_AUDIO_STEM_CHARS].rstrip(". ")
    if not stem:
        return "reference"
    if stem.split(".", 1)[0].upper() in _RESERVED_FILE_STEMS:
        stem = f"_{stem}"
    return stem


def unique_audio_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def prompt_text_from_path(path: Path) -> str:
    return _DUPLICATE_SUFFIX_RE.sub("", path.stem).strip()


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (OSError, EOFError, wave.Error):
        return None
    if rate <= 0:
        return None
    return frames / rate


def describe_audio(path: Path, root: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".wav":
        duration = wav_duration(path)
        readable = duration is not None
    else:
        # Only WAV headers are checked; other formats are trusted as-is.
        duration, readable = None, True
    out_of_range = duration is not None and not (WAV_MIN_SECONDS <= duration <= WAV_MAX_SECONDS)
    prompt_text = prompt_text_from_path(path)
    return {
        "path": str(path),
        "relative_path": path.relative_to(root).as_posix(),
        "filename": path.name,
        "prompt_text": prompt_text,
        "duration": None if duration is None else round(duration, 3),
        "out_of_range": out_of_range,
        "readable": readable,
        "usable": readable and not out_of_range and bool(prompt_text),
    }


def discover_emotions(root: Any) -> dict[str, list[dict[str, Any]]]:
    """Scan root/<emotion>/<reference text>.<ext>; never raises.

    Every valid emotion folder is returned, including empty ones; callers use
    each audio's "usable" flag to decide what may be selected.
    """
    if not isinstance(root, (str, Path)) or not str(root).strip():
        return {}
    try:
        base = Path(str(root).strip()).expanduser()
        if not base.is_dir():
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for folder in sorted(base.iterdir(), key=lambda item: item.name):
            if not folder.is_dir() or parse_emotion_name(folder.name) != folder.name:
                continue
            result[folder.name] = [
                describe_audio(item, base)
                for item in sorted(folder.iterdir(), key=lambda entry: entry.name)
                if item.is_file() and item.suffix.lower() in MEDIA_TYPES
            ]
        return result
    except (OSError, ValueError):
        return {}


def _default_model_dir_name(model_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", model_name)


def model_audio_root_path(
    config: dict[str, Any],
    model_name: Any,
    default_audio_dir: Path | None,
) -> Path | None:
    """Return a model's reference-audio root without creating it."""
    models = config.get("models", {})
    model = models.get(model_name) if isinstance(model_name, str) and isinstance(models, dict) else None
    if not isinstance(model, dict):
        return None
    configured = str(model.get("reference_audio_root", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if default_audio_dir is None:
        return None
    return Path(default_audio_dir) / _default_model_dir_name(model_name)


def _profile_params(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return default_emotion()
    return {field: copy.deepcopy(profile.get(field, value)) for field, value in default_emotion().items()}


def _has_style(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _tts_profile(
    config: dict[str, Any],
    character_name: str,
    character: dict[str, Any],
    emotion: str,
    params: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    result = dict(params)
    result["emotion"] = emotion
    result["character"] = character_name
    result["source"] = source
    model = config.get("models", {}).get(character.get("model_name", ""))
    if isinstance(model, dict):
        gpt = model.get("gpt_model_path")
        sovits = model.get("sovits_model_path")
        if isinstance(gpt, str) and gpt.strip() and isinstance(sovits, str) and sovits.strip():
            result["gpt_model_path"] = gpt.strip()
            result["sovits_model_path"] = sovits.strip()
    return result


def select_tts_profile(
    config: dict[str, Any],
    requested: str | None,
    *,
    no_tag_reason: str = "NoTag",
    audio_exists: Callable[[Any], bool] = _audio_exists,
    default_audio_dir: Path | None = None,
    rng: random.Random | None = None,
    discover: Callable[[Any], dict[str, list[dict[str, Any]]]] = discover_emotions,
) -> TtsSelection:
    """Pick the requested emotion, then the character default, then the global default.

    For each emotion a manual profile with an existing audio file wins; otherwise
    one usable file from the model's <root>/<emotion>/ folder is chosen at random
    (carrying the same-named manual profile's parameters and Irodori style); a
    manual profile with only a caption/emoji and no audio is returned as "style".
    reason is None only when the requested emotion was used without fallback.
    """
    character_name = config.get("default_character")
    characters = config.get("characters", {})
    character = (
        characters.get(character_name)
        if isinstance(character_name, str) and character_name and isinstance(characters, dict)
        else None
    )
    if not isinstance(character, dict):
        return TtsSelection(None, None, "NoCharacter")

    emotions = character.get("emotions")
    if not isinstance(emotions, dict):
        emotions = {}
    reason = no_tag_reason if requested is None else None
    candidates: list[str] = [] if requested is None else [requested]
    for fallback in (character.get("default_emotion"), config.get("default_emotion")):
        if isinstance(fallback, str) and fallback and fallback not in candidates:
            candidates.append(fallback)

    discovered: dict[str, list[dict[str, Any]]] | None = None
    chooser = rng if rng is not None else random
    for name in candidates:
        manual = emotions.get(name)
        if isinstance(manual, dict) and audio_exists(manual.get("ref_audio_path")):
            profile = _tts_profile(config, character_name, character, name, _profile_params(manual), "manual")
            return TtsSelection(profile, name, reason, "manual", 1, 1)

        if discovered is None:
            root = model_audio_root_path(config, character.get("model_name"), default_audio_dir)
            discovered = discover(root) if root is not None else {}
        usable = [audio for audio in discovered.get(name, []) if audio.get("usable")]
        if usable:
            # discover_emotions lists files sorted by name, so an index maps to one file.
            index = chooser.randrange(len(usable))
            chosen = usable[index]
            params = _profile_params(manual)
            params["ref_audio_path"] = chosen["path"]
            params["prompt_text"] = chosen["prompt_text"]
            profile = _tts_profile(config, character_name, character, name, params, "directory")
            return TtsSelection(profile, name, reason, "directory", len(usable), index + 1)

        if isinstance(manual, dict) and (
            _has_style(manual.get("caption")) or _has_style(manual.get("emoji"))
        ):
            profile = _tts_profile(config, character_name, character, name, _profile_params(manual), "style")
            return TtsSelection(profile, name, reason, "style", 0, 0)

        cause = "MissingAudio" if isinstance(manual, dict) or name in discovered else "UnknownEmotion"
        if reason is None:
            reason = cause
    return TtsSelection(None, None, reason or "NoUsableEmotion")


LEGACY_SONG_TEXT_FIELDS = ("accept_texts", "refuse_texts", "fail_texts", "limit_text")


def migrate_g1(config: dict[str, Any], replaced: list[tuple[str, str]] | None = None) -> bool:
    """G1 migration, in place: affinity names onto the characters, song texts into `texts`.

    Returns True when something changed (the caller backs the file up first and writes it back).
    A user's own values are carried over unchanged; a song text the new rules do not accept (an unknown
    {placeholder}, a bad length) is replaced by its default and listed in `replaced` as (key, reason).
    """
    if not isinstance(config, dict):
        return False
    changed = False
    affinity = config.get("affinity")
    characters = config.get("characters")
    if isinstance(affinity, dict) and ("character" in affinity or "display_name" in affinity) and isinstance(characters, dict) and characters:
        defaults = default_affinity_settings()
        default_name = config.get("default_character")
        if default_name not in characters:
            default_name = next(iter(characters))
        for name, character in characters.items():
            if not isinstance(character, dict):
                continue
            if name == default_name:
                st_name = affinity.get("character") or defaults["character"]
                nickname = affinity.get("display_name") or defaults["display_name"]
            else:
                st_name = nickname = name
            character.setdefault("st_name", st_name)
            character.setdefault("nickname", nickname)
        affinity.pop("character", None)
        affinity.pop("display_name", None)
        changed = True
    song_request = config.get("song_request")
    if isinstance(song_request, dict) and any(field in song_request for field in LEGACY_SONG_TEXT_FIELDS):
        texts = config.get("texts")
        fresh = not isinstance(texts, dict)
        if fresh:
            texts = config["texts"] = _texts_module().default_texts()
        moved = {field: song_request.pop(field) for field in LEGACY_SONG_TEXT_FIELDS if field in song_request}
        if fresh:
            checked, bad = _texts_module().lenient_texts(moved)
            texts.update({field: checked[field] for field in moved})
            if replaced is not None:
                replaced.extend(bad)
        changed = True
    return changed


def fill_config_defaults(config: dict[str, Any]) -> None:
    """Give a config written by an older version the sections it lacks, in place (also used for presets)."""
    config.setdefault("affinity", stored_affinity_defaults())
    config.setdefault("texts", _texts_module().default_texts())
    config.setdefault("network", _network_module().default_network_settings())
    config.setdefault("owner", _owner_module().default_owner_settings())
    config.setdefault("chat_platform", _chat_platform_module().default_chat_platform_settings())
    config.setdefault("install", _install_module().default_install_settings())
    config.setdefault("st_injection", _injection_module().default_injection_settings())
    fill_tier_descriptions(config.get("affinity"))
    config.setdefault("queue", default_queue_settings())
    config.setdefault("stickers", default_sticker_settings())
    config.setdefault("mood", default_mood_settings())
    config.setdefault("schedule", _schedule_module().default_schedule_section())
    if isinstance(config["schedule"], dict):  # L1d: a schedule section written before the proactive chat card
        config["schedule"].setdefault("proactive_chat", _schedule_module().default_proactive_chat())
    cover_defaults = _cover_module().default_cover_settings()
    if isinstance(config.get("cover"), dict):
        for name, value in cover_defaults.items():  # fields added since the config / preset was written
            config["cover"].setdefault(name, value)
    else:
        config.setdefault("cover", cover_defaults)
    config.setdefault("cleanup", _cleanup_module().default_cleanup_settings())
    config.setdefault("song_request", _song_request_module().stored_song_request_defaults())
    config["tts"] = validate_tts_settings(config.get("tts", {}), network=config["network"])


def _replace_with_retry(source: str, target: Path) -> None:
    """os.replace, retried while Windows reports a transient sharing lock."""
    for delay in (*_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


class ConfigStore:
    """Own config.json and the default reference-audio directory."""

    def __init__(self, data_dir: Path, logger: Any = None):
        self._logger = logger
        self.data_dir = Path(data_dir).resolve()
        self.audio_dir = self.data_dir / "audio"
        self.config_path = self.data_dir / "config.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._frozen = False
        self.config = self._load_or_create()

    def _load_or_create(self) -> dict[str, Any]:
        if not self.config_path.exists():
            config = default_config()
            self._atomic_write(config)
            return config
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("cannot read console config") from exc
        # Migrate first: the old song texts leave song_request before it is validated, and whatever they held
        # (an odd {placeholder}, a bad length) can only ever replace them by defaults, never fail the load.
        replaced: list[tuple[str, str]] = []
        migrated = migrate_g1(config, replaced)
        try:
            validate_config(config)
        except EmotionConfigError as exc:
            raise RuntimeError("console config has invalid entries") from exc
        if replaced:
            # The user's own wording stays in the backup; the console tells the user what was replaced.
            config["texts_notice"] = {"replaced": [key for key, _ in replaced], "backup": "config.json.bak-g1"}
            for key, reason in replaced:
                self._warn(f"[STBridge] A reply text was replaced by its default in the G1 upgrade (key={key} reason={reason})")
        if migrated:
            # Keep the file as it was before G1 (never overwrite an earlier backup), then write the migrated one.
            backup = self.config_path.with_name(self.config_path.name + ".bak-g1")
            if not backup.exists():
                shutil.copy2(self.config_path, backup)
        fill_config_defaults(config)
        if migrated:
            self._atomic_write(config)
        return config

    def _warn(self, message: str) -> None:
        logger = self._logger
        if logger is None:
            import logging

            logger = logging.getLogger(__name__)
        logger.warning(message)

    def _atomic_write(self, config: dict[str, Any]) -> None:
        validate_config(config)
        fd, temporary = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.config_path.exists():
                shutil.copy2(self.config_path, self.config_path.with_suffix(".json.bak"))
            _replace_with_retry(temporary, self.config_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def freeze(self) -> None:
        """Refuse every later update (an import just replaced config.json; the core restarts to read it)."""
        self._frozen = True

    async def update(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        async with self._lock:
            if self._frozen:
                raise EmotionConfigError(409, "核心正在重启，请稍后再试")
            candidate = copy.deepcopy(self.config)
            callback(candidate)
            validate_config(candidate)
            self._atomic_write(candidate)
            self.config = candidate
            return copy.deepcopy(candidate)

    def public(self) -> dict[str, Any]:
        result = copy.deepcopy(self.config)
        # Configs written by the standalone console carried a local token.
        result.pop("auth_token", None)
        if isinstance(result.get("cover"), dict):
            result["cover"] = _cover_module().public_cover_settings(result["cover"])
        return result

    def model_status(self, model: dict[str, Any]) -> dict[str, Any]:
        gpt = clean_path(model.get("gpt_model_path"), "gpt_model_path")
        sovits = clean_path(model.get("sovits_model_path"), "sovits_model_path")
        audio_root = clean_path(model.get("reference_audio_root"), "reference_audio_root")
        audio_exists = bool(audio_root and Path(audio_root).expanduser().exists())
        return {
            **model,
            "files": {
                "gpt_weights": bool(gpt and Path(gpt).expanduser().is_file()),
                "sovits_weights": bool(sovits and Path(sovits).expanduser().is_file()),
                "reference_audios": audio_exists,
            },
            "valid": bool(gpt and sovits),
        }

    def models(self) -> list[dict[str, Any]]:
        return [self.model_status(model) for model in self.config["models"].values()]

    def audio_root(self, model_name: str) -> Path | None:
        """A model's reference-audio root, without creating it."""
        root = model_audio_root_path(self.config, model_name, self.audio_dir)
        return None if root is None else root.resolve()

    def model_audio_root(self, model_name: str) -> Path:
        root = self.audio_root(model_name)
        if root is None:
            raise EmotionConfigError(404, "model not found")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def list_audio(self, model_name: str) -> list[dict[str, Any]]:
        root = self.model_audio_root(model_name)
        result = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MEDIA_TYPES:
                continue
            info = describe_audio(path, root)
            parts = info["relative_path"].split("/")
            stat = path.stat()
            result.append(
                {
                    "path": info["path"],
                    "relative_path": info["relative_path"],
                    "filename": info["filename"],
                    "language": parts[0] if len(parts) > 2 else "default",
                    "emotion": path.parent.name if path.parent != root else "default",
                    "prompt_text": info["prompt_text"],
                    "duration": info["duration"],
                    "out_of_range": info["out_of_range"],
                    "readable": info["readable"],
                    "usable": info["usable"],
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        return result

    def discovered(self, model_name: str) -> list[dict[str, Any]]:
        """List discovered emotions; "pick" numbers usable files like the TTS log does."""
        root = self.audio_root(model_name)
        found = discover_emotions(root) if root is not None else {}
        result = []
        for name, audios in found.items():
            entries = []
            pick = 0
            for audio in audios:
                if audio["usable"]:
                    pick += 1
                entries.append(
                    {
                        "relative_path": audio["relative_path"],
                        "prompt_text": audio["prompt_text"],
                        "duration": audio["duration"],
                        "out_of_range": audio["out_of_range"],
                        "readable": audio["readable"],
                        "usable": audio["usable"],
                        "pick": pick if audio["usable"] else None,
                    }
                )
            result.append({"name": name, "available": pick, "audios": entries})
        return result

    def resolve_audio(self, model_name: str, relative_path: str) -> Path:
        root = self.model_audio_root(model_name)
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise EmotionConfigError(400, "invalid audio path")
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_TYPES:
            raise EmotionConfigError(404, "audio not found")
        return candidate
