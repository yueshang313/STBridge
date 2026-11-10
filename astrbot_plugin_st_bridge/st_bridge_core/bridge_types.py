"""Shared constants, data classes and pure reply helpers of the ST bridge."""

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .affinity_store import AffinityUpdate, user_key
from .emotion_config import EMOTION_TAG_RE, OWNER_TRACK
from .song_request import DEFAULT_PERMISSION_VARIABLE


REQUEST_TIMEOUT_SECONDS = 120.0
_JAPANESE_REPLY_PATTERN = re.compile(r"☆‹(.*?)›", re.DOTALL)
_CHINESE_REPLY_PATTERN = re.compile(r'"([^"]*)"', re.DOTALL)
_GAME_OPEN_PATTERN = re.compile(r"<game\s*>", re.IGNORECASE)
_GAME_CLOSE_PATTERN = re.compile(r"</game\s*>", re.IGNORECASE)
# Default of the stat_data variable the ST world book reads to know whether the character may take a song
# request; the live name comes from the song_request settings.
SONG_PERMISSION_PATH = [DEFAULT_PERMISSION_VARIABLE]


class _ContentTooLongError(Exception):
    """A queued entry's final ST text no longer fits the request size limit."""


@dataclass(frozen=True)
class _Speaker:
    """Who sent a bridged message and which affinity track they belong to.

    ``value`` (their own value) and ``writes`` (the stat_data values for this turn)
    are None when the affinity store is unavailable.
    """

    key: str
    nickname: str
    track: str = OWNER_TRACK
    value: int | None = None
    writes: list | None = None
    # This turn's song-request permission (the same result ST was told); None when not decided.
    sing_allowed: bool | None = None
    # Why it was refused: "quota" (today's limit is used up) or "other"; None when allowed or undecided.
    sing_reason: str | None = None


@dataclass(frozen=True)
class _Origin:
    """Sender identity kept while queued; affinity is looked up again at dispatch."""

    platform_id: object
    sender_id: object
    nickname: object
    # "昵称（n）" in group chat; None for private messages, which get no prefix.
    group_label: str | None = None
    # Shown as "@name " on group replies that had to wait.
    mention_name: str = ""

    @property
    def key(self) -> str | None:
        if not isinstance(self.sender_id, str) or not self.sender_id.strip():
            return None
        return user_key(self.platform_id, self.sender_id)


@dataclass
class _PendingSongList:
    """A numbered song list waiting for its requester's pick."""

    query: str
    entries: list
    expires: float
    group: bool
    via: str
    preset: dict | None = None  # the pitch preset the request already named (kept across re-searches)


@dataclass
class _GroupSpeaker:
    order: int
    display_name: str = ""
    name_slots: dict[str, int] = field(default_factory=dict)
    fallback_label: str | None = None


def _console_error_type(error: BaseException) -> str:
    if isinstance(error, OSError):
        return "OSError"
    if isinstance(error, RuntimeError):
        return "RuntimeError"
    return "OtherError"


def _config_string(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    return value.strip() if isinstance(value, str) else ""


# Places where a <game> is only being talked about, never the body: a paired <think…>…</think…> (an unpaired one is
# not a region, so it cannot swallow the body), and <comment> / <details>, which sit after the body and may be cut
# off before they close (then they run to the end of the reply).
_THINK_REGION = re.compile(r"<think[\w:-]*(?:\s[^>]*)?>.*?</think[\w:-]*\s*>", re.IGNORECASE | re.DOTALL)
_NOTE_REGION = re.compile(r"<(comment|details)(?:\s[^>]*)?>.*?(?:</\1\s*>|\Z)", re.IGNORECASE | re.DOTALL)


def _ignored_regions(reply: str) -> list[tuple[int, int]]:
    return [match.span() for pattern in (_THINK_REGION, _NOTE_REGION) for match in pattern.finditer(reply)]


def _select_game(reply: str) -> tuple[re.Match, re.Match | None] | None:
    """The <game> that holds the body, with its </game> (None when the output was cut off before it).

    Candidates are the <game> tags outside the ignored regions. The last candidate with a </game> after it (also
    outside the regions) wins; with no complete block, the last candidate runs to the end of the reply.
    """
    regions = _ignored_regions(reply)

    def outside(match: re.Match) -> bool:
        return not any(start <= match.start() < end for start, end in regions)

    candidates = [match for match in _GAME_OPEN_PATTERN.finditer(reply) if outside(match)]
    if not candidates:
        return None
    closers = [match for match in _GAME_CLOSE_PATTERN.finditer(reply) if outside(match)]
    for opening in reversed(candidates):
        for closing in closers:
            if closing.start() >= opening.end():
                return opening, closing
    return candidates[-1], None


def _last_game_open(reply: str) -> re.Match | None:
    """The opening tag of the block split_game_block uses (for logging how much came before it)."""
    selected = _select_game(reply)
    return selected[0] if selected is not None else None


def split_game_block(reply: object) -> tuple[str, str] | None:
    """Split a reply into the <game> body and the text after its </game>.

    Everything before the chosen <game> (the model's reasoning) is dropped. The block is the last complete
    <game>…</game> outside <think…>, <comment> and <details> (see _select_game); a missing </game> (truncated
    output) makes the body run to the end. Returns None when there is no usable <game> tag, so older presets keep
    their behavior.
    """
    if not isinstance(reply, str):
        return None
    selected = _select_game(reply)
    if selected is None:
        return None
    opening, closing = selected
    if closing is None:
        return reply[opening.end():], ""
    return reply[opening.end():closing.start()], reply[closing.end():]


def extract_bilingual_reply(reply: object) -> tuple[str, str] | None:
    if not isinstance(reply, str):
        return None

    if (
        reply.count("☆‹") != 1
        or reply.count("›") != 1
        or reply.count('"') != 2
    ):
        return None

    japanese_matches = list(_JAPANESE_REPLY_PATTERN.finditer(reply))
    chinese_matches = list(_CHINESE_REPLY_PATTERN.finditer(reply))
    if len(japanese_matches) != 1 or len(chinese_matches) != 1:
        return None

    japanese_text = japanese_matches[0].group(1).strip()
    chinese_text = chinese_matches[0].group(1).strip()
    if not japanese_text or not chinese_text:
        return None

    return japanese_text, chinese_text


# A pair, as (Japanese, Chinese, the group's own raw emotion name or None).
ReplyPair = tuple[str, str, "str | None"]
_SENTENCE_END = "。！？!?…～ー」』）)．.~"


def extract_reply_pairs(text: object) -> tuple[list[ReplyPair], list[str]] | None:
    """Every ☆‹日文› + "中文" group of a reply, with each group's emotion; None when invalid.

    ``text`` still holds its [情绪:x] tags (their positions decide each group's emotion).
    Returns (pairs, all tag names in order). A group's emotion is the last tag between the end of
    the previous group and its own ☆‹; without one it keeps the previous group's, and the first
    group falls back to the first tag of the whole reply (so a single group behaves as before).
    """
    if not isinstance(text, str):
        return None
    # Tags come out first (quote/bracket counts are taken without them); remember where each sat.
    tags: list[tuple[int, str]] = []
    pieces: list[str] = []
    cursor = 0
    removed = 0
    for match in EMOTION_TAG_RE.finditer(text):
        pieces.append(text[cursor:match.start()])
        removed += match.start() - cursor
        tags.append((removed, match.group(1).strip()))
        cursor = match.end()
    pieces.append(text[cursor:])
    cleaned = "".join(pieces)
    names = [name for _, name in tags]

    groups = cleaned.count("☆‹")
    if groups < 1 or cleaned.count("›") != groups or cleaned.count('"') != 2 * groups:
        return None
    japanese = list(_JAPANESE_REPLY_PATTERN.finditer(cleaned))
    chinese = list(_CHINESE_REPLY_PATTERN.finditer(cleaned))
    if len(japanese) != groups or len(chinese) != groups:
        return None
    if groups > 1:
        for index in range(groups):
            if chinese[index].start() < japanese[index].end():
                return None
            if index + 1 < groups and chinese[index].end() > japanese[index + 1].start():
                return None

    pairs: list[ReplyPair] = []
    boundary = 0
    previous: str | None = names[0] if names else None
    for index in range(groups):
        japanese_text = japanese[index].group(1).strip()
        chinese_text = chinese[index].group(1).strip()
        if not japanese_text or not chinese_text:
            return None
        own = [name for position, name in tags if boundary <= position <= japanese[index].start()]
        emotion = own[-1] if own else previous
        pairs.append((japanese_text, chinese_text, emotion))
        previous = emotion
        boundary = chinese[index].end()
    return pairs, names


def escape_st_macros(text: str) -> str:
    """Make the text inert for SillyTavern's macro engine: "{{" becomes "｛｛" and "}}" becomes "｝｝".

    Everything a QQ user types goes through this before it reaches ST (ST substitutes macros in the user message
    it is given). Single braces stay. A run like "{{{x}}}" comes out as "｛｛{x｝｝}": no "{{" or "}}" is left.
    """
    if not isinstance(text, str):
        return text
    return text.replace("{{", "｛｛").replace("}}", "｝｝")


_ST_TAG_PATTERN = re.compile(r"<(?=/?(?:game|UpdateVariable))", re.IGNORECASE)


def escape_st_tags(text: str) -> str:
    """Make the text unable to pass as the model's own body or variable block: the "<" of <game, </game,
    <UpdateVariable and </UpdateVariable (any case) becomes the full-width "＜". The tag name stays; other angle
    brackets stay too.
    """
    if not isinstance(text, str):
        return text
    return _ST_TAG_PATTERN.sub("＜", text)


def merge_pairs(pairs: list[ReplyPair]) -> ReplyPair:
    """One group out of several: Chinese joined by newlines, Japanese joined (a missing sentence end gets 。)."""
    japanese = ""
    for piece, _, _ in pairs:
        if japanese and japanese[-1] not in _SENTENCE_END:
            japanese += "。"
        japanese += piece
    return japanese, "\n".join(chinese for _, chinese, _ in pairs), pairs[0][2]


def cap_pairs(pairs: list[ReplyPair], limit: int) -> list[ReplyPair]:
    """At most ``limit`` groups: the ones from the limit-th on are merged into the last."""
    limit = max(1, limit)
    if len(pairs) <= limit:
        return list(pairs)
    return [*pairs[:limit - 1], merge_pairs(pairs[limit - 1:])]


@dataclass
class _StickerPlan:
    """A sticker decided before the reply goes out, sent after it."""

    emotion: str
    path: Path
    candidates: int
    # True when the platform pre-uploads the image (see PlatformAPI.can_preupload_image).
    uploadable: bool = False
    task: asyncio.Task | None = None
    # time.monotonic() when the send was decided; resume_ms in the log starts here.
    decided_at: float = 0.0


def _retrieve_task_exception(task: asyncio.Task) -> None:
    """Keep an unawaited pre-upload failure from becoming "exception was never retrieved"."""
    if not task.cancelled():
        task.exception()


@dataclass
class _ParsedReply:
    """An ST reply after game-block, MVU, emotion-tag and bilingual processing."""

    japanese_text: str
    chinese_text: str
    emotion_tags: list[str]
    affinity_update: AffinityUpdate | None
    # The song named by a [唱歌:歌名] tag (already removed from both texts); only honoured when allowed.
    sing_name: str | None = None
    # Every group as (Japanese, Chinese, its emotion); the text fields above hold them merged.
    pairs: list = field(default_factory=list)
