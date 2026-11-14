"""Chat song requests: settings, the numbered pick list, reply parsing and NetEase link parsing.

Pure logic with no AstrBot or web-framework dependency; the plugin glue lives in main.py.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from .emotion_config import EmotionConfigError
from .music_source import SOURCE_LABELS, is_instrumental

LIST_SOURCES = ("wy", "kg")
LIBRARY_LABEL = "歌库"
NAME_LIMIT = 30
ARTIST_LIMIT = 20
ALBUM_LIMIT = 20
TEXT_MIN_LENGTH = 1
TEXT_MAX_LENGTH = 100
TEXTS_MAX_COUNT = 10
SONG_NAME_PLACEHOLDER = "{歌名}"

# The reply texts moved to the `texts` section in G1; the defaults and the {歌名} / {次数} fillers live there.
from .reply_texts import (
    DEFAULT_ACCEPT_TEXTS,
    DEFAULT_FAIL_TEXTS,
    DEFAULT_LIMIT_TEXT,
    DEFAULT_REFUSE_TEXTS,
    LIMIT_PLACEHOLDER,
    fill_limit,
    fill_song_name,
    texts_settings,
)

# Importable from here as before.
__all__ = ["LIMIT_PLACEHOLDER", "fill_limit", "fill_song_name"]

TEXT_FIELDS = ("accept_texts", "refuse_texts", "fail_texts")
PERMISSION_VARIABLE_MAX_CHARS = 40
DEFAULT_PERMISSION_VARIABLE = "点歌许可"
# passive: the voice is a reply to the message that picked the song; proactive: send_message (P7c behaviour).
DELIVERY_MODES = ("passive", "proactive")


def default_song_request_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "owner_min_affinity": 0,
        "group_min_affinity": 0,
        "refuse_percent": 0,
        "list_timeout_seconds": 180,
        "per_source": 5,
        "delivery_mode": "passive",
        "daily_limit": 5,
        "limit_owner": False,
        "permission_variable": DEFAULT_PERMISSION_VARIABLE,
        "limit_text": DEFAULT_LIMIT_TEXT,
        "accept_texts": list(DEFAULT_ACCEPT_TEXTS),
        "refuse_texts": list(DEFAULT_REFUSE_TEXTS),
        "fail_texts": list(DEFAULT_FAIL_TEXTS),
    }


def stored_song_request_defaults() -> dict[str, Any]:
    """The settings as stored in config.json: the reply texts live in the `texts` section."""
    settings = default_song_request_settings()
    for field in (*TEXT_FIELDS, "limit_text"):
        del settings[field]
    return settings


def _int(value: Any, label: str, low: int, high: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value != int(value)
        or not low <= int(value) <= high
    ):
        raise EmotionConfigError(400, f"invalid {label}")
    return int(value)


def _texts(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= TEXTS_MAX_COUNT:
        raise EmotionConfigError(400, f"invalid {label}")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise EmotionConfigError(400, f"invalid {label}")
        text = item.strip()
        if not TEXT_MIN_LENGTH <= len(text) <= TEXT_MAX_LENGTH:
            raise EmotionConfigError(400, f"invalid {label}")
        result.append(text)
    return result


def validate_song_request_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "song request settings must be an object")
    merged = {**default_song_request_settings(), **settings}
    if not isinstance(merged["enabled"], bool):
        raise EmotionConfigError(400, "invalid song request enabled")
    if merged["delivery_mode"] not in DELIVERY_MODES:
        raise EmotionConfigError(400, "invalid delivery_mode")
    if not isinstance(merged["limit_owner"], bool):
        raise EmotionConfigError(400, "invalid limit_owner")
    limit_text = merged["limit_text"]
    if not isinstance(limit_text, str) or not TEXT_MIN_LENGTH <= len(limit_text.strip()) <= TEXT_MAX_LENGTH:
        raise EmotionConfigError(400, "invalid limit_text")
    variable = merged["permission_variable"]
    if (
        not isinstance(variable, str)
        or not 1 <= len(variable.strip()) <= PERMISSION_VARIABLE_MAX_CHARS
        or re.search(r"[.\[\]\r\n]", variable)
    ):
        raise EmotionConfigError(400, "invalid permission_variable")
    result = {
        "enabled": merged["enabled"],
        "delivery_mode": merged["delivery_mode"],
        "permission_variable": variable.strip(),
        "daily_limit": _int(merged["daily_limit"], "daily_limit", 0, 100),
        "limit_owner": merged["limit_owner"],
        "limit_text": limit_text.strip(),
        "owner_min_affinity": _int(merged["owner_min_affinity"], "owner_min_affinity", 0, 100),
        "group_min_affinity": _int(merged["group_min_affinity"], "group_min_affinity", -50, 100),
        "refuse_percent": _int(merged["refuse_percent"], "refuse_percent", 0, 100),
        "list_timeout_seconds": _int(merged["list_timeout_seconds"], "list_timeout_seconds", 60, 600),
        "per_source": _int(merged["per_source"], "per_source", 1, 10),
    }
    for field in TEXT_FIELDS:
        result[field] = _texts(merged[field], field)
    return result


def song_request_settings(config: Any) -> dict[str, Any]:
    """The song-request settings; the four text fields come from the `texts` section (still holding `{昵称}`)."""
    try:
        settings = validate_song_request_settings(config.get("song_request", {}))
    except (AttributeError, EmotionConfigError):
        settings = default_song_request_settings()
    texts = texts_settings(config)
    for field in (*TEXT_FIELDS, "limit_text"):
        settings[field] = texts[field]
    return settings


# ---- NetEase links ----------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、）)\]】]+", re.IGNORECASE)
_SONG_PATH_RE = re.compile(r"/song/(\d{1,12})(?:[/?#&]|$)")
_SONG_ID_QUERY_RE = re.compile(r"[?&#/]id=(\d{1,12})(?:[&#]|$)")
_LONG_HOSTS = ("music.163.com", "y.music.163.com")
_SHORT_HOSTS = ("163cn.tv",)


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/?#:]+)", url, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def parse_netease_link(text: Any) -> tuple[str, Any] | None:
    """("id", song id) for a long NetEase song link, ("short", url) for a 163cn.tv link, else None.

    Playlists, albums and MVs are not songs and give None, as do links of other sites.
    """
    if not isinstance(text, str):
        return None
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        host = _host_of(url)
        if host in _SHORT_HOSTS:
            return "short", url
        if host not in _LONG_HOSTS:
            continue
        rest = url[url.lower().index(host) + len(host):]
        # Song pages: /song?id=N, /#/song?id=N, /m/song?id=N and /song/N. Album, playlist and MV pages have no /song.
        song_part = re.search(r"/song(?:/|\?|$)", rest)
        if song_part is None:
            continue
        tail = rest[song_part.start():]
        direct = _SONG_PATH_RE.match(tail)
        if direct is not None:
            return "id", direct.group(1)
        query = _SONG_ID_QUERY_RE.search(tail)
        if query is not None:
            return "id", query.group(1)
    return None


# ---- sing tag ---------------------------------------------------------------------------

_SING_TAG_RE = re.compile(r"\[唱歌[:：]([^\]\r\n]{1,60})\]")
_NAME_QUOTES = "《》「」\"'“”‘’"


def strip_sing_tags(text: Any) -> tuple[str, str | None]:
    """(text without any [唱歌:歌名] tag, the first tag's song name or None)."""
    if not isinstance(text, str):
        return "", None
    first = None
    for match in _SING_TAG_RE.finditer(text):
        name = match.group(1).strip().strip(_NAME_QUOTES).strip()
        if name and first is None:
            first = name
    return _SING_TAG_RE.sub("", text), first


# ---- the numbered list --------------------------------------------------------------------


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line(number: int, item: dict[str, Any]) -> str:
    line = f"{number}. {item['name']}"
    if item["artist"]:
        line += f" — {item['artist']}"
    if item["album"]:
        line += f"（{item['album']}）"
    return f"{line}[{item['label']}]"


def build_song_list(
    query: str,
    library_hits: list[dict[str, Any]],
    results_by_source: dict[str, list[dict[str, Any]]],
    per_source: int,
    *,
    group: bool = False,
    timeout_seconds: int = 180,
) -> tuple[list[dict[str, Any]], str]:
    """(entries, message text). Library songs come first, then wy, then kg; accompaniments are dropped.

    A library entry is {"kind": "library", "song_id", ...}; an online one is {"kind": "online", "source",
    "source_id", ...}. All carry name / artist / album / label. Empty entries give an empty text.
    """
    entries: list[dict[str, Any]] = []
    known: set[tuple[str, str]] = set()
    for song in library_hits:
        if is_instrumental(song.get("title"), song.get("album", "")):
            continue
        if song.get("source") and song.get("source_id"):
            known.add((str(song["source"]), str(song["source_id"])))
        entries.append({
            "kind": "library",
            "song_id": song["id"],
            "name": _clip(song.get("title"), NAME_LIMIT),
            "artist": _clip(song.get("artist"), ARTIST_LIMIT),
            "album": _clip(song.get("album"), ALBUM_LIMIT),
            "label": LIBRARY_LABEL,
        })
    for source in LIST_SOURCES:
        taken = 0
        for item in results_by_source.get(source) or []:
            if taken >= per_source:
                break
            if is_instrumental(item.get("name"), item.get("album", "")):
                continue
            if (source, str(item.get("id"))) in known:
                continue  # already listed as a library song
            taken += 1
            entries.append({
                "kind": "online",
                "source": source,
                "source_id": str(item["id"]),
                "name": _clip(item.get("name"), NAME_LIMIT),
                "artist": _clip(item.get("artist"), ARTIST_LIMIT),
                "album": _clip(item.get("album"), ALBUM_LIMIT),
                "label": SOURCE_LABELS[source],
            })
    if not entries:
        return [], ""
    minutes = max(1, round(timeout_seconds / 60))
    lines = [f"《{_clip(query, NAME_LIMIT)}》找到这些版本：", *(_line(n, item) for n, item in enumerate(entries, 1))]
    tail = f"回复序号选歌；回复“0 歌手或专辑”重新搜；回复“取消”结束（{minutes}分钟内有效）。"
    if group:
        tail += "群里请 @我 再回复哦。"
    lines.append(tail)
    return entries, "\n".join(lines)


# ---- replies to the list --------------------------------------------------------------------

_PICK_RE = re.compile(r"^\d{1,2}$")
_PICK_EXTRA_RE = re.compile(r"^(\d{1,2})\s+(\S.*)$", re.DOTALL)
_RESEARCH_RE = re.compile(r"^0[\s,:]+(\S.*)$", re.DOTALL)
_RESEARCH_EMPTY_RE = re.compile(r"^0[\s,:]*$")


def parse_pick(text: Any) -> tuple | None:
    """("pick", n, extra) / ("research", extra) / ("research_empty",) / ("cancel",), or None for anything else.

    `extra` is the text after the number ("2 男声"), "" for a bare number; whether it means anything is up to the
    caller (a pitch preset keyword), because "2 那首不错" must stay a normal chat message.
    """
    if not isinstance(text, str):
        return None
    value = unicodedata.normalize("NFKC", text).strip()
    if _PICK_RE.match(value):
        number = int(value)
        return ("pick", number, "") if number > 0 else ("research_empty",)
    match = _RESEARCH_RE.match(value)
    if match:
        return ("research", match.group(1).strip())
    if _RESEARCH_EMPTY_RE.match(value):
        return ("research_empty",)
    extra = _PICK_EXTRA_RE.match(value)
    if extra and int(extra.group(1)) > 0:
        return ("pick", int(extra.group(1)), extra.group(2).strip())
    if value == "取消":
        return ("cancel",)
    return None


# ---- pitch presets (keywords in the request) --------------------------------------------------

_LEFTOVER_EDGE = " \t\r\n,，、:：;；"


def match_pitch_preset(text: Any, presets: Any) -> tuple[dict[str, Any] | None, str]:
    """(preset, text without its keyword), or (None, text) when no keyword occurs.

    Presets are tried in order, keywords inside a preset in order; the first keyword found in the text (NFKC,
    case-insensitive) decides, and only its first occurrence is removed. Leftover blanks and separators
    around the gap are tidied up.
    """
    if not isinstance(text, str) or not isinstance(presets, list):
        return None, text if isinstance(text, str) else ""
    # Normalise character by character so a match can be cut out of the original text.
    folded: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        for piece in unicodedata.normalize("NFKC", char).casefold():
            folded.append(piece)
            origin.append(index)
    haystack = "".join(folded)
    for preset in presets:
        for keyword in preset.get("keywords", []):
            needle = unicodedata.normalize("NFKC", str(keyword)).casefold().strip()
            if not needle:
                continue
            start = haystack.find(needle)
            if start < 0:
                continue
            first, last = origin[start], origin[start + len(needle) - 1]
            cleaned = f"{text[:first]} {text[last + 1:]}"
            cleaned = re.sub(r"\s+", " ", cleaned).strip(_LEFTOVER_EDGE)
            return preset, cleaned
    return None, text
