"""The AstrBot side of the bridge: events in, results out, and the platform abilities the core asks for.

Everything that knows what an AstrBot event, message chain or QQ Official client looks like lives
here; the core only sees IncomingMessage / Segment / ReplyBatch and opaque ids.
"""

import asyncio
import base64
import random
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from .astrbot_api import At, Image, MessageChain, MessageType, Plain, Record
from .st_bridge_core.platform_api import (
    GROUP,
    OTHER,
    PRIVATE,
    IncomingMessage,
    PlatformAPI,
    ReplyBatch,
    Segment,
)

# Events and uploads the core can still refer to; the oldest are forgotten first.
MAX_KNOWN_EVENTS = 256
MAX_KNOWN_UPLOADS = 64


def _components(segments: list[Segment]) -> list:
    chain = []
    for segment in segments:
        if segment.kind == "text":
            chain.append(Plain(segment.text))
        elif segment.kind == "voice":
            chain.append(Record(file=str(segment.path)))
        elif segment.kind == "image":
            chain.append(Image.fromFileSystem(str(segment.path)))
        else:
            raise ValueError(f"unknown segment kind {segment.kind!r}")
    return chain


def _safe(event, getter: str, default=None):
    """``event.<getter>()``, or ``default`` when the event has no such getter or it fails."""
    try:
        return getattr(event, getter)()
    except Exception:
        return default


class AstrBotPlatform(PlatformAPI):
    """PlatformAPI on top of AstrBot; ``get_context`` is read lazily (the plugin's ``context``)."""

    def __init__(self, get_context: Callable[[], object]) -> None:
        self._get_context = get_context
        self._events: OrderedDict[str, object] = OrderedDict()
        self._uploads: OrderedDict[str, tuple[str, object]] = OrderedDict()

    # ---- events <-> refs ---------------------------------------------------------------------
    def register(self, event) -> str:
        """Remember an accepted event so the core can reply to it later; returns its opaque ref."""
        ref = uuid.uuid4().hex
        self._events[ref] = event
        while len(self._events) > MAX_KNOWN_EVENTS:
            self._events.popitem(last=False)
        return ref

    def _event(self, ref: str):
        event = self._events.get(ref)
        if event is None:
            raise RuntimeError("message reference expired")
        return event

    # ---- event -> neutral message -------------------------------------------------------------
    def incoming(
        self, event, *, text: str | None = None, command: str | None = None, args: str = ""
    ) -> IncomingMessage:
        """The neutral view of an event; getters that fail leave empty values (and identity_ok False)."""
        failed = False

        def read(getter: str):
            nonlocal failed
            try:
                return getattr(event, getter)()
            except Exception:
                failed = True
                return ""

        platform_id = read("get_platform_id")
        sender_id = read("get_sender_id")
        sender_name = read("get_sender_name")
        message_type = _safe(event, "get_message_type")
        message_str = _safe(event, "get_message_str", "")
        group_id = _safe(event, "get_group_id")
        conversation_id = getattr(event, "unified_msg_origin", None)
        session_id = getattr(event, "session_id", "")
        return IncomingMessage(
            platform=_safe(event, "get_platform_name", "") or "",
            platform_id=platform_id,
            chat_type=(
                GROUP
                if message_type is MessageType.GROUP_MESSAGE
                else PRIVATE if message_type is MessageType.FRIEND_MESSAGE else OTHER
            ),
            session_id=session_id if isinstance(session_id, str) else "",
            conversation_id=conversation_id if isinstance(conversation_id, str) else "",
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=group_id if isinstance(group_id, str) and group_id else None,
            text=text,
            message_str=message_str if isinstance(message_str, str) else "",
            identity_ok=not failed,
            command=command,
            args=args,
            wake_prefixes=self.wake_prefixes(),
        )

    @staticmethod
    def private_text(event) -> str | None:
        """The plain text of a private message, or None when it is anything else."""
        try:
            components = event.get_messages()
            plain_components = None
            if isinstance(components, list) and components:
                if all(isinstance(component, Plain) for component in components):
                    plain_components = components
                elif (
                    len(components) >= 2
                    and isinstance(components[0], At)
                    and components[0].qq == "qq_official"
                    and event.get_platform_name() == "qq_official"
                    and event.get_message_type() is MessageType.FRIEND_MESSAGE
                    and not event.get_group_id()
                    and all(
                        isinstance(component, Plain)
                        for component in components[1:]
                    )
                ):
                    # QQ Official adds this fixed At placeholder to private text events.
                    plain_components = components[1:]

            return (
                "".join(component.text for component in plain_components)
                if plain_components is not None
                and all(
                    isinstance(component.text, str)
                    for component in plain_components
                )
                else None
            )
        except Exception:
            return None

    @staticmethod
    def is_qq_official_group_event(event) -> bool:
        try:
            if (
                event.get_platform_name() != "qq_official"
                or event.get_message_type() is not MessageType.GROUP_MESSAGE
            ):
                return False

            from botpy.message import GroupMessage

            raw_message = event.message_obj.raw_message
            return isinstance(raw_message, GroupMessage)
        except Exception:
            return False

    @staticmethod
    def is_own_qq_group_message(event, raw_message: object) -> bool:
        author = getattr(raw_message, "author", None)
        if getattr(author, "bot", None) is True:
            return True

        try:
            sender_id = event.get_sender_id()
            self_id = event.get_self_id()
            return bool(
                sender_id
                and self_id
                and self_id != "qq_official"
                and str(sender_id) == str(self_id)
            )
        except Exception:
            return False

    @staticmethod
    def group_message_text(event) -> str | None:
        try:
            components = event.get_messages()
            if not isinstance(components, list) or not components:
                return None

            if isinstance(components[0], At):
                if str(components[0].qq) != str(event.get_self_id()):
                    return None
                components = components[1:]

            if not components or not all(
                isinstance(component, Plain)
                and isinstance(component.text, str)
                for component in components
            ):
                return None

            content = event.get_message_str()
            return content if isinstance(content, str) else None
        except Exception:
            return None

    @staticmethod
    def parsed_command_params(event) -> dict:
        """AstrBot's successfully parsed command parameters; raises when they are unavailable."""
        parsed_params = event.get_extra("handlers_parsed_params", {})
        if not isinstance(parsed_params, dict):
            raise TypeError("parsed command metadata is unavailable")
        return parsed_params

    # ---- batch -> AstrBot result --------------------------------------------------------------
    @staticmethod
    def result(event, batch: ReplyBatch):
        segments = batch.segments
        if len(segments) == 1 and segments[0].kind == "text":
            return event.plain_result(segments[0].text)
        chain = _components(list(segments))
        for component in chain:
            if isinstance(component, Record):
                try:
                    if getattr(component, "text", None) is None:
                        component.text = ""
                except Exception:
                    pass
        return event.chain_result(chain)

    # ---- PlatformAPI --------------------------------------------------------------------------
    async def send(self, ref: str, segments: list[Segment]) -> None:
        await self._event(ref).send(MessageChain(_components(segments)))

    async def send_to(self, conversation_id: str, segments: list[Segment]) -> bool:
        sent = await self._get_context().send_message(conversation_id, MessageChain(_components(segments)))
        return sent is not False

    def wake_prefixes(self) -> list[str] | None:
        """AstrBot's command wake prefixes: the top-level `wake_prefix` (not provider_settings', which is for the LLM)."""
        try:
            prefixes = self._get_context().get_config()["wake_prefix"]
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            return [item for item in prefixes if isinstance(item, str)]
        except Exception:
            return None

    def extra_temp_dir(self) -> Path | None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            return Path(get_astrbot_temp_path())
        except Exception:
            return None

    def capabilities(self) -> dict:
        """What the hello tells the core about this platform (the core keeps it for its console)."""
        temp = self.extra_temp_dir()
        return {
            "wake_prefixes": self.wake_prefixes(),
            "extra_temp_dir": str(temp) if temp is not None else None,
            "image_preupload": True,
        }

    @staticmethod
    def _upload_target(event) -> tuple[str, str] | None:
        """The qq_official upload target of the event, or None when it cannot pre-upload."""
        try:
            if event.get_platform_name() != "qq_official":
                return None
            if not callable(getattr(event, "upload_group_and_c2c_image", None)):
                return None
            raw = event.message_obj.raw_message
            group = getattr(raw, "group_openid", None)
            if isinstance(group, str) and group:
                return "group_openid", group
            user = getattr(getattr(raw, "author", None), "user_openid", None)
            if isinstance(user, str) and user:
                return "openid", user
        except Exception:
            pass
        return None

    async def can_preupload_image(self, ref: str) -> bool:
        event = self._events.get(ref)
        return event is not None and self._upload_target(event) is not None

    async def preupload_image(self, ref: str, path: Path) -> str:
        event = self._event(ref)
        target = self._upload_target(event)
        if target is None:
            raise RuntimeError("pre-upload unavailable")
        data = await asyncio.to_thread(path.read_bytes)
        encoded = base64.b64encode(data).decode("ascii")
        file_type = getattr(event, "IMAGE_FILE_TYPE", 1)
        media = await event.upload_group_and_c2c_image(encoded, file_type, **{target[0]: target[1]})
        handle = uuid.uuid4().hex
        self._uploads[handle] = (target, media)
        while len(self._uploads) > MAX_KNOWN_UPLOADS:
            self._uploads.popitem(last=False)
        return handle

    async def send_uploaded_image(self, ref: str, handle: str) -> None:
        """Send an uploaded image as its own passive reply (msg_id + msg_seq)."""
        event = self._event(ref)
        upload = self._uploads.pop(handle, None)
        if upload is None:
            raise RuntimeError("upload handle expired")
        (kind, openid), media = upload
        fields = {
            "msg_type": 7,
            "media": media,
            "msg_id": getattr(event.message_obj, "message_id", None),
            "msg_seq": random.randint(1, 10000),
        }
        if kind == "group_openid":
            await event.bot.api.post_group_message(group_openid=openid, **fields)
            return
        result = await event.post_c2c_message(openid=openid, **fields)
        if result is None:
            raise RuntimeError("c2c sticker send returned nothing")
