"""The connector protocol over a local WebSocket: the core's server and a connector's client transport.

Safety without any setup: the server listens on 127.0.0.1 only, refuses every request that carries an ``Origin``
header (a browser always sends one, a connector never does, so a web page cannot talk to the core), and the
hello must carry the token from ``connector_token`` in the data directory.
"""

import secrets
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from .connector_hub import ConnectorHub
from .connector_protocol import Transport
from .log import logger

TOKEN_FILE = "connector_token"
WS_PATH = "/connector"
MAX_FRAME_BYTES = 8 * 1024 * 1024
HEARTBEAT_SECONDS = 30.0


def ensure_token(data_dir: Path, filename: str = TOKEN_FILE) -> str:
    """A token file of the data directory (the connector's by default): created with random content on first use, then read back."""
    path = Path(data_dir) / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def read_token(data_dir: Path) -> str | None:
    """The token a connector presents; None while the core has not created it yet."""
    try:
        text = (Path(data_dir) / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


class WebSocketTransport(Transport):
    """A Transport over an aiohttp WebSocket (either end)."""

    def __init__(self, ws, session: aiohttp.ClientSession | None = None) -> None:
        self._ws = ws
        self._session = session

    async def send(self, text: str) -> None:
        if self._ws.closed:
            raise ConnectionError("websocket closed")
        await self._ws.send_str(text)

    async def receive(self) -> str | None:
        while True:
            message = await self._ws.receive()
            if message.type == WSMsgType.TEXT:
                return message.data
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                return None

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            if self._session is not None:
                await self._session.close()


async def connect_websocket(port: int, host: str = "127.0.0.1") -> WebSocketTransport:
    """Open the connector WebSocket of a core (no Origin header is sent)."""
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(
            f"http://{host}:{port}{WS_PATH}", max_msg_size=MAX_FRAME_BYTES, heartbeat=HEARTBEAT_SECONDS
        )
    except BaseException:
        await session.close()
        raise
    return WebSocketTransport(ws, session)


class ConnectorServer:
    """Accepts connector connections and hands them to the hub."""

    def __init__(self, hub: ConnectorHub, *, port: int, host: str = "127.0.0.1") -> None:
        self._hub = hub
        self._host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._open: set[web.WebSocketResponse] = set()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(WS_PATH, self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self._host, self.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        # Connections are closed first: the web runner would otherwise wait for them for a minute.
        for ws in list(self._open):
            await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
        if runner is not None:
            await runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if "Origin" in request.headers:
            logger.warning("[STBridge] Connector rejected (OriginPresent)")
            return web.Response(status=403, text="forbidden")
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=HEARTBEAT_SECONDS)
        await ws.prepare(request)
        self._open.add(ws)
        try:
            await self._hub.serve(WebSocketTransport(ws))
        finally:
            self._open.discard(ws)
        return ws
