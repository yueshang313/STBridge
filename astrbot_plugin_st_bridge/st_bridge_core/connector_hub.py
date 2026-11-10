"""The core's side of the connector protocol: the platform that forwards to the connected connector, and the hub
that serves a connector's calls with the core.

Methods the connector calls: ``hello`` (first, with the token), ``accept_private``, ``accept_group``,
``shutdown``; streams ``handle_private``, ``handle_group``, ``handle_command``.
Methods the core calls on the connector: ``send``, ``send_to``, ``can_preupload_image``, ``preupload_image``,
``send_uploaded_image``.
"""

import asyncio
import hmac
from datetime import datetime
from pathlib import Path
from collections.abc import Awaitable
from typing import Any, Callable

from .connector_protocol import (
    ProtocolError,
    RpcPeer,
    Transport,
    batch_to_dict,
    message_from_dict,
    segments_to_list,
)
from .chat_platform import ASTRBOT
from .core import BridgeCore
from .log import logger
from .platform_api import PlatformAPI, Segment
from .version import CORE_VERSION, PROTOCOL_VERSION, running_build_id

PREUPLOAD_TIMEOUT_SECONDS = 60.0


class ConnectorOffline(RuntimeError):
    """No connector is connected, so the core cannot reach the chat platform."""


class RemotePlatform(PlatformAPI):
    """The PlatformAPI the core runs on: every ability is a call to the connector that is connected now."""

    def __init__(self) -> None:
        self.peer: RpcPeer | None = None
        self.capabilities: dict[str, Any] = {}
        # In-process use only: makes the connection when a call finds none (a real core waits for connectors).
        self.ensure_connected: Callable[[], Awaitable[Any]] | None = None

    async def _call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        peer = self.peer
        if (peer is None or peer.closed) and self.ensure_connected is not None:
            await self.ensure_connected()
            peer = self.peer
        if peer is None or peer.closed:
            raise ConnectorOffline("no connector connected")
        return await peer.call(method, params, timeout)

    async def send(self, ref: str, segments: list[Segment]) -> None:
        await self._call("send", {"ref": ref, "segments": segments_to_list(segments)})

    async def send_to(self, conversation_id: str, segments: list[Segment]) -> bool:
        return bool(await self._call("send_to", {"conversation_id": conversation_id, "segments": segments_to_list(segments)}))

    def wake_prefixes(self) -> list[str] | None:
        prefixes = self.capabilities.get("wake_prefixes")
        if isinstance(prefixes, list) and all(isinstance(item, str) for item in prefixes):
            return list(prefixes)
        return None

    def extra_temp_dir(self) -> Path | None:
        path = self.capabilities.get("extra_temp_dir")
        return Path(path) if isinstance(path, str) and path else None

    async def can_preupload_image(self, ref: str) -> bool:
        return bool(await self._call("can_preupload_image", {"ref": ref}))

    async def preupload_image(self, ref: str, path: Path) -> str:
        handle = await self._call("preupload_image", {"ref": ref, "path": str(path)}, PREUPLOAD_TIMEOUT_SECONDS)
        if not isinstance(handle, str):
            raise ProtocolError("upload handle must be a string")
        return handle

    async def send_uploaded_image(self, ref: str, handle: str) -> None:
        await self._call("send_uploaded_image", {"ref": ref, "handle": handle})


class ConnectorHub:
    """Serves connectors: one at a time, a new one replaces the old."""

    def __init__(
        self,
        core: BridgeCore,
        platform: RemotePlatform,
        *,
        token: str | None,
        launched_by: str = "",
        on_shutdown: Callable[[], None] | None = None,
        log_connections: bool = True,
    ) -> None:
        self._core = core
        self._platform = platform
        self._token = token
        self.launched_by = launched_by
        self._on_shutdown = on_shutdown
        self._log_connections = log_connections
        self._info: dict[str, Any] | None = None
        self._peer: RpcPeer | None = None

    def status(self) -> dict[str, Any]:
        """Console view: who is connected (never the token)."""
        info = self._info
        if info is None or self._peer is None or self._peer.closed:
            return {"connected": False, "name": None, "version": None, "since": None}
        return {"connected": True, **info}

    async def drain(self, timeout: float = 5.0) -> None:
        """After the core stopped: give the replies still on their way (a waiting message's last line) time to arrive."""
        peer = self._peer
        if peer is not None and not peer.closed:
            await peer.drain(timeout)

    async def serve(self, transport: Transport) -> None:
        """Serve one connection until it ends."""
        state = {"hello": False}

        def guarded(handler):
            async def run(params):
                if not state["hello"]:
                    raise PermissionError("hello first")
                return await handler(params)
            return run

        def guarded_stream(producer):
            async def run(params):
                if not state["hello"]:
                    raise PermissionError("hello first")
                inner = producer(params)
                try:
                    async for item in inner:
                        yield item
                finally:
                    await inner.aclose()
            return run

        async def hello(params):
            try:
                return await self._hello(peer, state, params)
            except Exception:
                # A connector that cannot say hello properly gets no connection at all.
                asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(peer.close()))
                raise

        peer = RpcPeer(
            transport,
            "s",
            handlers={
                "hello": hello,
                "accept_private": guarded(self._accept_private),
                "accept_group": guarded(self._accept_group),
                "shutdown": guarded(self._shutdown),
            },
            streams={
                "handle_private": guarded_stream(self._handle_private),
                "handle_group": guarded_stream(self._handle_group),
                "handle_command": guarded_stream(self._handle_command),
            },
        )
        try:
            await peer.run()
        finally:
            if self._peer is peer:
                self._peer = None
                self._platform.peer = None
                name = (self._info or {}).get("name")
                self._info = None
                if self._log_connections:
                    logger.info(f"[STBridge] Connector disconnected (name={name})")

    # ---- the hello ------------------------------------------------------------------------------
    async def _hello(self, peer: RpcPeer, state: dict, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ProtocolError("hello must be an object")
        token = params.get("token")
        if self._token is not None and not (
            isinstance(token, str) and hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8"))
        ):
            logger.warning("[STBridge] Connector rejected (BadToken)")
            raise PermissionError("bad token")
        name, version = params.get("name"), params.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str):
            raise ProtocolError("hello needs a name and a version")
        capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
        previous = self._peer
        state["hello"] = True
        self._peer = peer
        self._platform.peer = peer
        self._platform.capabilities = dict(capabilities)
        self._info = {"name": name, "version": version, "since": datetime.now().isoformat(timespec="seconds")}
        if previous is not None and previous is not peer:
            logger.info(f"[STBridge] Connector replaced (new={name})")
            await previous.close()
        if self._log_connections:
            logger.info(f"[STBridge] Connector connected name={name} version={version}")
        owner = params.get("import_owner")
        if isinstance(owner, dict):
            await self._core.import_operator(owner.get("platform_id"), owner.get("sender_id"))
        return {
            "protocol": PROTOCOL_VERSION,
            "core_version": CORE_VERSION,
            "build_id": running_build_id(),
            "launched_by": self.launched_by,
            "cache": self._core.connector_cache(),
        }

    # ---- what connectors call -------------------------------------------------------------------
    def _in_use(self) -> bool:
        """Is AstrBot the chat platform in use? With NapCat active the connector's messages are ignored silently."""
        return self._core.active_platform() == ASTRBOT

    async def _accept_private(self, params: Any) -> bool:
        message = message_from_dict(_field(params, "message"))
        return self._in_use() and bool(self._core.accept_private(message))

    async def _accept_group(self, params: Any) -> None:
        message = message_from_dict(_field(params, "message"))
        if self._in_use():
            self._core.accept_group(message)

    async def _shutdown(self, params: Any) -> None:
        logger.info("[STBridge] Shutdown requested by the connector")
        if self._on_shutdown is not None:
            self._on_shutdown()

    async def _handle_private(self, params: Any):
        message = message_from_dict(_field(params, "message"))
        if not self._in_use():
            return
        async for batch in self._core.handle_private(message):
            yield batch_to_dict(batch)

    async def _handle_group(self, params: Any):
        message = message_from_dict(_field(params, "message"))
        if not self._in_use():
            return
        async for batch in self._core.handle_group(message):
            yield batch_to_dict(batch)

    async def _handle_command(self, params: Any):
        message = message_from_dict(_field(params, "message"))
        if not self._in_use():
            return
        async for batch in self._core.handle_command(message):
            yield batch_to_dict(batch)


def _field(params: Any, key: str) -> Any:
    if not isinstance(params, dict) or key not in params:
        raise ProtocolError(f"missing {key}")
    return params[key]

