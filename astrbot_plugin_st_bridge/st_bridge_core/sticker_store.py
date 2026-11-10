"""Per-emotion sticker images stored under <data_dir>/stickers/<emotion>/.

No web-framework dependency. The image type is decided by the file header, never by the
uploaded name or content type, and every path is rebuilt from a validated emotion name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .emotion_config import (
    EmotionConfigError,
    safe_audio_stem,
    safe_emotion,
    unique_audio_path,
)


STICKER_DIR_NAME = "stickers"
MAX_STICKER_BYTES = 10 * 1024 * 1024

# Fixed map on purpose: mimetypes reads the Windows registry and can disagree (see M1b.1).
STICKER_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def detect_image_suffix(content: bytes) -> str | None:
    """The canonical suffix for a PNG/JPEG/GIF header, else None."""
    if content.startswith(_PNG_MAGIC):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def sticker_content_type(path: Path) -> str:
    return STICKER_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


class StickerStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir).resolve() / STICKER_DIR_NAME

    def emotion_dir(self, emotion: str) -> Path:
        return self.root / safe_emotion(emotion)

    def list_files(self, emotion: str) -> list[Path]:
        """Usable images of one emotion, sorted by name; an unknown emotion has none."""
        try:
            directory = self.emotion_dir(emotion)
            if not directory.is_dir():
                return []
            return sorted(
                (
                    entry
                    for entry in directory.iterdir()
                    if entry.suffix.lower() in STICKER_CONTENT_TYPES and entry.is_file()
                ),
                key=lambda entry: entry.name,
            )
        except (EmotionConfigError, OSError):
            return []

    def add(self, emotion: str, filename: Any, content: bytes) -> Path:
        directory = self.emotion_dir(emotion)
        if not content:
            raise EmotionConfigError(400, "empty image")
        if len(content) > MAX_STICKER_BYTES:
            raise EmotionConfigError(413, "image too large")
        suffix = detect_image_suffix(content)
        if suffix is None:
            raise EmotionConfigError(400, "unsupported image format")
        original = Path(str(filename or "").replace("\\", "/")).name
        stem = safe_audio_stem(Path(original).stem or "sticker")
        directory.mkdir(parents=True, exist_ok=True)
        destination = unique_audio_path(directory, stem, suffix)
        destination.write_bytes(content)
        return destination

    def resolve(self, emotion: str, name: Any) -> Path:
        directory = self.emotion_dir(emotion).resolve()
        if not isinstance(name, str) or not name or Path(name.replace("\\", "/")).name != name:
            raise EmotionConfigError(400, "invalid image name")
        candidate = (directory / name).resolve()
        if candidate.parent != directory:
            raise EmotionConfigError(400, "invalid image name")
        if candidate.suffix.lower() not in STICKER_CONTENT_TYPES or not candidate.is_file():
            raise EmotionConfigError(404, "image not found")
        return candidate

    def groups(self, emotions: list[str]) -> list[dict[str, Any]]:
        """Group summaries for the given emotions plus any other folder that holds images."""
        names = list(dict.fromkeys(emotions))
        try:
            extra = sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and entry.name not in names
            ) if self.root.is_dir() else []
        except OSError:
            extra = []
        result = []
        for name in names + extra:
            if not _valid_emotion(name):
                continue
            files = self.list_files(name)
            if name in extra and not files:
                continue
            result.append({
                "name": name,
                "count": len(files),
                "images": [{"name": file.name, "size": _size(file)} for file in files],
            })
        return result


def _valid_emotion(name: str) -> bool:
    try:
        safe_emotion(name)
    except EmotionConfigError:
        return False
    return True


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
