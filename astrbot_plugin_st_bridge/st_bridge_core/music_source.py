"""Online song source (myhkw.cn open music API): search, resolve a download link, download.

No web-framework dependency. The API key travels as a query parameter, so nothing here ever puts a URL or
an exception text into a message without `redact` first; `MusicError` carries a short kind only.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp

API_BASE = "https://myhkw.cn/open/music"
SOURCES = ("wy", "qq", "kw", "kg", "mg", "qi")
SOURCE_LABELS = {"wy": "网易云", "qq": "QQ 音乐", "kw": "酷我", "kg": "酷狗", "mg": "咪咕", "qi": "千千"}
DEFAULT_SOURCES = ("wy", "kg", "qq")
API_TIMEOUT_SECONDS = 15.0
USER_AGENT = "Mozilla/5.0"
CHUNK_BYTES = 64 * 1024
SEARCH_LIMIT = 10

_KEY_PARAM_RE = re.compile(r"(?i)([?&]key=)[^&\s\"']*")
_BRACKETS_RE = re.compile(r"[(（\[【《<「『][^)）\]】》>」』]*[)）\]】》>」』]")
# Versions that are not the song itself; they sort behind plain matches.
_VARIANT_WORDS = ("原唱", "翻唱", "cover", "dj", "live")
# Accompaniment-like versions have no vocals to convert; they are excluded, not just sorted last.
_INSTRUMENTAL_CJK = ("伴奏", "纯音乐", "消音", "钢琴", "轻音乐", "八音盒")
_INSTRUMENTAL_RE = re.compile(
    r"(?<![a-z0-9])(?:instrumental|inst|karaoke|piano|off[\s\-]?vocal|no[\s\-]?vocal)(?![a-z0-9])",
    re.IGNORECASE,
)


class MusicError(Exception):
    """An online source failure; `kind` is a short name with no URL and no key."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def redact(text: Any, key: str = "") -> str:
    """`text` with the API key masked, both as a `key=` query parameter and as a bare value."""
    result = _KEY_PARAM_RE.sub(r"\1***", str(text))
    if key:
        result = result.replace(key, "***")
    return result


def normalize_text(value: Any) -> str:
    """casefold, drop bracketed parts, whitespace and punctuation (CJK and letters stay)."""
    text = str(value or "").casefold()
    stripped = _BRACKETS_RE.sub("", text)
    # A title that is only a bracket ("(live)") keeps its content instead of becoming empty.
    text = stripped if any(not unicodedata.category(c).startswith(("P", "S", "Z", "C")) for c in stripped) else text
    return "".join(c for c in text if not unicodedata.category(c).startswith(("P", "S", "Z", "C")))


def is_instrumental(name: Any, album: Any = "") -> bool:
    """True when the title or album says the version is an accompaniment / instrumental one."""
    for value in (name, album):
        text = str(value or "")
        if any(word in text for word in _INSTRUMENTAL_CJK) or _INSTRUMENTAL_RE.search(text):
            return True
    return False


def is_variant(name: str, query: str = "") -> bool:
    lowered, wanted = name.casefold(), query.casefold()
    return any(word in lowered and word not in wanted for word in _VARIANT_WORDS)


def pick_result(results: list[dict[str, Any]], name: str, artist: str | None) -> dict[str, Any] | None:
    """The best search result for a wanted title (and artist), or None when the source has no match."""
    wanted, wanted_artist = normalize_text(name), normalize_text(artist)
    usable = [item for item in results if not is_instrumental(item["name"], item.get("album", ""))]
    ordered = sorted(usable, key=lambda item: is_variant(item["name"], name))  # stable
    for item in ordered:
        if normalize_text(item["name"]) == wanted and (not wanted_artist or wanted_artist in normalize_text(item["artist"])):
            return item
    if wanted_artist:
        return None
    return ordered[0] if ordered else None


def parse_search(body: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or body.get("code") != 1 or not isinstance(body.get("data"), list):
        raise MusicError("api_failed")
    results = []
    for item in body["data"]:
        if not isinstance(item, dict):
            continue
        song_id, name = item.get("id"), item.get("name")
        if isinstance(song_id, bool) or not isinstance(song_id, (str, int)) or not str(song_id).strip():
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        artist = item.get("artist")
        if isinstance(artist, list):
            artist = "/".join(str(part) for part in artist)
        album = item.get("album")
        results.append({
            "source": source,
            "id": str(song_id).strip(),
            "name": " ".join(name.split()),
            "artist": " ".join(artist.split()) if isinstance(artist, str) else "",
            "album": " ".join(album.split()) if isinstance(album, str) else "",
        })
    return results


class MusicClient:
    def __init__(self, key_getter: Callable[[], str], *, base_url: str = API_BASE) -> None:
        self._key_getter = key_getter
        self._base = base_url.rstrip("/")

    def _key(self) -> str:
        key = self._key_getter()
        if not isinstance(key, str) or not key.strip():
            raise MusicError("music api key not set")
        return key.strip()

    async def _get_json(self, endpoint: str, params: dict[str, Any]) -> Any:
        key = self._key()
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
                async with session.get(f"{self._base}/{endpoint}", params={"key": key, **params}) as response:
                    if response.status != 200:
                        raise MusicError(f"HTTP{response.status}")
                    return await response.json(content_type=None)
        except asyncio.TimeoutError as error:
            raise MusicError("timeout") from error
        except (aiohttp.ClientError, ValueError, OSError) as error:
            # str(error) may carry the URL with the key; only the type name leaves this function.
            raise MusicError(f"network:{type(error).__name__}") from None

    async def search(self, name: str, source: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
        if source not in SOURCES:
            raise MusicError("invalid source")
        body = await self._get_json("search", {"name": name, "type": source, "page": 1, "limit": limit, "format": 1})
        return parse_search(body, source)

    async def get_url(self, song_id: Any, source: str) -> str:
        if source not in SOURCES:
            raise MusicError("invalid source")
        body = await self._get_json("url", {"id": str(song_id), "type": source})
        url = body.get("data") if isinstance(body, dict) and body.get("code") == 1 else None
        if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
            raise MusicError("no_link")
        return url

    async def download(self, url: str, *, max_bytes: int, timeout: float) -> bytes:
        """The whole file, aborting as soon as it is over `max_bytes`. Only http(s) links."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise MusicError("invalid_url")
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), headers={"User-Agent": USER_AGENT}
            ) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise MusicError(f"HTTP{response.status}")
                    if response.content_length is not None and response.content_length > max_bytes:
                        raise MusicError("too_large")
                    buffer = bytearray()
                    async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                        buffer.extend(chunk)
                        if len(buffer) > max_bytes:
                            raise MusicError("too_large")
        except asyncio.TimeoutError as error:
            raise MusicError("timeout") from error
        except (aiohttp.ClientError, OSError) as error:
            raise MusicError(f"network:{type(error).__name__}") from None
        if not buffer:
            raise MusicError("empty")
        return bytes(buffer)


# ---- NetEase song pages (chat links) -------------------------------------------------------

NETEASE_DETAIL_URL = "https://music.163.com/api/song/detail/"
LINK_TIMEOUT_SECONDS = 10.0
LINK_MAX_HOPS = 3
_LINK_HOST_SUFFIXES = ("music.163.com", "163cn.tv")


def is_netease_host(host: str) -> bool:
    """music.163.com, any subdomain of it, and the 163cn.tv short-link host."""
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _LINK_HOST_SUFFIXES)


def parse_netease_detail(body: Any) -> dict[str, str] | None:
    """{"name", "artist", "album"} from a song/detail response, or None when it has no song."""
    songs = body.get("songs") if isinstance(body, dict) else None
    song = songs[0] if isinstance(songs, list) and songs and isinstance(songs[0], dict) else None
    if song is None or not isinstance(song.get("name"), str) or not song["name"].strip():
        return None
    artists = song.get("artists") if isinstance(song.get("artists"), list) else song.get("ar")
    names = [a.get("name") for a in artists if isinstance(a, dict) and isinstance(a.get("name"), str)] if isinstance(artists, list) else []
    album = song.get("album") if isinstance(song.get("album"), dict) else song.get("al")
    album_name = album.get("name") if isinstance(album, dict) and isinstance(album.get("name"), str) else ""
    return {
        "name": " ".join(song["name"].split()),
        "artist": " ".join("/".join(names).split()),
        "album": " ".join(album_name.split()),
    }


async def fetch_netease_song(song_id: str, *, timeout: float = LINK_TIMEOUT_SECONDS) -> dict[str, str]:
    """Title, artist and album of a NetEase song id (no login needed). Raises MusicError."""
    if not str(song_id).isdigit():
        raise MusicError("invalid_id")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), headers={"User-Agent": USER_AGENT}
        ) as session:
            async with session.get(NETEASE_DETAIL_URL, params={"ids": f"[{song_id}]"}) as response:
                if response.status != 200:
                    raise MusicError(f"HTTP{response.status}")
                body = await response.json(content_type=None)
    except asyncio.TimeoutError as error:
        raise MusicError("timeout") from error
    except (aiohttp.ClientError, ValueError, OSError) as error:
        raise MusicError(f"network:{type(error).__name__}") from None
    detail = parse_netease_detail(body)
    if detail is None:
        raise MusicError("no_song")
    return detail


async def resolve_netease_short_link(
    url: str, *, timeout: float = LINK_TIMEOUT_SECONDS, max_hops: int = LINK_MAX_HOPS
) -> str:
    """Follow a 163cn.tv redirect chain by hand (never leaving NetEase hosts); the first non-short URL.

    Raises MusicError. The final page itself is not fetched.
    """
    current = url
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), headers={"User-Agent": USER_AGENT}
        ) as session:
            for _ in range(max_hops + 1):
                host = urlsplit(current).hostname or ""
                if urlsplit(current).scheme not in ("http", "https") or not is_netease_host(host):
                    raise MusicError("foreign_host")
                if host.lower() != "163cn.tv":
                    return current
                async with session.get(current, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status not in (301, 302, 303, 307, 308) or not location:
                        raise MusicError("no_redirect")
                    current = urljoin(current, location)
    except asyncio.TimeoutError as error:
        raise MusicError("timeout") from error
    except (aiohttp.ClientError, ValueError, OSError) as error:
        raise MusicError(f"network:{type(error).__name__}") from None
    raise MusicError("too_many_redirects")
