"""The AstrBot plugin's link to the core.

The core is its own program (STBridge's ``start.bat``); the plugin only connects to it. It never starts, replaces
or stops the core. Where the core keeps its data (the connector token and the ports) is written into
``core_link.json`` next to the plugin by the console's "install plugin" action::

    {"data_dir": "<release folder>\\data"}

Every connection attempt reads that file again, so the core can be started before or after AstrBot and can be
restarted at will. Why the plugin is not connected is logged once per reason, not on every retry.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .st_bridge_core.connector_client import ConnectorClient
from .st_bridge_core.connector_ws import connect_websocket, read_token
from .st_bridge_core.network_config import network_settings
from .st_bridge_core.platform_api import PlatformAPI
from .st_bridge_core.version import CORE_VERSION, compute_build_id

LINK_FILE = "core_link.json"
CLIENT_NAME = "astrbot"
RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0)

NOT_INSTALLED_TEXT = (
    "[STBridge] 找不到核心数据目录（{reason}）：请在 STBridge 控制台里重新安装 AstrBot 插件，并先运行 start.bat"
)
CORE_DOWN_TEXT = "[STBridge] STBridge 核心未运行，请先运行 start.bat（插件会自动连上）"
VERSION_MISMATCH_TEXT = (
    "[STBridge] 插件和核心版本不一致（核心 {core_version}/{core_build}，插件 {own_version}/{own_build}），"
    "请在控制台重装插件"
)


class CoreNotFound(RuntimeError):
    """The plugin does not know where the core's data directory is."""


class CoreLink:
    """What the plugin runs on: a client for the core, found through ``core_link.json``."""

    def __init__(
        self,
        *,
        platform: PlatformAPI,
        plugin_dir: Path,
        logger: Any,
        capabilities: Callable[[], dict[str, Any]],
        import_owner: Callable[[], dict[str, str] | None],
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        link_file: Path | None = None,
    ) -> None:
        self._plugin_dir = Path(plugin_dir)
        self._link_file = Path(link_file) if link_file is not None else self._plugin_dir / LINK_FILE
        self._core_dir = self._plugin_dir / "st_bridge_core"
        self._logger = logger
        # The one reason (if any) the plugin is not connected that has been logged; cleared by a connection.
        self._reported: str | None = None
        self._mismatch_reported: tuple | None = None
        self.client = ConnectorClient(
            name=CLIENT_NAME,
            version=CORE_VERSION,
            platform=platform,
            connect=self._connect,
            token=self._token,
            capabilities=capabilities,
            import_owner=import_owner,
            on_connected=self._on_connected,
            retry_delays=retry_delays,
        )

    # ---- where the core is ----------------------------------------------------------------------
    def data_dir(self) -> Path:
        """The core's data directory from ``core_link.json``; CoreNotFound when it is missing or invalid."""
        try:
            raw = json.loads(self._link_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CoreNotFound("没有 core_link.json") from None
        except (OSError, ValueError):
            raise CoreNotFound("core_link.json 无法读取") from None
        value = raw.get("data_dir") if isinstance(raw, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise CoreNotFound("core_link.json 里没有 data_dir")
        return Path(value.strip())

    def _ports(self, data_dir: Path) -> dict[str, int]:
        try:
            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        return network_settings(config)

    def _token(self) -> str | None:
        try:
            return read_token(self.data_dir())
        except CoreNotFound:
            return None

    def _report(self, key: str, text: str) -> None:
        if self._reported != key:
            self._reported = key
            self._logger.warning(text)

    async def _connect(self):
        try:
            data_dir = self.data_dir()
        except CoreNotFound as error:
            self._report(f"link:{error}", NOT_INSTALLED_TEXT.format(reason=error))
            raise
        try:
            return await connect_websocket(self._ports(data_dir)["connector_port"])
        except Exception:
            self._report("down", CORE_DOWN_TEXT)
            raise

    # ---- start / stop ---------------------------------------------------------------------------
    async def start(self) -> None:
        self.client.start()

    async def stop(self) -> None:
        """Disconnect. The core is never asked to stop: it is not the plugin's."""
        await self.client.stop()

    # ---- the core answered ----------------------------------------------------------------------
    async def _on_connected(self, hello: dict[str, Any]) -> None:
        self._reported = None  # a later outage is worth one new line
        own = (CORE_VERSION, compute_build_id(self._core_dir))
        theirs = (hello.get("core_version"), hello.get("build_id"))
        if theirs == own:
            self._mismatch_reported = None
            return
        if self._mismatch_reported != theirs:
            self._mismatch_reported = theirs
            self._logger.warning(
                VERSION_MISMATCH_TEXT.format(
                    core_version=theirs[0], core_build=theirs[1], own_version=own[0], own_build=own[1]
                )
            )
