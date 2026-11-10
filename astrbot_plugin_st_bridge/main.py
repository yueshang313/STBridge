"""AstrBot connector plugin for the SillyTavern bridge.

AstrBot binds @filter handlers by their defining module, so the handlers live here. The bridge itself is the
platform-independent core (``st_bridge_core``), which runs as its own program (STBridge's start.bat); this plugin
only connects to it over a local WebSocket (it never starts or stops it) and is only the AstrBot side: it turns
AstrBot events into neutral messages, asks the core what to do with them and turns the reply batches it streams
back into AstrBot results (see ``astrbot_platform`` and ``core_link``). While the core is not connected, messages that would have been
bridged get the core's own "unavailable" line (kept from the last hello). Names the tests read from this module
are re-exported below.
"""

import random
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

from .astrbot_platform import AstrBotPlatform
from .core_link import CoreLink
from .st_bridge_core.bridge_types import _Origin, _Speaker, _config_string, extract_bilingual_reply, split_game_block
from .st_bridge_core.connector_client import ConnectorUnavailable
from .st_bridge_core.connector_protocol import ConnectionLost, batch_from_dict, message_to_dict
from .st_bridge_core.emotion_config import GROUP_TRACK, OWNER_TRACK
from .st_bridge_core.http_protocol import DEFAULT_MAX_REQUEST_TEXT_BYTES
from .st_bridge_core.http_service import HttpServiceState
from .st_bridge_core.log import set_logger
from .st_bridge_core.mixin_bridge import (
    INVALID_REPLY_TEXT,
    QUEUE_FULL_TEXT,
    QUEUE_TIMEOUT_TEXT,
    TOO_LONG_TEXT,
    UNAVAILABLE_TEXT,
)
from .st_bridge_core.mixin_lifecycle import QUEUE_ACTIVE_HOLD_LIMIT_SECONDS
from .st_bridge_core.mixin_song import (
    SONG_BUSY_TEXT,
    SONG_CANCEL_TEXT,
    SONG_PICK_RANGE_TEXT,
    SONG_QUEUE_FULL_TEXT,
    SONG_RESEARCH_EMPTY_TEXT,
    SONG_RESEARCH_HINT,
    SONG_USAGE_TEXT,
)
from .st_bridge_core.platform_api import GROUP, PRIVATE, IncomingMessage


PLUGIN_NAME = "astrbot_plugin_st_bridge"

__all__ = [
    "DEFAULT_MAX_REQUEST_TEXT_BYTES",
    "GROUP_TRACK",
    "HttpServiceState",
    "INVALID_REPLY_TEXT",
    "OWNER_TRACK",
    "PLUGIN_NAME",
    "QUEUE_ACTIVE_HOLD_LIMIT_SECONDS",
    "QUEUE_FULL_TEXT",
    "QUEUE_TIMEOUT_TEXT",
    "SONG_BUSY_TEXT",
    "SONG_CANCEL_TEXT",
    "SONG_PICK_RANGE_TEXT",
    "SONG_QUEUE_FULL_TEXT",
    "SONG_RESEARCH_EMPTY_TEXT",
    "SONG_RESEARCH_HINT",
    "SONG_USAGE_TEXT",
    "STBridgePlugin",
    "TOO_LONG_TEXT",
    "UNAVAILABLE_TEXT",
    "_Origin",
    "_Speaker",
    "extract_bilingual_reply",
    "filter",
    "logger",
    "random",
    "split_game_block",
    "star",
]

# Failures that mean "the core is not there": the message is answered with the "unavailable" line.
_CORE_DOWN = (ConnectorUnavailable, ConnectionLost)


class STBridgePlugin(star.Star):
    """The AstrBot side of the bridge: connects to the core and carries messages both ways."""

    def __init__(
        self,
        context: star.Context,
        config: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(context, config)
        set_logger(logger)
        # Legacy bridge_token / console_token values in saved configs are ignored.
        settings = config if isinstance(config, Mapping) else {}
        # The owner now lives in the console; these two settings are only handed over once, at the first hello.
        platform_id = _config_string(settings, "operator_platform_id")
        sender_id = _config_string(settings, "operator_sender_id")
        self._owner_hint = (
            {"platform_id": platform_id, "sender_id": sender_id} if platform_id and sender_id else None
        )
        self._platform = AstrBotPlatform(lambda: self.context)
        # Only the in-process test cores use this; the real core's data directory comes from core_link.json.
        self._data_dir_provider = lambda: Path(star.StarTools.get_data_dir(PLUGIN_NAME))
        self._runtime = self._create_runtime()
        self._client = self._runtime.client

    def _create_runtime(self):
        return CoreLink(
            platform=self._platform,
            plugin_dir=Path(__file__).resolve().parent,
            logger=logger,
            capabilities=self._platform.capabilities,
            import_owner=lambda: self._owner_hint,
        )

    async def initialize(self) -> None:
        await self._runtime.start()

    async def terminate(self) -> None:
        # AstrBot only calls terminate when it is in this class's own __dict__
        # (star_manager._terminate_plugin), so it must be defined here, not inherited.
        # Only disconnects: the core is not the plugin's and keeps running.
        await self._runtime.stop()

    # ---- while the core is away -----------------------------------------------------------------
    def _cached(self) -> dict:
        hello = self._client.last_hello
        cache = hello.get("cache") if isinstance(hello, dict) else None
        return cache if isinstance(cache, dict) else {}

    def _offline_text(self) -> str:
        """The core's "unavailable" line from the last hello; the built-in one before any hello."""
        text = self._cached().get("unavailable_text")
        return text if isinstance(text, str) and text else UNAVAILABLE_TEXT

    def _offline_operator(self, msg: IncomingMessage) -> bool:
        """Is this the owner's private message, as far as the last hello (or the plugin's own setting) knows?"""
        owner = self._cached().get("operator")
        if not (isinstance(owner, dict) and owner.get("platform_id") and owner.get("sender_id")):
            owner = self._owner_hint
        return bool(
            owner
            and msg.chat_type == PRIVATE
            and msg.platform_id == owner["platform_id"]
            and msg.sender_id == owner["sender_id"]
        )

    # ---- talking to the core --------------------------------------------------------------------
    async def _send_batches(self, event: AstrMessageEvent, method: str, msg: IncomingMessage):
        """The core's reply batches as AstrBot results; the core goes on after each one has been sent."""
        try:
            async for item in self._client.stream(method, {"message": message_to_dict(msg)}):
                yield self._platform.result(event, batch_from_dict(item))
        except _CORE_DOWN:
            logger.warning("[STBridge] The core went away while a message was being handled")
            yield event.plain_result(self._offline_text())

    async def _run_command(self, event: AstrMessageEvent, command: str, args: str = ""):
        event.should_call_llm(True)
        msg = self._platform.incoming(event, command=command, args=args)
        if not await self._client.ensure():
            # "sing" is silent for anyone who is neither the owner nor in a group.
            if command != "sing" or msg.chat_type == GROUP or self._offline_operator(msg):
                yield event.plain_result(self._offline_text())
            return
        async for result in self._send_batches(event, "handle_command", msg):
            yield result

    @filter.command("stbridge_ping")
    async def stbridge_ping(self, event: AstrMessageEvent):
        """Return a fixed message confirming that the plugin is loaded."""
        yield event.plain_result("ST Bridge 插件已加载")

    @filter.command("好感度")
    async def stbridge_affinity(self, event: AstrMessageEvent):
        """Report the sender's own affinity in private or group chat."""
        async for result in self._run_command(event, "affinity"):
            yield result

    @filter.command("好感度设置")
    async def stbridge_affinity_set(self, event: AstrMessageEvent, value: str = ""):
        """Operator-only, private-only: set the operator's own affinity for testing."""
        async for result in self._run_command(event, "affinity_set", value):
            yield result

    @filter.command("stbridge_recover")
    async def stbridge_recover(self, event: AstrMessageEvent):
        """Manual fallback; the ST client normally opens the gate by reporting idle."""
        async for result in self._run_command(event, "recover"):
            yield result

    @filter.command("唱")
    async def stbridge_sing(self, event: AstrMessageEvent, value: str = ""):
        """Order a song: `唱 歌名`. The requester then picks a version from a numbered list."""
        async for result in self._run_command(event, "sing"):
            yield result

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE,
        priority=-1,
    )
    async def stbridge_private_message(self, event: AstrMessageEvent):
        """Bridge plain private messages from the configured operator only."""
        msg = self._platform.incoming(event, text=self._platform.private_text(event))
        try:
            accepted = bool(await self._client.call("accept_private", {"message": message_to_dict(msg)}))
            offline = False
        except _CORE_DOWN:
            accepted, offline = self._offline_operator(msg), True
        if not accepted:
            return

        # AstrBot exposes successfully parsed command parameters before plugin dispatch.
        # Let command handlers, including this plugin's commands, keep their normal path.
        try:
            if self._platform.parsed_command_params(event):
                return
        except Exception:
            event.should_call_llm(True)
            event.stop_event()
            yield event.plain_result("无法安全判断消息类型，未创建桥接请求。")
            return

        # AstrBot 4.29.3 enters the default LLM branch when call_llm is false.
        # Keep the event flowing so RespondStage can deliver this handler's result.
        event.should_call_llm(True)

        if offline:
            yield event.plain_result(self._offline_text())
            return
        msg = replace(msg, ref=self._platform.register(event))
        async for result in self._send_batches(event, "handle_private", msg):
            yield result

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=-1,
    )
    async def stbridge_group_message(self, event: AstrMessageEvent):
        """Bridge awakened QQ Official group text through the shared ST chat."""
        if not self._platform.is_qq_official_group_event(event):
            return

        raw_message = event.message_obj.raw_message
        if self._platform.is_own_qq_group_message(event, raw_message):
            event.should_call_llm(True)
            return
        msg = self._platform.incoming(event, text=self._platform.group_message_text(event))
        offline = False
        try:
            await self._client.call("accept_group", {"message": message_to_dict(msg)})
        except _CORE_DOWN:
            offline = True

        if getattr(event, "is_at_or_wake_command", False) is not True:
            return

        try:
            if self._platform.parsed_command_params(event):
                return
        except Exception:
            event.should_call_llm(True)
            yield event.plain_result("无法安全判断消息类型，未创建桥接请求。")
            return

        event.should_call_llm(True)
        if offline:
            yield event.plain_result(self._offline_text())
            return
        msg = replace(msg, ref=self._platform.register(event))
        async for result in self._send_batches(event, "handle_group", msg):
            yield result
