"""Scheduled proactive messages: config validation and the preset templates.

No web-framework dependency. The `schedule` section of the console config holds
`settings` and `plans`; presets are only templates that are written as ordinary
(disabled) plans the first time the section is created.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from typing import Any

from .emotion_config import EmotionConfigError
from .proactive_chat_config import default_proactive_chat, validate_proactive_chat


QUIET_MINUTES_RANGE = (0, 120)
MAX_DEFER_MINUTES_RANGE = (0, 240)
MIN_GAP_MINUTES_RANGE = (0, 240)
DEDUPE_RATIO_RANGE = (0.5, 1.0)
MAX_PLANS = 50
MAX_NAME_CHARS = 40
MAX_PROMPT_CHARS = 300
MAX_FALLBACK_TEXTS = 5
MAX_FALLBACK_CHARS = 200
JITTER_RANGE = (0, 60)
EVERY_MINUTES_RANGE = (15, 720)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_GROUP_KEY_RE = re.compile(r"^[0-9a-f]{8}$")
PLAN_KINDS = ("daily", "interval")


def default_schedule_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "quiet_minutes": 10,
        "max_defer_minutes": 60,
        "min_gap_minutes": 30,
        "dedupe_ratio": 0.8,
    }


def _int(value: Any, label: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise EmotionConfigError(400, f"invalid {label}")
    parsed = int(value)
    if not bounds[0] <= parsed <= bounds[1]:
        raise EmotionConfigError(400, f"invalid {label}")
    return parsed


def validate_schedule_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "schedule settings must be an object")
    merged = {**default_schedule_settings(), **settings}
    if not isinstance(merged.get("enabled"), bool):
        raise EmotionConfigError(400, "invalid schedule enabled")
    ratio = merged.get("dedupe_ratio")
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(ratio)
        or not DEDUPE_RATIO_RANGE[0] <= ratio <= DEDUPE_RATIO_RANGE[1]
    ):
        raise EmotionConfigError(400, "invalid dedupe ratio")
    return {
        "enabled": merged["enabled"],
        "quiet_minutes": _int(merged.get("quiet_minutes"), "quiet minutes", QUIET_MINUTES_RANGE),
        "max_defer_minutes": _int(
            merged.get("max_defer_minutes"), "max defer minutes", MAX_DEFER_MINUTES_RANGE
        ),
        "min_gap_minutes": _int(merged.get("min_gap_minutes"), "min gap minutes", MIN_GAP_MINUTES_RANGE),
        "dedupe_ratio": float(ratio),
    }


def parse_hhmm(value: Any, label: str) -> int:
    """Minutes since midnight of an "HH:MM" string."""
    match = _TIME_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise EmotionConfigError(400, f"invalid {label}")
    return int(match.group(1)) * 60 + int(match.group(2))


def _text(value: Any, label: str, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip()
    if (required and not text) or len(text) > limit:
        raise EmotionConfigError(400, f"invalid {label}")
    return text


def _validate_target(target: Any) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise EmotionConfigError(400, "invalid plan target")
    kind = target.get("type")
    if kind == "private":
        return {"type": "private"}
    if kind == "group":
        key = target.get("key")
        if not isinstance(key, str) or not _GROUP_KEY_RE.fullmatch(key):
            raise EmotionConfigError(400, "invalid plan group")
        return {"type": "group", "key": key}
    raise EmotionConfigError(400, "invalid plan target")


def validate_plan(plan: Any) -> dict[str, Any]:
    """Validate one plan; the id must already be set (see new_plan_id)."""
    if not isinstance(plan, dict):
        raise EmotionConfigError(400, "plan must be an object")
    plan_id = plan.get("id")
    if not isinstance(plan_id, str) or not _ID_RE.fullmatch(plan_id):
        raise EmotionConfigError(400, "invalid plan id")
    enabled = plan.get("enabled", True)
    if not isinstance(enabled, bool):
        raise EmotionConfigError(400, "invalid plan enabled")
    kind = plan.get("kind")
    if kind not in PLAN_KINDS:
        raise EmotionConfigError(400, "invalid plan kind")
    result: dict[str, Any] = {
        "id": plan_id,
        "name": _text(plan.get("name"), "plan name", MAX_NAME_CHARS, required=True),
        "enabled": enabled,
        "kind": kind,
    }
    if kind == "daily":
        parse_hhmm(plan.get("time"), "plan time")
        weekdays = plan.get("weekdays", [])
        if not isinstance(weekdays, list) or any(
            isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6
            for day in weekdays
        ):
            raise EmotionConfigError(400, "invalid plan weekdays")
        result["time"] = plan["time"]
        result["weekdays"] = sorted(set(weekdays))
        result["jitter_minutes"] = _int(plan.get("jitter_minutes", 0), "plan jitter", JITTER_RANGE)
    else:
        start = parse_hhmm(plan.get("start"), "plan start")
        end = parse_hhmm(plan.get("end"), "plan end")
        if start >= end:
            raise EmotionConfigError(400, "plan start must be before end")
        result["start"] = plan["start"]
        result["end"] = plan["end"]
        result["every_minutes"] = _int(plan.get("every_minutes"), "plan interval", EVERY_MINUTES_RANGE)
    result["target"] = _validate_target(plan.get("target", {"type": "private"}))
    result["prompt"] = _text(plan.get("prompt"), "plan prompt", MAX_PROMPT_CHARS, required=True)
    fallbacks = plan.get("fallback_texts", [])
    if not isinstance(fallbacks, list) or len(fallbacks) > MAX_FALLBACK_TEXTS:
        raise EmotionConfigError(400, "invalid plan fallback texts")
    result["fallback_texts"] = [
        text
        for text in (_text(item, "plan fallback text", MAX_FALLBACK_CHARS, required=False) for item in fallbacks)
        if text
    ]
    return result


def new_plan_id() -> str:
    return secrets.token_hex(4)


def validate_schedule_section(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise EmotionConfigError(400, "schedule must be an object")
    plans_value = section.get("plans", [])
    if not isinstance(plans_value, list) or len(plans_value) > MAX_PLANS:
        raise EmotionConfigError(400, "invalid schedule plans")
    plans = [validate_plan(plan) for plan in plans_value]
    if len({plan["id"] for plan in plans}) != len(plans):
        raise EmotionConfigError(400, "duplicate plan id")
    return {
        "settings": validate_schedule_settings(section.get("settings", {})),
        "plans": plans,
        "proactive_chat": validate_proactive_chat(section.get("proactive_chat", {})),
    }


# ---- preset templates ---------------------------------------------------------------

# Templates for "add from template". The first creation of the section writes all of them as
# ordinary plans (disabled), so nothing is sent until the user switches a plan on.
PLAN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "key": "morning",
        "name": "早安",
        "kind": "daily",
        "time": "09:30",
        "weekdays": [],
        "jitter_minutes": 10,
        "prompt": "现在是早上，制作人刚起床的时间。用你自己的口吻向制作人道早安，简短温柔，可以提醒制作人吃早饭、看看今天的安排。不要超过三句话。",
        "fallback_texts": [
            "制作人，早安。今天也请多多关照。",
            "早上好，制作人。记得吃早饭哦。",
            "制作人，起床了吗？今天也一起加油吧。",
        ],
    },
    {
        "key": "lunch",
        "name": "午饭",
        "kind": "daily",
        "time": "12:00",
        "weekdays": [],
        "jitter_minutes": 10,
        "prompt": "现在是午饭时间。用你自己的口吻提醒制作人去吃午饭，不要空着肚子工作。简短自然，不超过三句话。",
        "fallback_texts": [
            "制作人，到午饭时间了。请好好吃饭。",
            "已经中午了，制作人。休息一下，去吃点东西吧。",
            "制作人，午饭吃了吗？不要太勉强自己。",
        ],
    },
    {
        "key": "dinner",
        "name": "晚饭",
        "kind": "daily",
        "time": "18:30",
        "weekdays": [],
        "jitter_minutes": 10,
        "prompt": "现在是晚饭时间。用你自己的口吻提醒制作人去吃晚饭，今天辛苦了。简短自然，不超过三句话。",
        "fallback_texts": [
            "制作人，晚饭时间到了。今天也辛苦了。",
            "该吃晚饭了，制作人。要好好补充体力哦。",
            "制作人，工作先放一放，一起去吃晚饭吧。",
        ],
    },
    {
        "key": "water",
        "name": "喝水",
        "kind": "interval",
        "start": "10:00",
        "end": "20:00",
        "every_minutes": 180,
        "prompt": "提醒制作人喝点水，顺便活动一下身体、让眼睛休息一下。语气轻柔，一两句话就好。",
        "fallback_texts": [
            "制作人，该喝水了。",
            "稍微休息一下，喝点水吧，制作人。",
        ],
    },
    {
        "key": "bedtime",
        "name": "睡觉提醒",
        "kind": "daily",
        "time": "00:00",
        "weekdays": [],
        "jitter_minutes": 5,
        "prompt": "时间不早了。用你自己的口吻提醒制作人准备休息，不要熬夜，早点睡觉。语气温柔，不超过三句话。",
        "fallback_texts": [
            "制作人，时间不早了，该准备休息了。",
            "不要熬夜哦，制作人。早点睡吧。",
            "已经很晚了，制作人。今天就到这里吧。",
        ],
    },
    {
        "key": "goodnight",
        "name": "晚安",
        "kind": "daily",
        "time": "23:30",
        "weekdays": [],
        "jitter_minutes": 5,
        "prompt": "已经是睡觉的时间了。用你自己的口吻向制作人道晚安，说一句今天的感想或明天的期待。简短温柔，不超过三句话。",
        "fallback_texts": [
            "制作人，晚安。明天也请多多关照。",
            "晚安，制作人。做个好梦。",
            "今天辛苦了，制作人。晚安。",
        ],
    },
)


def template_list() -> list[dict[str, Any]]:
    """Templates as plan-shaped dicts without an id (the page posts one to add it)."""
    result = []
    for template in PLAN_TEMPLATES:
        item = {key: (list(value) if isinstance(value, list) else value) for key, value in template.items()}
        item["enabled"] = False
        item["target"] = {"type": "private"}
        result.append(item)
    return result


def _preset_id(key: str) -> str:
    # Stable ids: a section that was seeded in memory but never saved looks the same after a reload.
    return hashlib.sha1(f"schedule-preset:{key}".encode()).hexdigest()[:8]


def default_schedule_section() -> dict[str, Any]:
    plans = []
    for template in template_list():
        template.pop("key")
        template["id"] = _preset_id(PLAN_TEMPLATES[len(plans)]["key"])
        plans.append(validate_plan(template))
    return {"settings": default_schedule_settings(), "plans": plans, "proactive_chat": default_proactive_chat()}


def schedule_section(config: Any) -> dict[str, Any]:
    """Validated schedule section from a config, falling back to settings defaults and no plans."""
    try:
        return validate_schedule_section(config.get("schedule", {}))
    except (AttributeError, EmotionConfigError):
        return {"settings": default_schedule_settings(), "plans": [], "proactive_chat": default_proactive_chat()}
