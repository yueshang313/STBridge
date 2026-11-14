"""Plugin start/stop, service setup and the queue/console wiring."""

from pathlib import Path

from .log import logger
from .platform_api import PRIVATE, IncomingMessage, ReplyBatch
from .affinity_store import AffinityStore, user_hash
from .mood_store import MoodStore
from .console_server import ConsoleService, create_console_app
from .cover_config import cover_settings
from .cover_service import CoverService
from .daily_counts import DailyCounts
from .emotion_config import OWNER_TRACK, ConfigStore, default_queue_settings, queue_settings, tts_settings
from .http_protocol import BridgeRecoveryGate, HttpProtocolConfig, bridge_runtime_status, create_app
from .http_service import HttpServiceState, LocalHttpService
from .install_service import InstallService
from .network_config import default_network_settings, network_settings
from .request_queue import BridgeQueue, QueueEntry
from .request_state import RequestStateManager
from .proactive_chat import ProactiveChatScheduler
from .schedule_service import ProactiveState, ScheduleScheduler, ScheduleState, ScheduleTargets
from .sticker_store import StickerStore
from .storage_cleanup import CleanupService, cleanup_settings
from .tts_coordinator import TtsCoordinator
from .bridge_types import REQUEST_TIMEOUT_SECONDS, _console_error_type
from .emotion_config import QUEUE_HOLD_EXTRA_SECONDS


ST_ORIGIN = "http://localhost:11451"
HTTP_LISTEN_HOST = "127.0.0.1"
HTTP_LISTEN_PORT = 11452
# A dispatched turn may hold the queue slot this long before the next waiter frees it.
# (The default; the live limit is the configured reply time limit + QUEUE_HOLD_EXTRA_SECONDS.)
QUEUE_ACTIVE_HOLD_LIMIT_SECONDS = REQUEST_TIMEOUT_SECONDS + QUEUE_HOLD_EXTRA_SECONDS


class LifecycleMixin:
    """Plugin start/stop, service setup and the queue/console wiring."""

    # The ports this run listens on (read from the network settings when the plugin started).
    _network_in_use: dict | None = None
    _install_service: InstallService | None = None

    async def initialize(self) -> None:
        async with self._lifecycle_lock:
            if self._http_service is not None:
                await self._http_service.start()
                return

            # The ports come from the network settings, read once per start (a saved change waits for a reload).
            self._load_emotion_store()
            store = self._emotion_store
            ports = network_settings(store.config) if store is not None else default_network_settings()
            self._network_in_use = ports
            gate = BridgeRecoveryGate()
            state_manager = RequestStateManager(
                timeout_seconds=self._queue_settings()["reply_timeout_seconds"],
            )
            config = HttpProtocolConfig(
                allowed_origin=f"http://localhost:{ports['st_port']}",
                extra_origins=(f"http://127.0.0.1:{ports['st_port']}",),
                listen_host=HTTP_LISTEN_HOST,
                listen_port=ports["bridge_port"],
            )
            app = create_app(
                state_manager,
                config,
                recovery_gate=gate,
                on_client_recovery=self._log_client_recovery,
                injection_provider=self._injection_view,
                preset_backup=self._backup_st_preset,
            )
            service = LocalHttpService(app, config, gate)
            if self._queue.closed:
                self._queue = self._new_queue()
            self._recovery_gate = gate
            self._http_service = service
            self._request_state_manager = state_manager

            try:
                await service.start()
            except BaseException:
                try:
                    await service.stop()
                finally:
                    if service.state is HttpServiceState.STOPPED:
                        self._http_service = None
                        self._recovery_gate = None
                        self._request_state_manager = None
                raise

            self._load_affinity_store()
            self._load_mood_store()
            self._setup_tts()
            self._setup_schedule()
            self._setup_cover()
            self._setup_song_quota()
            self._setup_daily_counts()
            self._setup_cleanup()
            await self._start_console()

    @staticmethod
    def _log_client_recovery() -> None:
        logger.info("[STBridge] Recovery confirmed by ST client")

    def _active_hold_limit(self) -> float:
        """Seconds a dispatched turn may hold the queue slot: the reply time limit plus a margin."""
        try:
            return float(self._queue_settings()["reply_timeout_seconds"]) + QUEUE_HOLD_EXTRA_SECONDS
        except Exception:
            return QUEUE_ACTIVE_HOLD_LIMIT_SECONDS

    def _new_queue(self) -> BridgeQueue:
        return BridgeQueue(
            active_hold_limit=self._active_hold_limit,
            on_force_release=LifecycleMixin._log_queue_force_release,
        )

    @staticmethod
    def _log_queue_force_release(entry: QueueEntry, held: float) -> None:
        hashed = user_hash(entry.speaker_key) if entry.speaker_key else "none"
        logger.warning(
            f"[STBridge] Queue slot force-released user={hashed} held={held:.1f}s"
        )

    def _load_emotion_store(self) -> None:
        """Load the store shared by the console and emotion lookup; None disables both."""
        if self._emotion_store is not None:
            return
        try:
            data_dir = Path(self._data_dir())
            self._emotion_store = ConfigStore(data_dir, logger=logger)
            self._sticker_store = StickerStore(data_dir)
        except Exception as error:
            self._emotion_store = None
            logger.error(
                "[STBridge] Console unavailable "
                f"(ConsoleStartFailed: {_console_error_type(error)})"
            )

    def _setup_tts(self) -> None:
        """Create the TTS coordinator from the console config; failures only disable voice."""
        store = self._emotion_store
        if self._tts is not None or store is None:
            return
        try:
            self._tts = TtsCoordinator(
                tts_settings(store.config),
                data_dir=store.data_dir,
                logger=logger,
            )
        except Exception as error:
            self._tts = None
            logger.error(
                "[STBridge] TTS unavailable "
                f"(TtsSetupFailed: {_console_error_type(error)})"
            )

    def _load_affinity_store(self) -> None:
        """Load per-user affinity data; None disables affinity without affecting the bridge."""
        if self._affinity_store is not None:
            return
        try:
            data_dir = Path(self._data_dir())
            self._affinity_store = AffinityStore(
                data_dir,
                # Only used to migrate an old affinity file, which knows AstrBot's owner alone.
                owner_key=self._astrbot_owner_key(),
                group_initial_value=self._affinity_settings()["group"]["initial_value"],
            )
            if self._affinity_store.migration_pending:
                logger.warning("[STBridge] Affinity migration deferred (OwnerUnknown)")
            elif self._affinity_store.migrated:
                logger.info("[STBridge] Affinity data migrated (group values reset once)")
        except Exception as error:
            self._affinity_store = None
            logger.error(
                "[STBridge] Affinity unavailable "
                f"(AffinityStoreFailed: {_console_error_type(error)})"
            )

    def _load_mood_store(self) -> None:
        """Load the recent moods; None only drops the mood macros, never the bridge."""
        if self._mood_store is not None:
            return
        try:
            store = self._emotion_store
            self._mood_store = MoodStore(store.data_dir if store is not None else Path(self._data_dir()))
        except Exception as error:
            # Only the mood macros are lost, so this is a warning (the affinity / console errors say the rest).
            self._mood_store = None
            logger.warning(f"[STBridge] Mood unavailable (MoodStoreFailed: {_console_error_type(error)})")

    def _setup_daily_counts(self) -> None:
        """Load today's counts; a failure only leaves the overview numbers empty."""
        store = self._emotion_store
        if self._daily_counts is not None or store is None:
            return
        try:
            self._daily_counts = DailyCounts(store.data_dir, clock=lambda: self._schedule_now(), logger=logger)
        except Exception as error:
            self._daily_counts = None
            logger.warning(f"[STBridge] Daily counts unavailable (DailyCountsFailed: {_console_error_type(error)})")

    def _count_reply(self, msg) -> None:
        """A usable reply went to a person (never raises)."""
        counts = self._daily_counts
        if counts is not None:
            try:
                counts.add_reply("group" if msg.chat_type == "group" else "private")
            except Exception:
                pass

    def _count_song(self, track: str) -> None:
        counts = self._daily_counts
        if counts is not None:
            try:
                counts.add_song("owner" if track == OWNER_TRACK else "group")
            except Exception:
                pass

    def _today_overview(self) -> dict:
        """What the console overview shows besides the health chain: today's numbers, the chat scheduler, moods, queue.

        Every part is looked up on its own, so one that is unavailable is None and the others still show."""
        out: dict = {"date": None, "replies": None, "songs": None, "chat": None, "mood": None, "queue": None}
        try:
            counts = self._daily_counts.snapshot() if self._daily_counts is not None else None
            if counts is not None:
                out["date"], out["replies"], out["songs"] = counts["date"], counts["replies"], counts["songs"]
        except Exception:
            pass
        try:
            chat = self._chat_scheduler
            out["chat"] = chat.snapshot() if chat is not None else None
        except Exception:
            pass
        try:
            moods = self._mood_store
            if moods is not None:
                settings = self._mood_settings()
                out["mood"] = {"global": moods.summary("global", settings), "owner": moods.summary("owner", settings)}
        except Exception:
            pass
        try:
            queue = self._queue_status()
            out["queue"] = {"active": bool(queue["active"]), "waiting": len(queue["waiting"])}
        except Exception:
            pass
        return out

    def _queue_settings(self) -> dict:
        store = self._emotion_store
        return queue_settings(store.config) if store is not None else default_queue_settings()

    def _wake_prefixes(self) -> list[str] | None:
        """The platform's command wake prefixes; None when it cannot tell."""
        try:
            return self._platform.wake_prefixes()
        except Exception:
            return None

    def _bridge_status(self) -> dict:
        """Read-only bridge state for the console overview; never raises."""
        offline = {"gate": None, "last_poll_age": None, "active": False, "http_running": False}
        service = self._http_service
        manager = self._request_state_manager
        app = getattr(service, "app", None)
        if service is None or manager is None or app is None or service.state is not HttpServiceState.RUNNING:
            return offline
        try:
            runtime = bridge_runtime_status(app)
            return {
                "gate": runtime["state"],
                "last_poll_age": runtime["last_poll_age"],
                "active": manager.generation_in_progress(),
                "http_running": True,
            }
        except Exception:
            return offline

    def _queue_status(self) -> dict:
        """Console view of the queue: user hashes and wait times, never message text."""
        snapshot = self._queue.snapshot()
        return {
            "active": snapshot["active"],
            "waiting": [
                {
                    "user": user_hash(item["speaker_key"]) if item["speaker_key"] else None,
                    "waited_seconds": item["waited_seconds"],
                    "messages": item["messages"],
                }
                for item in snapshot["waiting"]
            ],
        }

    async def _start_console(self) -> None:
        """Start the emotion console; failures never affect the bridge."""
        store = self._emotion_store
        if self._console_service is not None or store is None:
            return
        # The default port is the service's own default; only a configured port is passed on.
        console_port = (self._network_in_use or default_network_settings())["console_port"]
        console_kwargs = {} if console_port == default_network_settings()["console_port"] else {"port": console_port}
        self._install_service = InstallService(store.data_dir)
        try:
            service = ConsoleService(
                create_console_app(
                    store,
                    logger=logger,
                    affinity_store=self._affinity_store,
                    mood=self._mood_store,
                    queue_status=self._queue_status,
                    bridge_status=self._bridge_status,
                    reply_stats=self._reply_stats.snapshot,
                    today_stats=self._today_overview,
                    wake_prefix=self._wake_prefixes,
                    connector_status=self._connector_status_view,
                    network_in_use=lambda: self._network_in_use,
                    onebot_info=self._onebot_info_view,
                    restart=self._restart_view,
                    install=self._install_service,
                    tts=self._tts,
                    stickers=self._sticker_store,
                    schedule_targets=self._schedule_targets,
                    scheduler=self._scheduler,
                    chat_scheduler=self._chat_scheduler,
                    cover=self._cover,
                    cleanup=self._cleanup,
                    test_chat=self.run_test_chat,
                    owner_claim_start=self.owner_claim_start,
                    owner_claim_status=self.owner_claim_status,
                ),
                **console_kwargs,
            )
            await service.start()
        except Exception as error:
            logger.error(
                "[STBridge] Console unavailable "
                f"(ConsoleStartFailed: {_console_error_type(error)})"
            )
            return
        self._console_service = service

    async def _stop_console(self) -> None:
        install, self._install_service = self._install_service, None
        if install is not None:
            await install.terminate()
        console = self._console_service
        self._console_service = None
        if console is None:
            return
        try:
            await console.stop()
        except Exception as error:
            logger.error(
                "[STBridge] Console stop failed "
                f"(ConsoleStopFailed: {_console_error_type(error)})"
            )

    async def _stop_tts(self) -> None:
        tts, self._tts = self._tts, None
        if tts is None:
            return
        try:
            await tts.terminate()
        except Exception as error:
            logger.error(
                "[STBridge] TTS service stop failed "
                f"(TtsStopFailed: {_console_error_type(error)})"
            )

    async def _terminate_impl(self) -> None:
        # Waiting handlers notice on their next poll and finish without an ST request.
        self._queue.close()
        async with self._lifecycle_lock:
            await self._stop_song_tasks()
            await self._stop_schedule()
            await self._stop_cleanup()
            await self._stop_cover()
            await self._stop_console()
            await self._stop_tts()
            self._emotion_store = None
            self._sticker_store = None
            self._schedule_targets = None
            self._proactive_state = None
            self._affinity_store = None
            self._mood_store = None
            service = self._http_service
            if service is None:
                return

            try:
                await service.stop()
            finally:
                if service.state is HttpServiceState.STOPPED:
                    self._http_service = None
                    self._recovery_gate = None
                    self._request_state_manager = None

    # ---- scheduled (proactive) messages -------------------------------------------------
    def _setup_schedule(self) -> None:
        """Start the scheduler; failures only disable scheduled messages."""
        store = self._emotion_store
        if self._scheduler is not None or store is None:
            return
        try:
            self._schedule_targets = ScheduleTargets(store.data_dir, clock=lambda: self._schedule_now())
            self._proactive_state = ProactiveState(store.data_dir, logger=logger)
            self._scheduler = ScheduleScheduler(
                get_config=lambda: store.config,
                targets=self._schedule_targets,
                state=ScheduleState(store.data_dir),
                runner=self._run_proactive,
                clock=lambda: self._schedule_now(),
                logger=logger,
                busy_reason=self._proactive_busy_reason,
                proactive=self._proactive_state,
            )
            self._scheduler.start()
            self._chat_scheduler = ProactiveChatScheduler(
                get_config=lambda: store.config,
                targets=self._schedule_targets,
                state=self._proactive_state,
                runner=self._run_proactive_chat,
                clock=lambda: self._schedule_now(),
                rng=lambda: self._schedule_random(),
                logger=logger,
                busy_reason=self._proactive_busy_reason,
                t1_blocking=self._scheduler.blocks_chat,
            )
            self._chat_scheduler.start()
        except Exception as error:
            self._scheduler = None
            self._chat_scheduler = None
            self._schedule_targets = None
            logger.error(
                "[STBridge] Schedule unavailable "
                f"(ScheduleSetupFailed: {_console_error_type(error)})"
            )

    def _setup_cover(self) -> None:
        """Create the song cover service; failures only disable covers."""
        store = self._emotion_store
        if self._cover is not None or store is None:
            return
        try:
            self._cover = CoverService(
                data_dir=store.data_dir,
                get_settings=lambda: cover_settings(store.config),
                logger=logger,
            )
            self._cover.start_idle_watch()
        except Exception as error:
            self._cover = None
            logger.error(
                "[STBridge] Cover unavailable "
                f"(CoverSetupFailed: {_console_error_type(error)})"
            )

    def _setup_cleanup(self) -> None:
        """Start the daily temp-file cleanup; failures only disable it."""
        store = self._emotion_store
        if self._cleanup is not None or store is None:
            return
        try:
            def astrbot_temp() -> Path | None:
                try:
                    return self._platform.extra_temp_dir()
                except Exception:
                    return None

            self._cleanup = CleanupService(
                data_dir=store.data_dir,
                get_settings=lambda: cleanup_settings(store.config),
                logger=logger,
                astrbot_temp=astrbot_temp,
                bridge_temp=lambda: store.data_dir / "temp_audio",
                cover_stems=lambda: store.data_dir / "cover_stems",
            )
            self._cleanup.start()
        except Exception as error:
            self._cleanup = None
            logger.error(
                "[STBridge] Cleanup unavailable "
                f"(CleanupSetupFailed: {_console_error_type(error)})"
            )

    async def _stop_cleanup(self) -> None:
        cleanup, self._cleanup = self._cleanup, None
        if cleanup is None:
            return
        try:
            await cleanup.stop()
        except Exception as error:
            logger.error(
                "[STBridge] Cleanup stop failed "
                f"(CleanupStopFailed: {_console_error_type(error)})"
            )

    async def _stop_cover(self) -> None:
        cover, self._cover = self._cover, None
        if cover is None:
            return
        try:
            await cover.terminate()
        except Exception as error:
            logger.error(
                "[STBridge] Cover stop failed "
                f"(CoverStopFailed: {_console_error_type(error)})"
            )

    async def _stop_schedule(self) -> None:
        scheduler, self._scheduler = self._scheduler, None
        chat, self._chat_scheduler = self._chat_scheduler, None
        if chat is not None:
            try:
                await chat.stop()
            except Exception as error:
                logger.error(
                    "[STBridge] Proactive chat stop failed "
                    f"(ChatStopFailed: {_console_error_type(error)})"
                )
        if scheduler is None:
            return
        try:
            await scheduler.stop()
        except Exception as error:
            logger.error(
                "[STBridge] Schedule stop failed "
                f"(ScheduleStopFailed: {_console_error_type(error)})"
            )

    async def _cmd_recover(self, msg: IncomingMessage):
        service = self._http_service
        gate = self._recovery_gate
        authorized = (
            bool(self._operator_platform_id)
            and bool(self._operator_sender_id)
            and service is not None
            and service.state is HttpServiceState.RUNNING
            and gate is not None
        )
        if authorized:
            authorized = (
                msg.chat_type == PRIVATE
                and msg.platform_id == self._operator_platform_id
                and msg.sender_id == self._operator_sender_id
            )

        if authorized and gate.confirm_recovery():
            message = (
                "人工恢复确认已记录；这仅代表操作者确认，"
                "不表示系统独立验证了 ST 生成状态。"
            )
        else:
            message = "恢复命令未执行。"

        yield ReplyBatch.of_text(message)
