"""Reply texts: every fixed line the bridge says (errors, queue, song requests), editable in the console.

Pure logic with no AstrBot or web-framework dependency. The texts live in the config section `texts`;
`{昵称}` stands for the current character's nickname and is filled in when a line is sent.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .emotion_config import EmotionConfigError

NICKNAME_PLACEHOLDER = "{昵称}"
SONG_NAME_PLACEHOLDER = "{歌名}"
LIMIT_PLACEHOLDER = "{次数}"
TEXT_MIN_LENGTH = 1
TEXT_MAX_LENGTH = 100
LINES_MAX_COUNT = 10

DEFAULT_ACCEPT_TEXTS = [
    "好～《{歌名}》对吧，我去准备一下，稍等我一会儿！",
    "收到！《{歌名}》……让我先练一下，马上就好～",
]
DEFAULT_REFUSE_TEXTS = [
    "诶……今天嗓子不太在状态，下次再唱给你听吧。",
    "唱歌的话……再让我多练习一阵子嘛。",
]
DEFAULT_FAIL_TEXTS = ["呜……这首没能唱好，换一首试试吧？"]
DEFAULT_LIMIT_TEXT = "今天已经唱了{次数}首啦，嗓子要休息一下，明天再来吧～"

GROUP_ERRORS = "排队与出错"
# What "unavailable" said before H1: a config that still holds exactly this (nobody edited it) reads as the new default.
OLD_UNAVAILABLE_DEFAULT = "桥接当前不可用，请检查服务状态与恢复保护。"
GROUP_SONG = "点歌"
GROUP_OWNER = "认领主人"

# (key, kind, group, label, extra placeholders, default). kind: "text" is one line, "lines" is 1-10 lines (one is picked at random).
TEXT_DEFS: tuple[tuple[str, str, str, str, tuple[str, ...], Any], ...] = (
    ("invalid_reply", "text", GROUP_ERRORS, "ST 回复的格式不对，没能送达", (), "（这次回复的格式不对，没能送达，请再说一次）"),
    ("too_long", "text", GROUP_ERRORS, "消息太长，没有创建请求", (), "消息超过桥接文本长度限制，未创建请求。"),
    ("unavailable", "text", GROUP_ERRORS, "角色不在线时回复别人的话（ST 没连上、还没就绪时）", (), "{昵称}现在不在线哦，晚点再来找我～"),
    ("queue_full", "text", GROUP_ERRORS, "排队的人太多，这条被拒绝", (), "现在找{昵称}的人太多啦，请稍后再来～"),
    ("queue_timeout", "text", GROUP_ERRORS, "排队太久，这条没能处理", (), "排队太久了，这条没能处理，请稍后再说一次。"),
    ("song_busy", "text", GROUP_SONG, "上一首还在准备时又有人点歌", (), "上一首还在准备中，等我唱完这首哦～"),
    ("song_queue_full", "text", GROUP_SONG, "点歌的人太多，翻唱队列满了", (), "现在点歌的人太多了，等一会儿再来吧"),
    ("song_usage", "text", GROUP_SONG, "命令里没写歌名时的用法提示", (), "用法：唱 歌名（比如：唱 晴天）"),
    ("song_cancel", "text", GROUP_SONG, "选歌时回复“取消”", (), "好，那下次再点吧～"),
    ("song_research_hint", "text", GROUP_SONG, "选歌时只回复了 0（提示怎么重新搜）", (), "0 后面写上歌手或专辑，比如：0 周杰伦"),
    ("song_research_empty", "text", GROUP_SONG, "重新搜索没有结果", (), "没搜到更合适的，换个歌手或专辑再试试～"),
    ("song_pick_range", "text", GROUP_SONG, "选歌时回复的序号不在列表里", (), "没有这个序号哦，请回复列表里有的序号～"),
    ("accept_texts", "lines", GROUP_SONG, "答应点歌时说的话（每行一句，随机选一句）", (SONG_NAME_PLACEHOLDER,), DEFAULT_ACCEPT_TEXTS),
    ("refuse_texts", "lines", GROUP_SONG, "不答应点歌时说的话（每行一句，随机选一句）", (), DEFAULT_REFUSE_TEXTS),
    ("fail_texts", "lines", GROUP_SONG, "没唱成时说的话（每行一句，随机选一句）", (SONG_NAME_PLACEHOLDER,), DEFAULT_FAIL_TEXTS),
    ("limit_text", "text", GROUP_SONG, "今天点满了", (LIMIT_PLACEHOLDER,), DEFAULT_LIMIT_TEXT),
    ("owner_claimed", "text", GROUP_OWNER, "认领主人成功后回复的话", (), "认领成功，以后你就是主人啦。"),
)
TEXT_KEYS = tuple(item[0] for item in TEXT_DEFS)
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


def default_texts() -> dict[str, Any]:
    return {key: copy.deepcopy(default) for key, _, _, _, _, default in TEXT_DEFS}


def text_meta() -> list[dict[str, Any]]:
    """What the console shows for each text: group, purpose, kind and the placeholders it accepts."""
    return [
        {"key": key, "kind": kind, "group": group, "label": label, "placeholders": [NICKNAME_PLACEHOLDER, *extra]}
        for key, kind, group, label, extra, _ in TEXT_DEFS
    ]


def unknown_placeholders(text: str, allowed: tuple[str, ...] | list[str]) -> list[str]:
    """Placeholders in `text` that are not in `allowed` (each as written, braces included)."""
    return [match.group(0) for match in _PLACEHOLDER_RE.finditer(text) if match.group(0) not in allowed]


def _line_problem(value: Any, allowed: list[str]) -> tuple[str | None, str | None, str]:
    """(clean text, reason code, detail) for one line; the reason is None when the line is fine."""
    if not isinstance(value, str):
        return None, "type", "not a string"
    text = value.strip()
    if not TEXT_MIN_LENGTH <= len(text) <= TEXT_MAX_LENGTH:
        return None, "length", f"1-{TEXT_MAX_LENGTH} characters"
    unknown = unknown_placeholders(text, allowed)
    if unknown:
        return None, "unknown_placeholder", f"unknown placeholder {unknown[0]} (allowed: {' '.join(allowed)})"
    return text, None, ""


def _item_problem(kind: str, extra: tuple[str, ...], value: Any) -> tuple[Any, str | None, str]:
    """(clean value, reason code, detail) for one text of the section."""
    allowed = [NICKNAME_PLACEHOLDER, *extra]
    if kind == "lines":
        if not isinstance(value, list):
            return None, "type", "not a list"
        if not 1 <= len(value) <= LINES_MAX_COUNT:
            return None, "count", f"1-{LINES_MAX_COUNT} lines"
        lines = []
        for item in value:
            text, reason, detail = _line_problem(item, allowed)
            if reason:
                return None, reason, detail
            lines.append(text)
        return lines, None, ""
    return _line_problem(value, allowed)


def validate_texts(section: Any) -> dict[str, Any]:
    """Validate the texts section (a save from the console): missing keys take the defaults; a bad text is a 400."""
    if not isinstance(section, dict):
        raise EmotionConfigError(400, "texts must be an object")
    merged = {**default_texts(), **{key: value for key, value in section.items() if key in TEXT_KEYS}}
    result: dict[str, Any] = {}
    for key, kind, _, _, extra, _ in TEXT_DEFS:
        clean, reason, detail = _item_problem(kind, extra, merged[key])
        if reason:
            raise EmotionConfigError(400, f"texts.{key}: {detail}")
        result[key] = clean
    return result


def lenient_texts(section: Any) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Texts read from the config: every text that is not fine is replaced by its default, one by one.

    Returns (texts, [(key, reason code)] of the replaced ones). Never raises.
    """
    result = default_texts()
    replaced: list[tuple[str, str]] = []
    if not isinstance(section, dict):
        return result, replaced
    for key, kind, _, _, extra, _ in TEXT_DEFS:
        if key not in section:
            continue
        clean, reason, _ = _item_problem(kind, extra, section[key])
        if reason:
            replaced.append((key, reason))
        elif key == "unavailable" and clean == OLD_UNAVAILABLE_DEFAULT:
            continue
        else:
            result[key] = clean
    return result, replaced


def texts_settings(config: Any) -> dict[str, Any]:
    """Texts from a config; a text that is not valid falls back to its own default, the others stay."""
    try:
        section = config.get("texts", {})
    except AttributeError:
        return default_texts()
    return lenient_texts(section)[0]


def fill_nickname(text: str, nickname: str) -> str:
    """`{昵称}` replaced by the nickname (plain replace: user text may hold other braces)."""
    return text.replace(NICKNAME_PLACEHOLDER, nickname)


def fill_song_name(text: str, name: str) -> str:
    """`{歌名}` replaced by the song name (plain replace)."""
    return text.replace(SONG_NAME_PLACEHOLDER, name)


def fill_limit(text: str, limit: int) -> str:
    """`{次数}` replaced by the daily limit (plain replace)."""
    return text.replace(LIMIT_PLACEHOLDER, str(limit))
