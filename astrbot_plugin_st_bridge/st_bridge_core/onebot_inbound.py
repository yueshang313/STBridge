"""NapCat events to bridge calls: who may talk, what wakes the character, which command it is.

Only one chat platform works at a time (``chat_platform.active``); while AstrBot is active every NapCat message is
ignored. Private chats are the owner's only (everyone else is ignored without an answer); groups must be on the whitelist.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any

from .chat_platform import NAPCAT, NAPCAT_PLATFORM_ID, group_conversation, private_conversation
from .core import BridgeCore
from .log import logger
from .onebot_link import OneBotLink, _Connection
from .platform_api import GROUP, PRIVATE, IncomingMessage, ReplyBatch

PLATFORM_NAME = "onebot"
PING_TEXT = "ST Bridge 核心运行中（NapCat 模式）"
# The five commands AstrBot's @filter.command handles for the plugin: first word -> core command.
COMMANDS = {
    "stbridge_ping": "ping",
    "好感度": "affinity",
    "好感度设置": "affinity_set",
    "stbridge_recover": "recover",
    "唱": "sing",
}


def flatten_message(segments: list[Any], self_id: str) -> tuple[str, bool]:
    """(plain text, whether the bot itself was @-mentioned): text segments in order, @ segments left out.

    Images, voices, faces, replies and every other segment are ignored.
    """
    parts: list[str] = []
    at_self = False
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        kind = segment.get("type")
        if kind == "text":
            text = data.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "at" and str(data.get("qq")) == self_id:
            at_self = True
    return "".join(parts).strip(), at_self


def strip_wake_prefix(text: str, prefixes: list[str]) -> tuple[str, bool]:
    """The text without a leading wake prefix (longest first) and whether one was there."""
    text = text.lstrip()
    for prefix in sorted({item for item in prefixes if item}, key=len, reverse=True):
        if text.startswith(prefix):
            return text[len(prefix):].strip(), True
    return text.strip(), False


def parse_command(text: str, prefixes: list[str]) -> tuple[str, str] | None:
    """(command, argument) when the text, without a wake prefix, starts with one of the five command words."""
    rest, _ = strip_wake_prefix(text, prefixes)
    found = re.match(r"(\S+)\s*(.*)", rest, re.DOTALL)
    if found is None:
        return None
    command = COMMANDS.get(found.group(1))
    if command is None:
        return None
    args = found.group(2).strip()
    # Like AstrBot's command parameter: only the first word is the value.
    return command, (args.split()[0] if command == "affinity_set" and args else args)


class OneBotInbound:
    """Handles the events of the NapCat connection."""

    def __init__(self, core: BridgeCore, link: OneBotLink) -> None:
        self._core = core
        self._link = link
        self._format_logged = False
        link.on_event = self.on_event

    async def on_event(self, event: dict[str, Any], conn: _Connection) -> None:
        if event.get("post_type") != "message":
            return  # notices, requests (friend / group invitations are never handled) and sent-message echoes
        core = self._core
        if core.active_platform() != NAPCAT:
            return
        segments = event.get("message")
        if isinstance(segments, str):
            self._link.format_warning = True
            if not self._format_logged:
                self._format_logged = True
                logger.warning("[STBridge] NapCat message format is not array; set it to array in NapCat")
            return
        if not isinstance(segments, list):
            return
        self_id = conn.self_id or (str(event.get("self_id")) if event.get("self_id") is not None else "")
        user_id = str(event.get("user_id")) if event.get("user_id") is not None else ""
        if not user_id.isdigit() or user_id == self_id:
            return
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        name = sender.get("card") or sender.get("nickname") or ""
        name = name if isinstance(name, str) else ""
        text, at_self = flatten_message(segments, self_id)
        kind = event.get("message_type")
        if kind == "private":
            await self._private(user_id, name, text)
        elif kind == "group" and event.get("group_id") is not None:
            await self._group(str(event["group_id"]), user_id, name, text, at_self)

    # ---- the two chat types ------------------------------------------------------------------
    def _message(
        self, chat_type: str, session: str, conversation: str, user_id: str, name: str, group: str | None,
        text: str | None, message_str: str, prefixes: list[str],
    ) -> IncomingMessage:
        return IncomingMessage(
            platform=PLATFORM_NAME,
            platform_id=NAPCAT_PLATFORM_ID,
            chat_type=chat_type,
            session_id=session,
            conversation_id=conversation,
            sender_id=user_id,
            sender_name=name,
            group_id=group,
            text=text,
            message_str=message_str,
            ref=conversation,
            wake_prefixes=list(prefixes),
        )

    async def _private(self, user_id: str, name: str, text: str) -> None:
        core = self._core
        prefixes = self._link.wake_prefixes() or []
        # Plain chat only loses a "/nickname" (AstrBot's "/" wake word); a bare nickname is part of what was said.
        rest, _ = strip_wake_prefix(text, [item for item in prefixes if item.startswith("/")])
        conversation = private_conversation(user_id)
        msg = self._message(PRIVATE, user_id, conversation, user_id, name, None, rest or None, rest, prefixes)
        if not core.accept_private(msg):
            return  # not the owner: no answer at all
        command = parse_command(text, prefixes)
        if command is not None:
            await self._command(msg, command, text, prefixes)
        elif rest:
            await self._deliver(conversation, core.handle_private(msg))

    async def _group(self, group: str, user_id: str, name: str, text: str, at_self: bool) -> None:
        core = self._core
        if group not in core.chat_platform_settings()["group_whitelist"]:
            return  # not even remembered as a scheduled-message target
        prefixes = self._link.wake_prefixes() or []
        rest, prefixed = strip_wake_prefix(text, prefixes)
        conversation = group_conversation(group)
        msg = self._message(GROUP, group, conversation, user_id, name, group, rest or None, rest, prefixes)
        core.accept_group(msg)
        if not (at_self or prefixed):
            return
        command = parse_command(text, prefixes)
        if command is not None:
            await self._command(msg, command, text, prefixes)
        elif rest:
            await self._deliver(conversation, core.handle_group(msg))

    async def _command(self, msg: IncomingMessage, command: tuple[str, str], text: str, prefixes: list[str]) -> None:
        name, args = command
        rest, _ = strip_wake_prefix(text, prefixes)
        if name == "ping":
            await self._deliver(msg.ref, _single(PING_TEXT))
            return
        msg = replace(msg, command=name, args=args, text=None, message_str=rest)
        await self._deliver(msg.ref, self._core.handle_command(msg))

    async def _deliver(self, ref: str, batches) -> None:
        """Send each batch the core produces; the core goes on only after NapCat answered the send."""
        try:
            async for batch in batches:
                try:
                    await self._link.send(ref, list(batch.segments))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(f"[STBridge] NapCat send failed ({type(error).__name__})")
        finally:
            close = getattr(batches, "aclose", None)
            if close is not None:
                await close()


async def _single(text: str):
    yield ReplyBatch.of_text(text)
