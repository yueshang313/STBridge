"""Settings of the owner private-chat proactive messages (L1d "主动搭话", 3a): defaults, validation, time windows.

The section lives at ``schedule.proactive_chat``. No web-framework dependency.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .emotion_config import EmotionConfigError


SILENCE_RANGE = (30, 1440)
FOLLOWUP_FACTOR_RANGE = (1.0, 4.0)
MAX_UNANSWERED_RANGE = (0, 10)
DAILY_LIMIT_RANGE = (0, 20)
MAX_WINDOWS = 6
MAX_PROMPT_CHARS = 300
ELAPSED_PLACEHOLDER = "{{elapsed}}"
DEFAULT_PROMPT = (
    "你有一阵没和制作人说话了（上次说话是 {{elapsed}} 前），现在想主动找他。"
    "可以从你的 life_log（进行中的意外、快到时间的约定）、最近聊过的话题或现在的心情里找一个自然的理由；"
    "没有特别的事，也可以只是想他了。像平常发消息一样简短，不要像在完成任务。"
)
UNANSWERED_NOTE = "（你上次主动找他，他还没回。）"
DUPLICATE_NOTE = "这是一条独立的主动消息，不是在回复上一条，不要重复你之前说过的话。"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def default_proactive_chat() -> dict[str, Any]:
    return {
        "enabled": True,
        "silence_min": 120,
        "silence_max": 240,
        "followup_factor": 1.5,
        "max_unanswered": 2,
        "daily_limit": 3,
        "quiet_hours": {"start": "00:00", "end": "08:00"},
        "active_windows": {
            "weekday": [{"start": "12:00", "end": "13:30"}, {"start": "16:30", "end": "23:30"}],
            "weekend": [{"start": "10:00", "end": "23:30"}],
        },
        "prompt": DEFAULT_PROMPT,
    }


def _minutes(value: Any, label: str) -> int:
    match = _TIME_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise EmotionConfigError(400, f"invalid {label}")
    return int(match.group(1)) * 60 + int(match.group(2))


def _int(value: Any, label: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise EmotionConfigError(400, f"invalid {label}")
    parsed = int(value)
    if not bounds[0] <= parsed <= bounds[1]:
        raise EmotionConfigError(400, f"invalid {label}")
    return parsed


def _window(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EmotionConfigError(400, f"invalid {label}")
    start = _minutes(value.get("start"), f"{label} start")
    end = _minutes(value.get("end"), f"{label} end")
    if start >= end:
        raise EmotionConfigError(400, f"{label} start must be before end")
    return {"start": value["start"], "end": value["end"]}


def _windows(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_WINDOWS:
        raise EmotionConfigError(400, f"invalid {label}")
    result = sorted((_window(item, label) for item in value), key=lambda item: item["start"])
    return result


def validate_proactive_chat(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EmotionConfigError(400, "proactive chat must be an object")
    defaults = default_proactive_chat()
    merged = {**defaults, **value}
    if not isinstance(merged.get("enabled"), bool):
        raise EmotionConfigError(400, "invalid proactive chat enabled")
    silence_min = _int(merged.get("silence_min"), "silence min", SILENCE_RANGE)
    silence_max = _int(merged.get("silence_max"), "silence max", SILENCE_RANGE)
    if silence_min > silence_max:
        raise EmotionConfigError(400, "silence min must not exceed silence max")
    factor = merged.get("followup_factor")
    if (
        isinstance(factor, bool)
        or not isinstance(factor, (int, float))
        or not math.isfinite(factor)
        or not FOLLOWUP_FACTOR_RANGE[0] <= factor <= FOLLOWUP_FACTOR_RANGE[1]
    ):
        raise EmotionConfigError(400, "invalid followup factor")
    quiet = merged.get("quiet_hours")
    if not isinstance(quiet, dict):
        raise EmotionConfigError(400, "invalid quiet hours")
    _minutes(quiet.get("start"), "quiet hours start")
    _minutes(quiet.get("end"), "quiet hours end")
    windows = merged.get("active_windows")
    if not isinstance(windows, dict):
        raise EmotionConfigError(400, "invalid active windows")
    prompt = merged.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > MAX_PROMPT_CHARS:
        raise EmotionConfigError(400, "invalid proactive chat prompt")
    return {
        "enabled": merged["enabled"],
        "silence_min": silence_min,
        "silence_max": silence_max,
        "followup_factor": float(factor),
        "max_unanswered": _int(merged.get("max_unanswered"), "max unanswered", MAX_UNANSWERED_RANGE),
        "daily_limit": _int(merged.get("daily_limit"), "daily limit", DAILY_LIMIT_RANGE),
        "quiet_hours": {"start": quiet["start"], "end": quiet["end"]},
        "active_windows": {
            "weekday": _windows(windows.get("weekday", []), "weekday window"),
            "weekend": _windows(windows.get("weekend", []), "weekend window"),
        },
        "prompt": prompt.strip(),
    }


def proactive_chat_settings(config: Any) -> dict[str, Any]:
    """Validated settings from a whole config; a missing or broken section reads as the defaults."""
    try:
        return validate_proactive_chat(config.get("schedule", {}).get("proactive_chat", {}))
    except (AttributeError, EmotionConfigError):
        return default_proactive_chat()


# ---- time windows ---------------------------------------------------------------------


def in_quiet_hours(settings: dict[str, Any], minute_of_day: int) -> bool:
    """Is the minute inside the do-not-disturb range (may cross midnight; equal ends = never)?"""
    start = _minutes(settings["quiet_hours"]["start"], "quiet hours start")
    end = _minutes(settings["quiet_hours"]["end"], "quiet hours end")
    if start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def quiet_hours_end(settings: dict[str, Any]) -> int:
    return _minutes(settings["quiet_hours"]["end"], "quiet hours end")


def windows_for_day(settings: dict[str, Any], weekday: int) -> list[tuple[int, int]]:
    """Allowed (start, end) minutes of the day; weekday 5 and 6 (Saturday, Sunday) use the weekend list."""
    key = "weekend" if weekday >= 5 else "weekday"
    return [
        (_minutes(item["start"], "window start"), _minutes(item["end"], "window end"))
        for item in settings["active_windows"][key]
    ]


def window_state(settings: dict[str, Any], weekday: int, minute_of_day: int) -> tuple[bool, int | None]:
    """(inside an allowed window now, minutes of the day the current window ends or None).

    An empty list for the day means "no limit" (inside, no end).
    """
    windows = windows_for_day(settings, weekday)
    if not windows:
        return True, None
    for start, end in windows:
        if start <= minute_of_day < end:
            return True, end
    return False, None


def next_window_start(settings: dict[str, Any], weekday: int, minute_of_day: int) -> tuple[int, int, int]:
    """The next allowed window start after now: (days ahead, start minute, window length in minutes).

    Looks over the coming eight days; (0, minute_of_day, 0) when no day has a window. A later day whose list is
    empty means "no limit" there: it opens at 00:00 and lasts the whole day.
    """
    for ahead in range(0, 8):
        day = (weekday + ahead) % 7
        windows = windows_for_day(settings, day)
        if not windows and ahead >= 1:
            return ahead, 0, 24 * 60
        for start, end in sorted(windows):
            if ahead == 0 and start <= minute_of_day:
                continue
            return ahead, start, end - start
    return 0, minute_of_day, 0
