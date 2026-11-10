"""The WebSocket server NapCat connects to ("WebSocket client" network in NapCat, URL ``ws://127.0.0.1:<port>/onebot``).

Same safety as the connector server, without any setup: it listens on 127.0.0.1 only, refuses every request that
carries an ``Origin`` header (a web page cannot talk to it) and wants ``Authorization: Bearer <token>`` with the
token from ``onebot_token`` in the data directory. It is always started, whichever chat platform is active.
"""

from __future__ import annotations

import hmac

from aiohttp import web

from .log import logger
from .onebot_link import OneBotLink

TOKEN_FILE = "onebot_token"
WS_PATH = "/onebot"
MAX_FRAME_BYTES = 16 * 1024 * 1024
HEARTBEAT_SECONDS = 30.0


def onebot_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}{WS_PATH}"


class OneBotServer:
    def __init__(self, link: OneBotLink, *, token: str, port: int, host: str = "127.0.0.1") -> None:
        self._link = link
        self._token = token
        self._host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(WS_PATH, self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, self.port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        # The connection is closed first: the web runner would otherwise wait for it for a minute.
        await self._link.stop()
        if runner is not None:
            await runner.cleanup()

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return hmac.compare_digest(header.encode("utf-8"), f"Bearer {self._token}".encode("utf-8"))

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if "Origin" in request.headers:
            logger.warning("[STBridge] NapCat rejected (OriginPresent)")
            return web.Response(status=403, text="forbidden")
        if not self._authorized(request):
            logger.warning("[STBridge] NapCat rejected (BadToken)")
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=HEARTBEAT_SECONDS)
        await ws.prepare(request)
        await self._link.serve(ws, request.headers.get("X-Self-ID"))
        return ws


__all__ = ["OneBotServer", "OneBotLink", "TOKEN_FILE", "WS_PATH", "onebot_url"]
