"""Chat song requests (点歌): permission, list, pick and delivery."""

import asyncio
import re
import time

from .log import logger
from .platform_api import GROUP, IncomingMessage, ReplyBatch, Segment
from .affinity_store import group_key, user_hash
from .cover_config import cover_settings
from .cover_service import CoverError
from .emotion_config import GROUP_TRACK, OWNER_TRACK, default_affinity_settings
from .music_source import MusicError, fetch_netease_song, resolve_netease_short_link
from .request_state import BridgeRequest
from .song_quota import SongQuota
from .reply_texts import default_texts, fill_limit, fill_nickname, fill_song_name
from .song_request import LIST_SOURCES, build_song_list, default_song_request_settings, match_pitch_preset, parse_netease_link, parse_pick, song_request_settings
from .bridge_types import _Origin, _PendingSongList, _Speaker, _console_error_type, _retrieve_task_exception


SONG_SEARCH_TIMEOUT_SECONDS = 20.0
# A passive delivery shares the picking message with the pipeline that is still sending the accept line
# (the QQ Official reply keeps its message in one per-event buffer); a cached cover must not overtake that.
SONG_PASSIVE_SETTLE_SECONDS = 2.0
# The built-in default lines (the live ones come from the `texts` config via SpeakerMixin._reply_text);
# kept as names for the tests and for main's re-exports.
_DEFAULT_TEXTS = default_texts()
_DEFAULT_NICKNAME = default_affinity_settings()["display_name"]
SONG_BUSY_TEXT = fill_nickname(_DEFAULT_TEXTS["song_busy"], _DEFAULT_NICKNAME)
SONG_QUEUE_FULL_TEXT = fill_nickname(_DEFAULT_TEXTS["song_queue_full"], _DEFAULT_NICKNAME)
SONG_USAGE_TEXT = fill_nickname(_DEFAULT_TEXTS["song_usage"], _DEFAULT_NICKNAME)
SONG_CANCEL_TEXT = fill_nickname(_DEFAULT_TEXTS["song_cancel"], _DEFAULT_NICKNAME)
SONG_RESEARCH_HINT = fill_nickname(_DEFAULT_TEXTS["song_research_hint"], _DEFAULT_NICKNAME)
SONG_RESEARCH_EMPTY_TEXT = fill_nickname(_DEFAULT_TEXTS["song_research_empty"], _DEFAULT_NICKNAME)
SONG_PICK_RANGE_TEXT = fill_nickname(_DEFAULT_TEXTS["song_pick_range"], _DEFAULT_NICKNAME)


class SongMixin:
    """Chat song requests (点歌): permission, list, pick and delivery."""

    # ---- song requests (P7c) ---------------------------------------------------------------
    def _song_settings(self) -> dict:
        store = self._emotion_store
        return song_request_settings(store.config) if store is not None else default_song_request_settings()

    def _song_feature_on(self) -> bool:
        """The master switch is on and the cover service exists and is enabled."""
        store, cover = self._emotion_store, self._cover
        if store is None or cover is None:
            return False
        try:
            return bool(self._song_settings()["enabled"] and cover_settings(store.config)["enabled"])
        except Exception:
            return False

    def _setup_song_quota(self) -> None:
        """Load the daily counts; failures only switch the daily limit off."""
        store = self._emotion_store
        if self._song_quota is not None or store is None:
            return
        try:
            self._song_quota = SongQuota(store.data_dir, clock=lambda: self._song_now(), logger=logger)
        except Exception as error:
            self._song_quota = None
            logger.error(f"[STBridge] Song quota unavailable (SongQuotaFailed: {_console_error_type(error)})")

    def _song_limited(self, track: str, settings: dict | None = None) -> bool:
        """Does the daily limit apply to this track? Group members always, the owner only if switched on."""
        settings = settings or self._song_settings()
        if settings["daily_limit"] <= 0 or self._song_quota is None:
            return False
        return track == GROUP_TRACK or bool(settings["limit_owner"])

    def _song_quota_text(self, key: str, track: str) -> str:
        """"used/limit" for the log, or "off" for somebody the limit does not apply to."""
        settings = self._song_settings()
        if not self._song_limited(track, settings):
            return "off"
        return f"{self._song_quota.used(key)}/{settings['daily_limit']}"

    def _song_check(self, value: int | None, track: str, key: str | None = None) -> str | None:
        """None when this speaker may order a song now, else why not: "quota" or "other".

        Order: feature switch, the affinity threshold of their own track, today's count, the refusal roll.
        An unknown value (affinity store unavailable) passes for the owner and fails for group members.
        """
        if not self._song_feature_on():
            return "other"
        settings = self._song_settings()
        if value is None:
            if track != OWNER_TRACK:
                return "other"
        else:
            floor = settings["owner_min_affinity"] if track == OWNER_TRACK else settings["group_min_affinity"]
            if value < floor:
                return "other"
        if (
            key is not None
            and self._song_limited(track, settings)
            and self._song_quota.used(key) >= settings["daily_limit"]
        ):
            return "quota"
        if self._song_random() * 100.0 < settings["refuse_percent"]:
            return "other"
        return None

    def _song_allowed(self, value: int | None, track: str, key: str | None = None) -> bool:
        return self._song_check(value, track, key) is None

    def _song_verdict(self, speaker: _Speaker) -> str | None:
        """The turn's refusal reason (None = allowed); a speaker made without a decision gets a fresh one."""
        if speaker.sing_allowed is not None:
            return None if speaker.sing_allowed else (speaker.sing_reason or "other")
        return self._song_check(speaker.value, speaker.track, speaker.key)

    def _song_refusal_text(self, reason: str | None, name: str = "") -> str:
        """The line for a refused command / link: the limit line when today's count is used up."""
        if reason == "quota":
            settings = self._song_settings()
            return fill_nickname(fill_limit(settings["limit_text"], settings["daily_limit"]), self._nickname())
        return self._song_text("refuse_texts", name)

    def _song_consume(self, key: str, track: str) -> tuple[bool, str | None]:
        """Count one song for a picked / linked request: (allowed, the date it was counted on or None)."""
        if not self._song_limited(track):
            return True, None
        quota = self._song_quota
        date = quota.today()
        if quota.try_consume(key, self._song_settings()["daily_limit"]):
            return True, date
        return False, None

    def _song_text(self, field: str, name: str = "") -> str:
        return fill_nickname(fill_song_name(self._song_choice(self._song_settings()[field]), name), self._nickname())

    def _song_permission_path(self) -> list[str]:
        """The stat_data path the ST world book reads to learn whether a song request is welcome."""
        return [self._song_settings()["permission_variable"]]

    @staticmethod
    def _song_user_key(origin: _Origin | None) -> str | None:
        """The requester key (the same one the affinity store uses; group members keep their own)."""
        key = origin.key if origin is not None else None
        if key is None:
            return None
        return group_key(key) if origin.group_label is not None else key

    @staticmethod
    def _song_log(
        key: str, via: str, pick: str, result: str, started: float | None = None, mode: str | None = None,
        quota: str | None = None, preset: str | None = None,
    ) -> None:
        total = f" total_ms={int((time.monotonic() - started) * 1000)}" if started is not None else ""
        sending = f" mode={mode}" if mode is not None else ""
        counted = f" quota={quota}" if quota is not None else ""
        chosen = f" preset={preset}" if preset is not None else ""
        try:
            logger.info(
                f"[STBridge] SongRequest user={user_hash(key)} via={via} pick={pick} "
                f"result={result}{sending}{total}{chosen}{counted}"
            )
        except Exception:
            pass

    def _song_skip_log(self, key: str, via: str, reason: str | None, track: str) -> None:
        if reason == "quota":
            self._song_log(key, via, "none", f"skip:QuotaExceeded used={self._song_quota_text(key, track)}")
        else:
            self._song_log(key, via, "none", "skip:NotAllowed")

    def _song_pending_for(self, conversation: str, key: str) -> _PendingSongList | None:
        now = self._song_clock()
        for pending_key in [k for k, item in self._song_pending.items() if item.expires <= now]:
            del self._song_pending[pending_key]
        return self._song_pending.get((conversation, key))

    def _song_register(
        self, conversation: str, key: str, query: str, entries: list, group: bool, via: str, preset: dict | None = None
    ) -> None:
        seconds = self._song_settings()["list_timeout_seconds"]
        self._song_pending[(conversation, key)] = _PendingSongList(
            query, entries, self._song_clock() + seconds, group, via, preset
        )

    def _pitch_presets(self) -> list:
        store = self._emotion_store
        if store is None:
            return []
        try:
            return cover_settings(store.config)["pitch_presets"]
        except Exception:
            return []

    def _match_preset(self, text: object) -> tuple[dict | None, str]:
        """(preset, text without the keyword) for a request text; (None, text) when nothing matches."""
        return match_pitch_preset(text, self._pitch_presets())

    def _tag_song_request(
        self, msg: IncomingMessage, request: BridgeRequest | None, name: str
    ) -> tuple[dict | None, str]:
        """(preset, song name) for a `[唱歌:歌名]` reply: the preset comes from the user's own words.

        The name comes from ST and is only cleaned when it happens to contain a preset keyword (an empty
        result keeps the original name).
        """
        try:
            preset, _ = self._match_preset(self._raw_user_text(msg, request))
            named, cleaned = self._match_preset(name)
            if named is not None:
                if cleaned:
                    name = cleaned
                preset = preset or named
            return preset, name
        except Exception:
            return None, name

    @staticmethod
    def _raw_user_text(msg: IncomingMessage, request: BridgeRequest | None) -> str:
        """What the user actually wrote (no group prefix); the request text is the fallback."""
        text = msg.message_str
        if isinstance(text, str) and text.strip():
            return text
        content = getattr(request, "content", "")
        return content if isinstance(content, str) else ""

    async def _song_list(self, query: str, *, group: bool, extra: str = "") -> tuple[list, str]:
        """Search the library and the sources; (entries, list text), empty when nothing usable came back."""
        cover = self._cover
        if cover is None:
            return [], ""
        settings = self._song_settings()
        library_hits = cover.library_matches(query, extra or None)
        search_name = f"{query} {extra}".strip()

        async def one(source: str) -> tuple[str, list]:
            try:
                return source, (await cover.search_online(search_name, source))["results"]
            except Exception:
                return source, []

        pairs = await asyncio.gather(*(one(source) for source in LIST_SOURCES))
        return build_song_list(
            query,
            library_hits,
            dict(pairs),
            settings["per_source"],
            group=group,
            timeout_seconds=settings["list_timeout_seconds"],
        )

    async def _song_list_guarded(self, query: str, *, group: bool, extra: str = "") -> tuple[list, str]:
        try:
            return await asyncio.wait_for(
                self._song_list(query, group=group, extra=extra), SONG_SEARCH_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return [], ""

    # A: the reply carries [唱歌:歌名]
    def _start_song_tag_search(self, speaker: _Speaker | None, name: str | None) -> asyncio.Task | None:
        """Start the list search next to the reply's voice synthesis; None when no search is wanted."""
        if not name or speaker is None:
            return None
        if speaker.sing_allowed is not True:
            self._song_skip_log(speaker.key, "tag", speaker.sing_reason, speaker.track)
            return None
        try:
            task = asyncio.create_task(self._song_list(name, group=speaker.track == GROUP_TRACK))
            task.add_done_callback(_retrieve_task_exception)
            return task
        except Exception:
            return None

    async def _finish_song_tag(
        self, msg: IncomingMessage, speaker: _Speaker, name: str, task: asyncio.Task, preset: dict | None = None
    ) -> None:
        """After the reply went out: send the numbered list (or the failure line). Never raises."""
        try:
            try:
                entries, text = await asyncio.wait_for(task, SONG_SEARCH_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                entries, text = [], ""
            conversation = msg.conversation_id
            if entries and conversation:
                # A platform send, not another yielded batch: a second batch could be voiced again.
                await self._platform.send(msg.ref, [Segment.of_text(text)])
                self._song_register(conversation, speaker.key, name, entries, speaker.track == GROUP_TRACK, "tag", preset)
            else:
                self._song_log(speaker.key, "tag", "none", "failed:NoResults")
                await self._platform.send(msg.ref, [Segment.of_text(self._song_text("fail_texts", name))])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"[STBridge] SongRequest list failed ({_console_error_type(error)})")

    # picks, links
    def _song_busy(self, key: str) -> bool:
        task = self._song_tasks.get(key)
        return task is not None and not task.done()

    async def _song_intercept(
        self, msg: IncomingMessage, content: object, origin: _Origin | None
    ) -> list[str] | None:
        """Handle a message that belongs to the song flow; the reply texts, or None to bridge it to ST."""
        try:
            if not isinstance(content, str) or origin is None:
                return None
            key = self._song_user_key(origin)
            conversation = msg.conversation_id
            if key is None or not conversation:
                return None
            text = content.strip()
            pending = self._song_pending_for(conversation, key)
            if pending is not None:
                action = parse_pick(text)
                if action is not None and action[0] == "pick" and action[2]:
                    # "2 男声": the words after the number only count when they name a preset;
                    # otherwise this is ordinary chat ("2 那首不错").
                    named, _ = self._match_preset(action[2])
                    action = ("pick", action[1], named) if named is not None else None
                elif action is not None and action[0] == "pick":
                    action = ("pick", action[1], None)
                if action is not None:
                    return await self._song_answer(msg, conversation, key, pending, action)
            if not self._song_feature_on():
                return None
            link = parse_netease_link(text)
            if link is not None:
                return await self._song_link(msg, conversation, key, origin, link, self._match_preset(text)[0])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"[STBridge] SongRequest intercept failed ({_console_error_type(error)})")
        return None

    async def _song_answer(
        self, msg: IncomingMessage, conversation: str, key: str, pending: _PendingSongList, action: tuple
    ) -> list[str]:
        kind = action[0]
        if kind == "cancel":
            self._song_pending.pop((conversation, key), None)
            return [self._reply_text("song_cancel")]
        if kind == "research_empty":
            return [self._reply_text("song_research_hint")]
        if kind == "research":
            # The extra text may name a preset too: it replaces the earlier one and is not searched for.
            named, extra = self._match_preset(action[1])
            preset = named or pending.preset
            entries, text = await self._song_list_guarded(pending.query, group=pending.group, extra=extra)
            if not entries:
                return [self._reply_text("song_research_empty")]
            self._song_register(conversation, key, pending.query, entries, pending.group, pending.via, preset)
            return [text]
        number = action[1]
        if number > len(pending.entries):
            return [self._reply_text("song_pick_range")]
        if self._song_busy(key):
            return [self._reply_text("song_busy")]
        entry = pending.entries[number - 1]
        self._song_pending.pop((conversation, key), None)
        track = GROUP_TRACK if pending.group else OWNER_TRACK
        counted, date = self._song_consume(key, track)
        if not counted:
            self._song_skip_log(key, pending.via, "quota", track)
            return [self._song_refusal_text("quota")]
        self._start_song_delivery(conversation, key, pending.via, entry, msg, track, date, action[2] or pending.preset)
        return [self._song_text("accept_texts", entry["name"])]

    async def _song_link(
        self, msg: IncomingMessage, conversation: str, key: str, origin: _Origin, link: tuple,
        preset: dict | None = None,
    ) -> list[str]:
        """A pasted NetEase song link: no list, the song itself is sung."""
        speaker = self._origin_speaker(origin)
        if speaker is None:
            return []
        reason = self._song_verdict(speaker)
        if reason is not None:
            self._song_skip_log(key, "link", reason, speaker.track)
            return [self._song_refusal_text(reason)]
        started = time.monotonic()
        try:
            song_id = link[1]
            if link[0] == "short":
                resolved = parse_netease_link(await resolve_netease_short_link(link[1]))
                if resolved is None or resolved[0] != "id":
                    raise MusicError("not_a_song")
                song_id = resolved[1]
            try:
                detail = await fetch_netease_song(song_id)
            except MusicError:
                detail = {"name": f"网易云歌曲 {song_id}", "artist": "", "album": ""}
        except asyncio.CancelledError:
            raise
        except Exception:
            self._song_log(key, "link", "wy", "failed:LinkResolve", started)
            return [self._song_text("fail_texts")]
        if self._song_busy(key):
            return [self._reply_text("song_busy")]
        counted, date = self._song_consume(key, speaker.track)
        if not counted:
            self._song_skip_log(key, "link", "quota", speaker.track)
            return [self._song_refusal_text("quota")]
        entry = {
            "kind": "online",
            "source": "wy",
            "source_id": str(song_id),
            "name": detail["name"],
            "artist": detail["artist"],
            "album": detail["album"],
            "label": "网易云",
        }
        self._start_song_delivery(conversation, key, "link", entry, msg, speaker.track, date, preset)
        return [self._song_text("accept_texts", entry["name"])]

    # delivery
    def _start_song_delivery(
        self,
        conversation: str,
        key: str,
        via: str,
        entry: dict,
        msg: IncomingMessage | None = None,
        track: str = OWNER_TRACK,
        counted_on: str | None = None,
        preset: dict | None = None,
    ) -> None:
        """Start the background delivery. ``counted_on``: the date this song was counted on (refunded on failure)."""
        task = asyncio.create_task(self._run_song_delivery(conversation, key, via, entry, msg, track, counted_on, preset))
        self._song_tasks[key] = task
        # A song on its way counts as "being sent to" this session (proactive messages wait).
        self._sending_begin(conversation)

        def done(finished: asyncio.Task) -> None:
            if self._song_tasks.get(key) is finished:
                del self._song_tasks[key]
            self._sending_end(conversation)
            self._touch_conversation(conversation, chat=True)
            _retrieve_task_exception(finished)

        task.add_done_callback(done)

    async def _song_send(
        self, msg: IncomingMessage | None, conversation: str, segments: list[Segment], delivery: dict
    ) -> None:
        """Send one message of a delivery; ``delivery["mode"]`` records how it actually went out.

        proactive: a proactive send, exactly as in P7c. passive: a reply to the message that picked the song,
        which QQ accepts in groups that did not grant proactive messages; only a private
        chat falls back to the proactive send when that fails.
        """
        if delivery["mode"] == "proactive" or msg is None:
            await self._proactive_send_chain(conversation, segments)
            return
        if not delivery.get("settled"):
            delivery["settled"] = True
            await asyncio.sleep(SONG_PASSIVE_SETTLE_SECONDS)
        try:
            await self._platform.send(msg.ref, segments)
        except asyncio.CancelledError:
            raise
        except Exception:
            if msg.chat_type == GROUP:
                raise  # a proactive group message is always refused; the passive window may have passed
            delivery["mode"] = "proactive_fallback"
            await self._proactive_send_chain(conversation, segments)

    def _song_refund(self, key: str, counted_on: str | None) -> None:
        if counted_on is None or self._song_quota is None:
            return
        try:
            self._song_quota.refund(key, counted_on)
        except Exception as error:
            logger.warning(f"[STBridge] Song quota refund failed ({_console_error_type(error)})")

    async def _run_song_delivery(
        self,
        conversation: str,
        key: str,
        via: str,
        entry: dict,
        msg: IncomingMessage | None = None,
        track: str = OWNER_TRACK,
        counted_on: str | None = None,
        preset: dict | None = None,
    ) -> None:
        """Download if needed, sing, and send the voice (see _song_send); failures send a short line."""
        started = time.monotonic()
        pick = "lib" if entry["kind"] == "library" else entry["source"]
        result = "failed:OtherError"
        fail_line = None
        delivery = {"mode": self._song_settings()["delivery_mode"]}
        preset_name = preset["name"] if preset else "none"
        try:
            cover = self._cover
            if cover is None:
                raise CoverError("Unavailable")
            if entry["kind"] == "library":
                song = cover.library.get(entry["song_id"])
                if song is None:
                    raise CoverError("SongNotFound")
            else:
                song = await cover.resolve_online_song(
                    entry["source"], entry["source_id"], entry["name"], entry["artist"]
                )
            sung = await cover.sing(
                song["id"],
                f0_up_key=preset["vocal"] if preset else None,
                inst_pitch=preset["inst"] if preset else None,
            )
            await self._song_send(msg, conversation, [Segment.voice(cover.cover_file(sung))], delivery)
            result = "sent"
            self._count_song(track)
        except asyncio.CancelledError:
            self._song_refund(key, counted_on)
            self._song_log(key, via, pick, "failed:Cancelled", started, delivery["mode"], self._song_quota_text(key, track), preset_name)
            raise
        except Exception as error:
            kind = error.kind if isinstance(error, (CoverError, MusicError)) else type(error).__name__
            result = f"failed:{kind}"
            fail_line = self._reply_text("song_queue_full") if kind == "QueueFull" else self._song_text("fail_texts", entry["name"])
        if result != "sent":
            self._song_refund(key, counted_on)
        self._song_log(key, via, pick, result, started, delivery["mode"], self._song_quota_text(key, track), preset_name)
        if fail_line is not None:
            try:
                await self._song_send(msg, conversation, [Segment.of_text(fail_line)], delivery)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[STBridge] SongRequest failure line not sent ({_console_error_type(error)})")

    async def _stop_song_tasks(self) -> None:
        tasks = list(self._song_tasks.values())
        self._song_tasks.clear()
        self._song_pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # B: /<昵称> 唱 歌名
    def _sing_command_name(self, msg: IncomingMessage) -> str:
        """Everything after the command word (song names often hold spaces).

        The command may start with one of AstrBot's wake prefixes and the character's nickname ("/琴音 唱 歌名");
        the prefixes are tried longest first and default to "/" when AstrBot's setting cannot be read.
        """
        text = msg.message_str
        if not isinstance(text, str):
            return ""
        known = getattr(msg, "wake_prefixes", None)
        if known is None:
            known = self._wake_prefixes()
        prefixes = sorted({prefix for prefix in (known or ["/"]) if prefix}, key=len, reverse=True)
        prefix_part = "(?:(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + r")\s*)?" if prefixes else ""
        pattern = r"^\s*" + prefix_part + "(?:" + re.escape(self._nickname()) + r"\s*)?唱\s*"
        return re.sub(pattern, "", text, count=1).strip()

    async def _cmd_sing(self, msg: IncomingMessage):
        group = msg.chat_type == GROUP
        if not group and not self._is_operator_private_message(msg):
            return
        name = self._sing_command_name(msg)
        # A pitch keyword ("唱 男声 春日影") picks a preset and is not part of the song name.
        preset, name = self._match_preset(name)
        if not name:
            yield ReplyBatch.of_text(self._reply_text("song_usage"))
            return
        speaker = self._message_speaker(msg)
        conversation = msg.conversation_id
        if speaker is None or not conversation:
            return
        reason = self._song_verdict(speaker)
        if reason is not None:
            self._song_skip_log(speaker.key, "command", reason, speaker.track)
            yield ReplyBatch.of_text(self._song_refusal_text(reason, name))
            return
        entries, text = await self._song_list_guarded(name, group=group)
        if not entries:
            self._song_log(speaker.key, "command", "none", "failed:NoResults")
            yield ReplyBatch.of_text(self._song_text("fail_texts", name))
            return
        self._song_register(conversation, speaker.key, name, entries, group, "command", preset)
        yield ReplyBatch.of_text(text)
