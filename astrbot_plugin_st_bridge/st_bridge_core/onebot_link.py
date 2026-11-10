"""NapCat (OneBot v11) inside the core: the connection NapCat opens to the core, its actions, and the PlatformAPI on top.

NapCat connects as a "WebSocket client" (reverse WebSocket, Universal): events and actions share one connection.
Only one connection is kept; a new one replaces the old. This module knows nothing about messages of the bridge;
``OneBotInbound`` (onebot_inbound.py) turns events into core calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType

from .chat_platform import parse_conversation
from .log import logger
from .platform_api import PlatformAPI, Segment

ACTION_TIMEOUT_SECONDS = 30.0
# A voice has to be uploaded and converted by NapCat first.
VOICE_TIMEOUT_SECONDS = 60.0


class OneBotOffline(RuntimeError):
    """NapCat is not connected (or the connection ended), so no action can be done."""


class OneBotActionError(RuntimeError):
    """NapCat answered an action with a failure (``retcode``) or not at all in time."""

    def __init__(self, action: str, retcode: object) -> None:
        super().__init__(f"{action} failed ({retcode})")
        self.action = action
        self.retcode = retcode


class _Connection:
    def __init__(self, ws: Any, self_id: str | None, clock: Callable[[], float]) -> None:
        self.ws = ws
        self.self_id = self_id
        self.since = datetime.now().isoformat(timespec="seconds")
        self.connected_at = clock()
        self.last_heartbeat: float | None = None
        self.heartbeat_ok: bool | None = None


def file_ref(path: Path) -> str:
    """A local file as NapCat wants it: an absolute path with forward slashes."""
    return str(Path(path).resolve()).replace("\\", "/")


def build_messages(segments: list[Segment]) -> list[list[dict[str, Any]]]:
    """Segments to OneBot messages, in order: text and images share a message, a voice is always alone."""
    messages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for segment in segments:
        if segment.kind == "voice":
            if current:
                messages.append(current)
                current = []
            messages.append([{"type": "record", "data": {"file": file_ref(segment.path)}}])
        elif segment.kind == "image":
            current.append({"type": "image", "data": {"file": file_ref(segment.path)}})
        elif segment.kind == "text":
            if segment.text:
                current.append({"type": "text", "data": {"text": segment.text}})
        else:
            raise ValueError(f"unknown segment kind {segment.kind!r}")
    if current:
        messages.append(current)
    return messages


class OneBotLink(PlatformAPI):
    """The state of NapCat's connection and the actions on it; as a PlatformAPI it sends to NapCat conversations."""

    def __init__(
        self,
        *,
        nickname: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._nickname = nickname
        self._clock = clock
        self._conn: _Connection | None = None
        self._pending: dict[str, tuple[_Connection, asyncio.Future]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sequence = 0
        # Set by whoever handles the events (OneBotInbound.on_event).
        self.on_event: Callable[[dict[str, Any], _Connection], Awaitable[None]] | None = None
        # True once a message arrived in the "string" format (the bridge needs "array").
        self.format_warning = False
        # Set by the runner when the OneBot port could not be opened (the core runs on without NapCat).
        self.unavailable_port: int | None = None

    # ---- state -----------------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        conn = self._conn
        return conn is not None and not conn.ws.closed

    @property
    def self_id(self) -> str | None:
        conn = self._conn
        return conn.self_id if conn is not None else None

    def status(self) -> dict[str, Any]:
        """Console view; never the token."""
        conn = self._conn
        # port_unavailable: the port could not be opened when the core started (NapCat cannot connect at all).
        common = {"format_warning": self.format_warning, "port_unavailable": self.unavailable_port is not None, "port": self.unavailable_port}
        if conn is None or conn.ws.closed:
            return {"connected": False, "self_id": None, "since": None, "heartbeat_age": None, **common}
        age = None if conn.last_heartbeat is None else max(0.0, self._clock() - conn.last_heartbeat)
        return {
            "connected": True,
            "self_id": conn.self_id,
            "since": conn.since,
            "heartbeat_age": None if age is None else round(age, 1),
            **common,
        }

    # ---- one connection --------------------------------------------------------------------
    async def serve(self, ws: Any, self_id: str | None = None) -> None:
        """Serve one NapCat connection until it ends; it replaces the one before."""
        conn = _Connection(ws, self_id if self_id and self_id.isdigit() else None, self._clock)
        previous, self._conn = self._conn, conn
        if previous is not None:
            self._fail_pending(previous)
            logger.info("[STBridge] NapCat connection replaced")
            try:
                await previous.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
            except Exception:
                pass
        else:
            logger.info("[STBridge] NapCat connected")
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    self._on_text(conn, message.data)
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
        finally:
            self._fail_pending(conn)
            if self._conn is conn:
                self._conn = None
                logger.info("[STBridge] NapCat disconnected")

    def _on_text(self, conn: _Connection, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        post_type = data.get("post_type")
        if post_type is None:
            # An answer to an action (it has no post_type).
            echo = data.get("echo")
            entry = self._pending.get(echo) if isinstance(echo, str) else None
            if entry is not None and entry[0] is conn and not entry[1].done():
                entry[1].set_result(data)
            return
        self_id = data.get("self_id")
        if conn.self_id is None and isinstance(self_id, (int, str)) and str(self_id).isdigit():
            conn.self_id = str(self_id)
        if post_type == "meta_event":
            if data.get("meta_event_type") == "heartbeat":
                conn.last_heartbeat = self._clock()
                status = data.get("status")
                conn.heartbeat_ok = bool(status.get("online")) if isinstance(status, dict) else None
            return
        handler = self.on_event
        if handler is None:
            return
        task = asyncio.ensure_future(self._run_event(handler, data, conn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run_event(handler: Callable[..., Awaitable[None]], data: dict[str, Any], conn: _Connection) -> None:
        try:
            await handler(data, conn)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"[STBridge] NapCat event failed ({type(error).__name__})")

    def _fail_pending(self, conn: _Connection) -> None:
        for echo, (owner, future) in list(self._pending.items()):
            if owner is conn and not future.done():
                future.set_exception(OneBotOffline("NapCat connection ended"))

    async def stop(self) -> None:
        """Close the connection first (the web runner would otherwise wait for it), then end the event tasks."""
        conn, self._conn = self._conn, None
        if conn is not None:
            self._fail_pending(conn)
            try:
                await conn.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
            except Exception:
                pass
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- actions ---------------------------------------------------------------------------
    async def call(self, action: str, params: dict[str, Any], timeout: float = ACTION_TIMEOUT_SECONDS) -> Any:
        """Do an OneBot action and return its ``data``; raises OneBotOffline / OneBotActionError."""
        conn = self._conn
        if conn is None or conn.ws.closed:
            raise OneBotOffline("NapCat is not connected")
        self._sequence += 1
        echo = f"stb-{self._sequence}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = (conn, future)
        try:
            await conn.ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            try:
                answer = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                logger.warning(f"[STBridge] NapCat action timed out action={action}")
                raise OneBotActionError(action, "timeout") from None
        except (ConnectionError, RuntimeError) as error:
            if isinstance(error, (OneBotOffline, OneBotActionError)):
                raise
            raise OneBotOffline("NapCat is not connected") from error
        finally:
            self._pending.pop(echo, None)
        retcode = answer.get("retcode")
        if retcode != 0 or answer.get("status") == "failed":
            logger.warning(f"[STBridge] NapCat action failed action={action} retcode={retcode}")
            raise OneBotActionError(action, retcode)
        return answer.get("data")

    # ---- PlatformAPI ------------------------------------------------------------------------
    async def send_to(self, conversation_id: str, segments: list[Segment]) -> bool:
        target = parse_conversation(conversation_id)
        if target is None:
            return False
        kind, number = target
        for message in build_messages(list(segments)):
            voice = message[0]["type"] == "record"
            if kind == "private":
                action, params = "send_private_msg", {"user_id": int(number), "message": message}
            else:
                action, params = "send_group_msg", {"group_id": int(number), "message": message}
            await self.call(action, params, VOICE_TIMEOUT_SECONDS if voice else ACTION_TIMEOUT_SECONDS)
        return True

    async def send(self, ref: str, segments: list[Segment]) -> None:
        # The ref of a NapCat message is its conversation id: there is no passive-reply window to find again.
        if not await self.send_to(ref, segments):
            raise RuntimeError("message reference is not a NapCat conversation")

    def wake_prefixes(self) -> list[str] | None:
        """NapCat has no wake prefix setting: the nickname (with and without "/") wakes the character in a group."""
        name = ""
        try:
            name = self._nickname() if self._nickname is not None else ""
        except Exception:
            name = ""
        return [f"/{name}", name] if name else None
