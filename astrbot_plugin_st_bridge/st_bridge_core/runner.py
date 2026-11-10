"""Run the core as a process: ``python -m st_bridge_core --data-dir <dir>``.

Starts everything the core owns (the ST bridge, the console, the services), the connector WebSocket (AstrBot) and the
OneBot WebSocket (NapCat), then waits for Ctrl+C, SIGTERM or a shutdown request from a connector and stops it all in order.
"""

import argparse
import asyncio
import signal
import sys
import webbrowser
from pathlib import Path

from .connector_hub import ConnectorHub, RemotePlatform
from .connector_ws import ConnectorServer, ensure_token
from .core import BridgeCore
from .log import configure_process_logging, logger, set_logger
from .network_config import default_network_settings
from .onebot_inbound import OneBotInbound
from .onebot_link import OneBotLink
from .onebot_ws import TOKEN_FILE as ONEBOT_TOKEN_FILE, OneBotServer, onebot_url
from .platform_router import PlatformRouter
from .version import CORE_VERSION, RESTART_EXIT_CODE, running_build_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="st_bridge_core", description="The SillyTavern bridge core.")
    parser.add_argument("--data-dir", required=True, help="where config.json, the logs and the data files live")
    parser.add_argument("--launched-by", default="", help="who started this core (reported to connectors)")
    parser.add_argument("--open-console", action="store_true", help="open the console in the browser once it is up")
    return parser.parse_args(argv)


def open_browser(url: str) -> None:
    """Open a page in the default browser (a function of its own so tests can replace it)."""
    webbrowser.open(url)


async def run(
    data_dir: Path, launched_by: str = "", stop: asyncio.Event | None = None, open_console: bool = False
) -> int:
    """Run the core until ``stop`` is set (or a connector asks it to stop); returns the exit code."""
    stop = stop if stop is not None else asyncio.Event()
    restart = {"requested": False}
    # Computed now, before anything else: files replaced while this process runs must not change what it reports.
    build_id = running_build_id()
    remote = RemotePlatform()
    onebot = OneBotLink(nickname=lambda: core._nickname())
    router = PlatformRouter(remote, onebot, lambda: core.active_platform())
    core = BridgeCore(router, lambda: data_dir)
    OneBotInbound(core, onebot)
    onebot_token = ensure_token(data_dir, ONEBOT_TOKEN_FILE)
    hub = ConnectorHub(core, remote, token=ensure_token(data_dir), launched_by=launched_by, on_shutdown=stop.set)
    core.connector_status = hub.status

    def request_restart() -> dict:
        """Stop with the restart exit code a moment later (the console's answer is sent first)."""
        restart["requested"] = True
        logger.info("[STBridge] Restart requested from the console")
        asyncio.get_running_loop().call_later(0.4, stop.set)
        return {"auto": launched_by == "start.bat"}

    core.request_restart = request_restart
    server: ConnectorServer | None = None
    onebot_server: OneBotServer | None = None
    try:
        await core.initialize()
        ports = core._network_in_use or default_network_settings()
        core.onebot_info = lambda: {
            "port": ports["onebot_port"],
            "url": onebot_url(ports["onebot_port"]),
            "token": onebot_token,
            "status": onebot.status(),
        }
        server = ConnectorServer(hub, port=ports["connector_port"])
        await server.start()
        onebot_server = await _start_onebot(onebot, onebot_token, ports["onebot_port"])
    except Exception as error:
        if isinstance(error, OSError):
            # The usual cause is a second copy of the core: the first one holds the ports.
            ports = core._network_in_use or default_network_settings()
            logger.error(
                "[STBridge] 核心启动失败：端口被占用。STBridge 核心可能已经在运行（请不要重复启动），"
                f"或者别的程序占用了这些端口：桥接 {ports['bridge_port']}、控制台 {ports['console_port']}、"
                f"连接器 {ports['connector_port']}（{type(error).__name__}）"
            )
        else:
            logger.error(f"[STBridge] Core start failed ({type(error).__name__})")
        await _stop(core, server, hub, onebot_server)
        return 1
    logger.info(
        f"[STBridge] Core started version={CORE_VERSION} build_id={build_id} connector_port={ports['connector_port']} "
        f"onebot_port={ports['onebot_port']} platform={core.active_platform()} launched_by={launched_by or 'manual'}"
    )
    if open_console and core._console_service is not None:
        url = f"http://127.0.0.1:{ports['console_port']}/"
        try:
            open_browser(url)
        except Exception as error:
            logger.warning(f"[STBridge] Could not open the browser ({type(error).__name__}); the console is at {url}")
    await stop.wait()
    logger.info("[STBridge] Core stopping")
    await _stop(core, server, hub, onebot_server)
    logger.info("[STBridge] Core stopped")
    if restart["requested"]:
        if launched_by != "start.bat":
            logger.info("[STBridge] Core stopped for a restart; start it again (it was not started by start.bat)")
        return RESTART_EXIT_CODE
    return 0


async def _start_onebot(link: OneBotLink, token: str, port: int) -> OneBotServer | None:
    """Open the NapCat port; when that fails (say the port is taken) the core goes on without NapCat."""
    server = OneBotServer(link, token=token, port=port)
    try:
        await server.start()
    except Exception as error:
        link.unavailable_port = port
        logger.error(f"[STBridge] OneBot port {port} unavailable ({type(error).__name__}); NapCat cannot connect")
        return None
    return server


async def _stop(
    core: BridgeCore,
    server: ConnectorServer | None,
    hub: ConnectorHub | None = None,
    onebot_server: OneBotServer | None = None,
) -> None:
    try:
        await core.terminate()
    except Exception as error:
        logger.error(f"[STBridge] Core stop failed ({type(error).__name__})")
    if hub is not None:
        await hub.drain()
    if server is not None:
        try:
            await server.stop()
        except Exception as error:
            logger.error(f"[STBridge] Connector server stop failed ({type(error).__name__})")
    if onebot_server is not None:
        try:
            await onebot_server.stop()
        except Exception as error:
            logger.error(f"[STBridge] OneBot server stop failed ({type(error).__name__})")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import logging

    configure_process_logging(data_dir)
    set_logger(logging.getLogger("st_bridge_core"))

    async def runner() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                loop.add_signal_handler(number, stop.set)
            except (NotImplementedError, RuntimeError):
                try:
                    signal.signal(number, lambda *_: loop.call_soon_threadsafe(stop.set))
                except (ValueError, OSError):
                    pass
        return await run(data_dir, args.launched_by, stop, args.open_console)

    return asyncio.run(runner())
