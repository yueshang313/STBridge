"""Platform-neutral types of the bridge core and the interface a platform adapter implements.

Everything crossing this boundary is plain, serializable data plus opaque string ids, so the
adapter can later live in another process. The core never sees a platform object (message
event, message chain, bot client); the adapter keeps those and finds them again by ``ref``.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


OTHER = "other"
PRIVATE = "private"
GROUP = "group"
# Conversation ids / refs of the console test chat (A1) start with this; no platform ever delivers to them.
TEST_CONVERSATION_PREFIX = "stbridge-test:"


@dataclass
class ChatProbe:
    """Marks a message as a console test chat (A1) and collects what the core made of the ST reply.

    Only the console's test endpoint creates one; it is not part of the connector protocol and the OneBot
    inbound never sets it. A message carrying a probe goes through the normal flow (queue, escaping, prefixes,
    reply parsing, split settings) but has no side effects: no voice, sticker, song flow, affinity write, mood
    record, schedule target or platform send.
    """

    # The full ST reply as the core received it ("" when none arrived).
    raw: str = ""
    # The failure code of the turn (None: the reply was delivered normally).
    result: str | None = None
    emotion_tags: list = field(default_factory=list)
    # {"operations": [[op, n], ...], "delta": int | None}; parsed, never applied.
    affinity_change: dict | None = None
    sing: str | None = None

    def fail(self, code: str) -> None:
        if self.result is None:
            self.result = code


@dataclass(frozen=True)
class Segment:
    """One part of an outgoing message: text, a voice file or an image file (local paths)."""

    kind: str  # "text" | "voice" | "image"
    text: str = ""
    path: Path | None = None

    @classmethod
    def of_text(cls, text: str) -> "Segment":
        return cls("text", text=text)

    @classmethod
    def voice(cls, path: Path) -> "Segment":
        return cls("voice", path=path)

    @classmethod
    def image(cls, path: Path) -> "Segment":
        return cls("image", path=path)


@dataclass(frozen=True)
class ReplyBatch:
    """What the core hands back for one step of a reply; the adapter sends it as one message."""

    segments: tuple[Segment, ...]

    @classmethod
    def of_text(cls, text: str) -> "ReplyBatch":
        return cls((Segment.of_text(text),))


@dataclass(frozen=True)
class IncomingMessage:
    """A message (or command) as the core sees it."""

    platform: str  # e.g. "qq_official"
    platform_id: str  # the configured platform instance
    chat_type: str  # PRIVATE | GROUP
    # Per-chat key used for the voice and sticker cooldowns (AstrBot: event.session_id).
    session_id: str
    # Opaque key of the conversation: song picks wait under it and scheduled messages are sent
    # to it (AstrBot: event.unified_msg_origin).
    conversation_id: str
    sender_id: str
    sender_name: str
    group_id: str | None
    # The plain text to bridge; None when the message is not plain text.
    text: str | None
    # The text as the platform presents it (after its own prefix handling); "" when unavailable.
    message_str: str
    # Opaque id the adapter uses to find the original message again (passive replies, uploads).
    ref: str = ""
    # False when the platform could not tell who sent it.
    identity_ok: bool = True
    # Set for commands: the command name ("ping", "affinity", "affinity_set", "recover", "sing") and its argument.
    command: str | None = None
    args: str = ""
    # The platform's command wake prefixes when the message was made (None: unknown).
    wake_prefixes: list[str] | None = None
    # Set only by the console test endpoint (see ChatProbe); never serialized, never equal-compared.
    probe: Any = field(default=None, compare=False, repr=False)


class PlatformAPI:
    """What the core asks of the platform. Optional abilities have a harmless default."""

    async def send(self, ref: str, segments: list[Segment]) -> None:
        """Send one more message in the conversation the message ``ref`` came from."""
        raise NotImplementedError

    async def send_to(self, conversation_id: str, segments: list[Segment]) -> bool:
        """Send a proactive message; False when the platform cannot deliver it."""
        raise NotImplementedError

    def wake_prefixes(self) -> list[str] | None:
        """The platform's command wake prefixes, or None when unknown."""
        return None

    def extra_temp_dir(self) -> Path | None:
        """A temp directory of the host application that the storage cleanup may also tidy."""
        return None

    async def can_preupload_image(self, ref: str) -> bool:
        """Can an image be uploaded ahead of time for a reply to ``ref``?"""
        return False

    async def preupload_image(self, ref: str, path: Path) -> str:
        """Upload an image file without sending it; returns an opaque handle."""
        raise NotImplementedError

    async def send_uploaded_image(self, ref: str, handle: str) -> None:
        """Send a pre-uploaded image as a reply to ``ref``."""
        raise NotImplementedError
