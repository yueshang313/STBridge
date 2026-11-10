"""Song library (`<data_dir>/songs/`) and cover cache (`<data_dir>/covers/`) for AI song covers.

No web-framework dependency. File types are decided by the file header, never by the uploaded name.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .emotion_config import EmotionConfigError, safe_audio_stem, unique_audio_path
from .schedule_service import _read_json, _write_json

SONG_MAX_BYTES = 50 * 1024 * 1024
MAX_TITLE_CHARS = 80
MAX_SONGS = 500

# Fixed map on purpose: mimetypes reads the Windows registry and can disagree (see M1b.1).
AUDIO_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}


def detect_song_suffix(head: bytes) -> str | None:
    """The canonical suffix of an mp3 / flac / wav / m4a / ogg header, else None."""
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return ".mp3"
    return None


def audio_content_type(path: Path) -> str:
    return AUDIO_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, "invalid title")
    title = " ".join(value.split())
    if not title or len(title) > MAX_TITLE_CHARS:
        raise EmotionConfigError(400, "invalid title")
    return title


class SongLibrary:
    def __init__(self, data_dir: Path) -> None:
        self.directory = Path(data_dir) / "songs"
        self._index = self.directory / "songs.json"
        songs = _read_json(self._index).get("songs")
        self._songs: list[dict[str, Any]] = [
            song for song in (songs if isinstance(songs, list) else [])
            if isinstance(song, dict) and isinstance(song.get("id"), str) and isinstance(song.get("file"), str)
        ]

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_json(self._index, {"songs": self._songs})

    def list(self) -> list[dict[str, Any]]:
        return [dict(song) for song in self._songs]

    def get(self, song_id: Any) -> dict[str, Any] | None:
        return next((dict(song) for song in self._songs if song["id"] == song_id), None)

    def path(self, song: dict[str, Any]) -> Path:
        # The stored name always comes from add(); resolve defends against a hand-edited index.
        candidate = (self.directory / song["file"]).resolve()
        if candidate.parent != self.directory.resolve():
            raise EmotionConfigError(400, "invalid song file")
        return candidate

    def find_by_sha(self, digest: str) -> dict[str, Any] | None:
        return next((dict(song) for song in self._songs if song.get("sha256") == digest), None)

    def find_by_source(self, source: str, source_id: str) -> dict[str, Any] | None:
        """The song downloaded from this online source id, if any."""
        return next(
            (
                dict(song) for song in self._songs
                if song.get("source") == source and str(song.get("source_id") or "") == str(source_id)
            ),
            None,
        )

    def add(self, filename: Any, content: bytes, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store one song; the caller fills in the duration afterwards.

        `meta` (online songs): title, artist, source, source_id.
        """
        if len(self._songs) >= MAX_SONGS:
            raise EmotionConfigError(400, "too many songs")
        if not content:
            raise EmotionConfigError(400, "empty file")
        if len(content) > SONG_MAX_BYTES:
            raise EmotionConfigError(413, "file too large")
        suffix = detect_song_suffix(content[:16])
        if suffix is None:
            raise EmotionConfigError(400, "unsupported audio format")
        digest = hashlib.sha256(content).hexdigest()
        if any(song.get("sha256") == digest for song in self._songs):
            raise EmotionConfigError(409, "song already in the library")
        original = Path(str(filename or "").replace("\\", "/")).name
        stem = safe_audio_stem(Path(original).stem or "song")
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = unique_audio_path(self.directory, stem, suffix)
        destination.write_bytes(content)
        meta = meta or {}
        title = " ".join(str(meta.get("title") or Path(original).stem).split())[:MAX_TITLE_CHARS] or stem
        song = {
            "id": secrets.token_hex(4),
            "title": title,
            "filename": original[:120],
            "file": destination.name,
            "ext": suffix,
            "size": len(content),
            "sha256": digest,
            "duration": 0.0,
            "clip_start": None,
            "added": _now(),
        }
        for field in ("artist", "source", "source_id"):
            if isinstance(meta.get(field), str) and meta[field].strip():
                song[field] = " ".join(meta[field].split())[:MAX_TITLE_CHARS]
        self._songs.append(song)
        try:
            self._save()
        except OSError:
            self._songs.remove(song)
            destination.unlink(missing_ok=True)
            raise
        return dict(song)

    def set_duration(self, song_id: str, duration: float) -> None:
        for song in self._songs:
            if song["id"] == song_id:
                song["duration"] = round(float(duration), 2)
        self._save()

    def update(self, song_id: str, *, title: Any = None, clip_start: Any = ..., ) -> dict[str, Any]:
        """Change the title and/or the start second (None clears it)."""
        song = next((item for item in self._songs if item["id"] == song_id), None)
        if song is None:
            raise EmotionConfigError(404, "song not found")
        if title is not None:
            song["title"] = _clean_title(title)
        if clip_start is not ...:
            if clip_start is None:
                song["clip_start"] = None
            else:
                if isinstance(clip_start, bool) or not isinstance(clip_start, (int, float)):
                    raise EmotionConfigError(400, "invalid clip start")
                limit = float(song.get("duration") or 0) or 86400.0
                if not 0 <= clip_start <= limit:
                    raise EmotionConfigError(400, "invalid clip start")
                song["clip_start"] = round(float(clip_start), 2)
        self._save()
        return dict(song)

    def delete(self, song_id: str) -> None:
        song = next((item for item in self._songs if item["id"] == song_id), None)
        if song is None:
            raise EmotionConfigError(404, "song not found")
        path = self.path(song)
        self._songs.remove(song)
        self._save()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def remove_quietly(self, song_id: str) -> None:
        """Drop a half-added song (failed duration probe)."""
        try:
            self.delete(song_id)
        except EmotionConfigError:
            pass


def cache_key(song_sha256: str, params: dict[str, Any]) -> str:
    """Hash of the song file and every parameter that changes the audio: a changed parameter misses."""
    payload = json.dumps({"song": song_sha256, "params": params}, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class CoverCache:
    def __init__(self, data_dir: Path) -> None:
        self.directory = Path(data_dir) / "covers"
        self._index = self.directory / "covers.json"
        covers = _read_json(self._index).get("covers")
        self._covers: dict[str, dict[str, Any]] = {
            key: value for key, value in (covers.items() if isinstance(covers, dict) else [])
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_json(self._index, {"covers": self._covers})

    def path_for(self, key: str) -> Path:
        if not key.isalnum() or len(key) > 32:
            raise EmotionConfigError(400, "invalid cover key")
        return self.directory / f"{key}.wav"

    def partial_path(self, key: str) -> Path:
        return self.directory / f"{key}.part.wav"

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._covers.get(key)
        try:
            if entry is not None and self.path_for(key).is_file():
                return dict(entry, key=key)
        except EmotionConfigError:
            pass
        return None

    def add(self, key: str, *, song_id: str, title: str, summary: str, duration: float, timings: dict[str, Any]) -> dict[str, Any]:
        path = self.path_for(key)
        entry = {
            "song_id": song_id,
            "title": title,
            "summary": summary,
            "created": _now(),
            "size": path.stat().st_size if path.is_file() else 0,
            "duration": round(float(duration), 2),
            "timings": {name: value for name, value in timings.items() if name.endswith("_ms")},
        }
        self._covers[key] = entry
        self._save()
        return dict(entry, key=key)

    def usage(self) -> tuple[int, int]:
        """(entries, bytes) of the finished covers."""
        items = self.list()
        return len(items), sum(int(item.get("size") or 0) for item in items)

    def clear(self) -> tuple[int, int]:
        """Delete every finished cover and empty the index; generating (`.part.wav`) files stay. (files, bytes)"""
        removed = freed = 0
        try:
            candidates = [p for p in self.directory.iterdir() if p.is_file() and p.suffix == ".wav" and not p.name.endswith(".part.wav")]
        except OSError:
            candidates = []
        for path in candidates:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
        self._covers = {}
        self._save()
        return removed, freed

    def list(self) -> list[dict[str, Any]]:
        items = [dict(entry, key=key) for key, entry in self._covers.items() if self.get(key) is not None]
        return sorted(items, key=lambda item: item.get("created", ""), reverse=True)

    def delete(self, key: str) -> None:
        if key not in self._covers:
            raise EmotionConfigError(404, "cover not found")
        path = self.path_for(key)
        del self._covers[key]
        self._save()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
