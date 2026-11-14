"""The platform-independent bridge core: one object that owns all state and services.

A platform adapter creates it with a PlatformAPI, feeds it IncomingMessage values and sends the
ReplyBatch values it yields. The mixins hold the behavior; this class only owns the state.
"""

import asyncio
import random
import time
import weakref
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .affinity_store import AffinityStore
from .bridge_types import _console_error_type
from .chat_platform import NAPCAT, NAPCAT_PLATFORM_ID, chat_platform_settings
from .console_server import ConsoleService
from .cover_service import CoverService
from .daily_counts import DailyCounts
from .emotion_config import ConfigStore
from .http_protocol import BridgeRecoveryGate
from .http_service import LocalHttpService
from .log import logger
from .mood_store import MoodStore
from .owner_claim import OwnerClaim
from .owner_config import owner_settings
from .platform_api import IncomingMessage, PlatformAPI, ReplyBatch
from .reply_stats import ReplyStats
from .request_state import RequestStateManager
from .proactive_chat import ProactiveChatScheduler
from .schedule_service import ProactiveState, ScheduleScheduler, ScheduleTargets
from .song_quota import SongQuota
from .sticker_store import StickerStore
from .storage_cleanup import CleanupService
from .tts_coordinator import TtsCoordinator
from .bridge_types import _GroupSpeaker, _PendingSongList
from .mixin_bridge import BridgeMixin
from .mixin_lifecycle import LifecycleMixin
from .mixin_owner import OwnerMixin
from .mixin_schedule import ScheduleMixin
from .mixin_song import SongMixin
from .mixin_speaker import SpeakerMixin
from .mixin_sticker import StickerMixin
from .mixin_testchat import ChatTestMixin
from .mixin_tts import TtsMixin


class BridgeCore(
    LifecycleMixin,
    SpeakerMixin,
    BridgeMixin,
    OwnerMixin,
    SongMixin,
    ScheduleMixin,
    StickerMixin,
    TtsMixin,
    ChatTestMixin,
):
    """The bridge between a chat platform and SillyTavern."""

    def __init__(
        self,
        platform: PlatformAPI,
        data_dir: Callable[[], Path],
        operator_platform_id: str = "",
        operator_sender_id: str = "",
    ) -> None:
        """``data_dir`` is called when the stores are loaded (at start), not here."""
        self._platform = platform
        self._data_dir = data_dir
        # Used until the console config holds an owner (tests, and a core without a readable config).
        self._operator_seed = (operator_platform_id, operator_sender_id)
        self._console_service: ConsoleService | None = None
        self._emotion_store: ConfigStore | None = None
        self._affinity_store: AffinityStore | None = None
        self._mood_store: MoodStore | None = None
        self._tts: TtsCoordinator | None = None
        self._tts_session_state: dict[str, float] = {}
        self._sticker_store: StickerStore | None = None
        self._sticker_session_state: dict[str, float] = {}
        # Injectable for tests.
        self._sticker_random = random.random
        self._sticker_clock = time.monotonic
        self._cover: CoverService | None = None
        self._cleanup: CleanupService | None = None
        self._schedule_targets: ScheduleTargets | None = None
        self._scheduler: ScheduleScheduler | None = None
        self._chat_scheduler: ProactiveChatScheduler | None = None
        self._proactive_state: ProactiveState | None = None
        # Conversations a reply / proactive message / song delivery is being sent to (count per session).
        self._sending: dict[str, int] = {}
        # Injectable for tests.
        self._schedule_random = random.random
        self._schedule_choice = random.choice
        self._schedule_now = datetime.now
        self._song_pending: dict[tuple[str, str], _PendingSongList] = {}
        self._song_tasks: dict[str, asyncio.Task] = {}
        # Injectable for tests.
        self._song_random = random.random
        self._song_choice = random.choice
        self._song_clock = time.monotonic
        self._song_quota: SongQuota | None = None
        # What happened today (replies sent, songs sung) for the console overview.
        self._daily_counts: DailyCounts | None = None
        self._song_now = datetime.now
        # Set by whoever serves connectors (the hub); the console overview shows it.
        self.connector_status: Callable[[], dict] | None = None
        # Set by the runner: NapCat's connection state plus the URL / token to enter in NapCat (the console shows it).
        self.onebot_info: Callable[[], dict] | None = None
        # Set by the runner: stops the core with the restart exit code; returns {"auto": bool} (does start.bat restart it?).
        self.request_restart: Callable[[], dict] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._http_service: LocalHttpService | None = None
        self._recovery_gate: BridgeRecoveryGate | None = None
        self._request_state_manager: RequestStateManager | None = None
        self._queue = self._new_queue()
        # How the last requests to ST ended (memory only; the console health chain shows it).
        self._reply_stats = ReplyStats()
        # The owner claim (console button + a private message with the 6 digits); memory only.
        self._owner_claim = OwnerClaim()
        self._claim_pending: dict[str, dict] = {}
        self._group_speakers: dict[str, _GroupSpeaker] = {}
        # Per list-search task, the reasons a "[唱歌:…]" search failed (weak keys: a cancelled reply cannot leak one).
        self._song_tag_errors: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = weakref.WeakKeyDictionary()
        self._group_name_slot_counts: dict[str, int] = {}
        self._next_group_speaker_order = 1
        self._next_group_fallback_index = 1

    async def terminate(self) -> None:
        await self._terminate_impl()

    # ---- the chat platform in use (AstrBot's connector or the built-in NapCat) ----
    def chat_platform_settings(self) -> dict:
        store = self._emotion_store
        return chat_platform_settings(store.config if store is not None else {})

    def active_platform(self) -> str:
        """"astrbot" or "napcat": the one platform whose messages are handled (read live, a switch is immediate)."""
        return self.chat_platform_settings()["active"]

    def _restart_view(self) -> dict | None:
        """Ask the process to restart (console button); None when this core has no one to do it."""
        hook = self.request_restart
        return hook() if hook is not None else None

    def _onebot_info_view(self) -> dict:
        provider = self.onebot_info
        return provider() if provider is not None else {}

    # ---- the owner (console config; the first connector's own settings are imported once) ----
    def _operator(self) -> tuple[str, str]:
        """The owner on the active platform; on NapCat the QQ number kept in chat_platform."""
        if self.active_platform() == NAPCAT:
            number = self.chat_platform_settings()["napcat_owner"]
            return (NAPCAT_PLATFORM_ID, number) if number else ("", "")
        return self._astrbot_operator()

    def _astrbot_operator(self) -> tuple[str, str]:
        store = self._emotion_store
        if store is not None:
            owner = owner_settings(store.config)
            if owner["platform_id"] and owner["sender_id"]:
                return owner["platform_id"], owner["sender_id"]
        return self._operator_seed

    @property
    def _operator_platform_id(self) -> str:
        return self._operator()[0]

    @property
    def _operator_sender_id(self) -> str:
        return self._operator()[1]

    async def import_operator(self, platform_id: object, sender_id: object) -> None:
        """Take the owner from a connector's own settings, once; after that only the console changes it."""
        if not isinstance(platform_id, str) or not isinstance(sender_id, str):
            return
        platform_id, sender_id = platform_id.strip(), sender_id.strip()
        if not platform_id or not sender_id:
            return
        store = self._emotion_store
        if store is None:
            self._operator_seed = (platform_id, sender_id)
            return
        owner = owner_settings(store.config)
        if owner["imported"] or (owner["platform_id"] and owner["sender_id"]):
            return
        try:
            await store.update(
                lambda config: config.__setitem__(
                    "owner", {"platform_id": platform_id, "sender_id": sender_id, "imported": True}
                )
            )
        except Exception as error:
            logger.error(f"[STBridge] Owner import failed ({_console_error_type(error)})")
            return
        logger.info("[STBridge] Owner imported from the connector's settings (once)")

    def connector_cache(self) -> dict:
        """What a connector may keep for the time the core is away: the "unavailable" line and the owner."""
        platform_id, sender_id = self._astrbot_operator()
        return {
            "unavailable_text": self._reply_text("unavailable"),
            "operator": {"platform_id": platform_id, "sender_id": sender_id},
        }

    def _connector_status_view(self) -> dict:
        provider = self.connector_status
        if provider is None:
            return {"connected": False, "name": None, "version": None, "since": None}
        return provider()

    async def handle_command(self, msg: IncomingMessage):
        """Run a command (``msg.command``): the batches to send, in order; nothing for an unknown command."""
        handler = {
            "affinity": self._cmd_affinity,
            "affinity_set": self._cmd_affinity_set,
            "recover": self._cmd_recover,
            "sing": self._cmd_sing,
        }.get(msg.command or "")
        if handler is None:
            return
        async for batch in handler(msg):
            yield batch


__all__ = ["BridgeCore", "IncomingMessage", "ReplyBatch"]
