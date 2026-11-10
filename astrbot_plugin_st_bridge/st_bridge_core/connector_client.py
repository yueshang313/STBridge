"""The connector's side of the protocol, for any chat platform: connect, say hello, keep the connection up
(reconnecting with a back-off), call the core and stream replies from it, and serve the core's calls with a
PlatformAPI of the connector's own.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from .connector_protocol import (
    ConnectionLost,
    ProtocolError,
    RpcPeer,
    Transport,
    segments_from_list,
)
from .log import logger
from .platform_api import PlatformAPI

HELLO_TIMEOUT_SECONDS = 10.0
RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)


class ConnectorUnavailable(RuntimeError):
    """The core cannot be reached right now (not connected)."""


def _field(params: Any, key: str) -> Any:
    if not isinstance(params, dict) or key not in params:
        raise ProtocolError(f"missing {key}")
    return params[key]


class ConnectorClient:
    def __init__(
        self,
        *,
        name: str,
        version: str,
        platform: PlatformAPI,
        connect: Callable[[], Awaitable[Transport]],
        token: Callable[[], str | None],
        capabilities: Callable[[], dict[str, Any]] | None = None,
        import_owner: Callable[[], dict[str, str] | None] | None = None,
        on_connected: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        on_demand: bool = False,
    ) -> None:
        self.name = name
        self.version = version
        self._platform = platform
        self._connect = connect
        self._token = token
        self._capabilities = capabilities or (lambda: {})
        self._import_owner = import_owner or (lambda: None)
        self._on_connected = on_connected
        self._retry_delays = retry_delays
        # Connect when a call finds no connection (in-process use and tests; a real connector runs start()).
        self._on_demand = on_demand
        self._peer: RpcPeer | None = None
        self._runner: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._stopping = False
        # What the core said in its last hello (core_version, launched_by, cache); kept while it is away.
        self.last_hello: dict[str, Any] | None = None

    # ---- state ----------------------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._peer is not None and not self._peer.closed

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # ---- the core calling us --------------------------------------------------------------------
    def _handlers(self) -> dict[str, Callable[[Any], Awaitable[Any]]]:
        platform = self._platform

        async def send(params):
            await platform.send(_field(params, "ref"), segments_from_list(_field(params, "segments")))

        async def send_to(params):
            return bool(await platform.send_to(_field(params, "conversation_id"), segments_from_list(_field(params, "segments"))))

        async def can_preupload_image(params):
            return bool(await platform.can_preupload_image(_field(params, "ref")))

        async def preupload_image(params):
            return await platform.preupload_image(_field(params, "ref"), Path(_field(params, "path")))

        async def send_uploaded_image(params):
            await platform.send_uploaded_image(_field(params, "ref"), _field(params, "handle"))

        return {
            "send": send,
            "send_to": send_to,
            "can_preupload_image": can_preupload_image,
            "preupload_image": preupload_image,
            "send_uploaded_image": send_uploaded_image,
        }

    # ---- connecting -----------------------------------------------------------------------------
    async def _session(self, transport: Transport) -> RpcPeer:
        """Say hello over a fresh transport; on success the peer is the live connection."""
        peer = RpcPeer(transport, "c", handlers=self._handlers())
        runner = asyncio.create_task(peer.run())
        try:
            hello = await peer.call(
                "hello",
                {
                    "name": self.name,
                    "version": self.version,
                    "token": self._token(),
                    "capabilities": self._capabilities(),
                    "import_owner": self._import_owner(),
                },
                HELLO_TIMEOUT_SECONDS,
            )
            if not isinstance(hello, dict):
                raise ProtocolError("hello answer must be an object")
        except BaseException:
            await peer.close()
            await asyncio.gather(runner, return_exceptions=True)
            raise
        self._peer, self._runner, self.last_hello = peer, runner, hello
        self._connected.set()
        return peer

    async def connect_once(self) -> bool:
        """One connection attempt (also used on demand); True when connected afterwards."""
        if self.connected:
            return True
        try:
            transport = await self._connect()
            await self._session(transport)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        await self._notify_connected()
        return True

    async def _notify_connected(self) -> None:
        if self._on_connected is not None and self.last_hello is not None:
            try:
                await self._on_connected(self.last_hello)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[STBridge] Connector hello hook failed ({type(error).__name__})")

    def start(self) -> None:
        """Keep a connection to the core in the background."""
        if self._loop_task is None:
            self._stopping = False
            self._loop_task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        attempt = 0
        reported = False
        while not self._stopping:
            try:
                transport = await self._connect()
                await self._session(transport)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not reported:
                    logger.info(f"[STBridge] Waiting for the core ({type(error).__name__})")
                    reported = True
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
                continue
            attempt, reported = 0, False
            await self._notify_connected()
            runner = self._runner
            if runner is not None:
                await asyncio.gather(runner, return_exceptions=True)
            self._connected.clear()
            self._peer = None
            if not self._stopping:
                logger.warning("[STBridge] Connection to the core lost; reconnecting")
                await asyncio.sleep(self._retry_delays[0])

    async def stop(self) -> None:
        self._stopping = True
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        peer, self._peer = self._peer, None
        self._connected.clear()
        if peer is not None:
            await peer.close()
        runner, self._runner = self._runner, None
        if runner is not None:
            await asyncio.gather(runner, return_exceptions=True)

    # ---- calling the core -----------------------------------------------------------------------
    async def ensure(self) -> bool:
        """Is the core connected? (With on-demand connecting this makes the connection first.)"""
        if not self.connected and self._on_demand:
            await self.connect_once()
        return self.connected

    async def _live_peer(self) -> RpcPeer:
        if not self.connected and self._on_demand:
            await self.connect_once()
        peer = self._peer
        if peer is None or peer.closed:
            raise ConnectorUnavailable("core not connected")
        return peer

    async def call(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        return await (await self._live_peer()).call(method, params, timeout)

    async def stream(self, method: str, params: Any = None) -> AsyncIterator[Any]:
        inner = (await self._live_peer()).stream(method, params)
        try:
            async for item in inner:
                yield item
        finally:
            await inner.aclose()


__all__ = ["ConnectorClient", "ConnectorUnavailable", "ConnectionLost"]
