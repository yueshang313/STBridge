"""The bridge request flow: wait for the ST reply, parse it, deliver it."""

import asyncio
import time

from .log import logger
from .platform_api import IncomingMessage, ReplyBatch
from .affinity_store import AffinityUpdate, strip_update_blocks, user_hash
from .emotion_config import OWNER_TRACK, affinity_track, default_affinity_settings
from .http_protocol import BridgeRecoveryGate, DEFAULT_MAX_REQUEST_TEXT_BYTES
from .http_service import HttpServiceState, LocalHttpService
from .request_queue import QueueEntry, QueueFullError, QueueTimeoutError, QueueUnavailableError
from .request_state import BridgeRequest, RequestBusyError, RequestStateManager, RequestStatus
from .reply_texts import default_texts, fill_nickname
from .song_request import strip_sing_tags
from .bridge_types import _ContentTooLongError, _Origin, _ParsedReply, _Speaker, _last_game_open, extract_reply_pairs, merge_pairs, cap_pairs, split_game_block


REQUEST_POLL_INTERVAL_SECONDS = 0.1
# The built-in default lines with the default nickname; the live lines come from the `texts` config
# (see SpeakerMixin._reply_text). Kept as names for the tests and for main's re-exports.
_DEFAULT_TEXTS = default_texts()
_DEFAULT_NICKNAME = default_affinity_settings()["display_name"]
INVALID_REPLY_TEXT = fill_nickname(_DEFAULT_TEXTS["invalid_reply"], _DEFAULT_NICKNAME)
TOO_LONG_TEXT = fill_nickname(_DEFAULT_TEXTS["too_long"], _DEFAULT_NICKNAME)
UNAVAILABLE_TEXT = fill_nickname(_DEFAULT_TEXTS["unavailable"], _DEFAULT_NICKNAME)
QUEUE_FULL_TEXT = fill_nickname(_DEFAULT_TEXTS["queue_full"], _DEFAULT_NICKNAME)
QUEUE_TIMEOUT_TEXT = fill_nickname(_DEFAULT_TEXTS["queue_timeout"], _DEFAULT_NICKNAME)


class BridgeMixin:
    """The bridge request flow: wait for the ST reply, parse it, deliver it."""

    @staticmethod
    def _fail_request_if_active(
        state_manager: RequestStateManager,
        request_id: str,
    ) -> None:
        try:
            state_manager.fail_request(request_id, "extension_error")
        except Exception:
            # Terminal and expired requests must keep their existing state.
            pass

    @staticmethod
    def _reply_failure_code(
        state_manager: RequestStateManager,
        request: BridgeRequest,
        service: LocalHttpService,
        gate: BridgeRecoveryGate,
    ) -> str | None:
        """Why a request ST was asked for ended without a reply, as a ReplyStats code (H1).

        None: the model is not to blame and nothing is recorded: the bridge was stopped or protected, or ST never
        took the request (an ST that is offline is shown by its own node)."""
        if service.state is not HttpServiceState.RUNNING or gate.allows_business_requests() is not True:
            return None
        try:
            current = state_manager.get_request(request.request_id)
        except Exception:
            return None
        if current.status is RequestStatus.SUCCEEDED:
            return "EmptyReply"
        if current.claim_attempt_id is None:
            return None
        return "Timeout" if current.status is RequestStatus.EXPIRED else "StFailed"

    def _record_reply_result(self, code: str | None, started: float) -> None:
        """Count one finished request (never raises; the code None records nothing)."""
        stats = getattr(self, "_reply_stats", None)
        if code is None or stats is None:
            return
        try:
            stats.record(code, int((time.monotonic() - started) * 1000))
        except Exception:
            pass

    async def _wait_for_reply(
        self,
        state_manager: RequestStateManager,
        request: BridgeRequest,
        service: LocalHttpService,
        gate: BridgeRecoveryGate,
    ) -> tuple[bool, str]:
        """Return (True, ST reply) or (False, a Chinese notice for the QQ user)."""
        while True:
            current = state_manager.get_request(request.request_id)
            if current.status is RequestStatus.SUCCEEDED:
                if current.reply:
                    return True, current.reply
                return False, "ST 已完成请求，但没有可用的文本回复。"
            if current.status is RequestStatus.EXPIRED:
                return False, "请求已超时；如果 ST 仍在生成，请等待生成明确结束后再试。"
            if current.status in {RequestStatus.REJECTED, RequestStatus.FAILED}:
                return False, "ST 未能完成本次请求；请确认 ST 状态后再试。"

            if (
                service.state is not HttpServiceState.RUNNING
                or gate.allows_business_requests() is not True
            ):
                self._fail_request_if_active(state_manager, request.request_id)
                return False, "桥接服务已停止或进入保护状态，本次请求未完成。"

            remaining = request.expires_at - time.monotonic()
            if remaining <= 0:
                state_manager.expire_due_request()
                current = state_manager.get_request(request.request_id)
                if current.status is RequestStatus.EXPIRED:
                    return False, "请求已超时；如果 ST 仍在生成，请等待生成明确结束后再试。"
                self._fail_request_if_active(state_manager, request.request_id)
                return False, "请求等待超时，本次未返回 ST 回复。"

            await asyncio.sleep(min(REQUEST_POLL_INTERVAL_SECONDS, remaining))

    async def _process_bridge_content(
        self,
        msg: IncomingMessage,
        content: object,
        origin: _Origin | None = None,
    ):
        if not isinstance(content, str) or not content.strip():
            yield ReplyBatch.of_text("仅支持非空纯文本消息。")
            return

        content = content.strip()
        try:
            content.encode("utf-8")
        except UnicodeEncodeError:
            yield ReplyBatch.of_text("消息文本格式无效，未创建请求。")
            return
        probe = msg.probe
        if not self._content_fits(origin, [content]):
            if probe is not None:
                probe.fail("TooLong")
            yield ReplyBatch.of_text(self._reply_text("too_long"))
            return

        state_manager = self._request_state_manager
        service = self._http_service
        gate = self._recovery_gate
        if (
            state_manager is None
            or service is None
            or service.state is not HttpServiceState.RUNNING
            or gate is None
            or gate.allows_business_requests() is not True
        ):
            if probe is not None:
                probe.fail("StUnavailable")
            yield ReplyBatch.of_text(self._reply_text("unavailable"))
            return

        queue = self._queue
        # A test chat never merges into (or is merged with) a real user's waiting turn.
        key = origin.key if origin is not None and probe is None else None
        hashed = user_hash(key) if key else "none"
        waiting = queue.waiting_entry(key)
        if waiting is not None:
            if not self._content_fits(origin, [*waiting.texts, content]):
                yield ReplyBatch.of_text(self._reply_text("too_long"))
                return
            # The user's earlier, still-waiting handler sends one reply for both.
            waiting.texts.append(content)
            logger.info(
                f"[STBridge] Queue merged user={hashed} pos={queue.position(waiting)} "
                f"messages={len(waiting.texts)}"
            )
            return

        settings = self._queue_settings()
        try:
            entry = queue.enqueue(key, content, max_waiting=settings["max_waiting"])
        except QueueFullError:
            logger.warning(f"[STBridge] Queue full user={hashed} waiting={queue.waiting_count}")
            if probe is not None:
                probe.fail("QueueFull")
            yield ReplyBatch.of_text(self._reply_text("queue_full"))
            return
        except QueueUnavailableError:
            if probe is not None:
                probe.fail("QueueUnavailable")
            yield ReplyBatch.of_text(self._reply_text("unavailable"))
            return
        # Only turns that really wait are logged; immediate ones keep the log quiet.
        if entry.waited:
            logger.info(f"[STBridge] Queue enqueued user={hashed} pos={queue.position(entry)}")
        mention = (
            f"@{origin.mention_name} "
            if entry.waited
            and origin is not None
            and origin.group_label is not None
            and origin.mention_name
            else ""
        )

        def dispatch(current: QueueEntry):
            # Affinity and the group prefix are read now, not on receipt.
            speaker = self._origin_speaker(origin)
            request_content = self._compose_content(origin, speaker, "\n".join(current.texts), self._group_role_tag())
            if len(request_content.encode("utf-8")) > DEFAULT_MAX_REQUEST_TEXT_BYTES:
                raise _ContentTooLongError
            try:
                request = state_manager.create_request(
                    request_content,
                    timeout_seconds=settings["reply_timeout_seconds"],
                    affinity_writes=speaker.writes if speaker is not None else None,
                    macros=self._turn_macros(speaker, origin),
                )
            except RequestBusyError:
                # Includes an unconfirmed earlier generation: keep waiting at the head.
                return None
            return request, speaker

        def available() -> bool:
            return (
                service.state is HttpServiceState.RUNNING
                and gate.allows_business_requests() is True
            )

        try:
            request, speaker = await queue.wait_turn(
                entry,
                max_wait_seconds=settings["max_wait_seconds"],
                dispatch=dispatch,
                available=available,
            )
        except QueueTimeoutError:
            logger.warning(
                f"[STBridge] Queue wait timed out user={hashed} "
                f"waited={queue.waited_seconds(entry):.1f}s"
            )
            if probe is not None:
                probe.fail("QueueTimeout")
            yield ReplyBatch.of_text(f"{mention}{self._reply_text("queue_timeout")}")
            return
        except QueueUnavailableError:
            logger.warning(f"[STBridge] Queue wait ended (BridgeUnavailable) user={hashed}")
            if probe is not None:
                probe.fail("QueueUnavailable")
            yield ReplyBatch.of_text(f"{mention}{self._reply_text("unavailable")}")
            return
        except _ContentTooLongError:
            if probe is not None:
                probe.fail("TooLong")
            yield ReplyBatch.of_text(f"{mention}{self._reply_text("too_long")}")
            return
        except ValueError:
            if probe is not None:
                probe.fail("InvalidMessage")
            yield ReplyBatch.of_text(f"{mention}消息无效，未创建桥接请求。")
            return
        except Exception:
            if probe is not None:
                probe.fail("CreateFailed")
            yield ReplyBatch.of_text(f"{mention}桥接请求创建失败，请稍后重试。")
            return
        try:
            if entry.waited or entry.blocked:
                logger.info(
                    f"[STBridge] Queue dispatched user={hashed} "
                    f"waited={queue.waited_seconds(entry):.1f}s"
                )
            delivery = self._deliver_reply(
                msg, state_manager, request, service, gate, speaker, mention,
            )
            try:
                async for batch in delivery:
                    yield batch
            finally:
                # Closed right away (not when it is garbage collected): a connection that went away must not
                # keep the queue slot.
                await delivery.aclose()
        finally:
            # Held until the reply went out (the platform resumes the handler after sending),
            # so replies keep arrival order.
            queue.release(entry)

    def _parse_reply(self, reply: str, track: str) -> "_ParsedReply | None":
        """Shared reply processing for QQ turns and scheduled turns; None: not a valid bilingual reply."""
        # MVU blocks go first: their analysis text may contain '"' that would break the bilingual check.
        # Only the speaker's own track is read; the other track's commands are ignored.
        affinity_settings_now = self._affinity_settings()
        affinity_character = affinity_settings_now["character"]
        affinity_target = ".".join(
            affinity_track(affinity_settings_now, track)["path"]
        )
        game = split_game_block(reply)
        if game is None:
            without_updates, affinity_update = strip_update_blocks(
                reply, affinity_character, affinity_target
            )
        else:
            # Reasoning before <game> never takes part; MVU blocks may sit in the body or after it.
            body, tail = game
            logger.info(
                f"[STBridge] Reply game block found (thinking_chars={_last_game_open(reply).start()})"
            )
            without_updates, body_update = strip_update_blocks(
                body, affinity_character, affinity_target
            )
            _, tail_update = strip_update_blocks(tail, affinity_character, affinity_target)
            operations = tuple(
                operation
                for update in (body_update, tail_update)
                if update is not None
                for operation in update.operations
            )
            affinity_update = AffinityUpdate(operations) if operations else None
        # Before the bilingual split, so the tag leaves both the Japanese (TTS) and the Chinese text.
        # The emotion tags stay in for now: their positions say which group each one belongs to.
        cleaned_reply, sing_name = strip_sing_tags(without_updates)
        extracted = extract_reply_pairs(cleaned_reply)
        if extracted is None:
            logger.warning("[STBridge] Reply rejected (InvalidBilingualReply)")
            return None
        pairs, emotion_tags = extracted
        japanese_text, chinese_text, _ = merge_pairs(pairs)
        return _ParsedReply(japanese_text, chinese_text, emotion_tags, affinity_update, sing_name, pairs)

    async def _deliver_reply(
        self,
        msg: IncomingMessage,
        state_manager: RequestStateManager,
        request: BridgeRequest,
        service: LocalHttpService,
        gate: BridgeRecoveryGate,
        speaker: _Speaker | None,
        mention: str,
    ):
        probe = msg.probe
        started = time.monotonic()
        try:
            ok, reply = await self._wait_for_reply(state_manager, request, service, gate)
        except asyncio.CancelledError:
            self._fail_request_if_active(state_manager, request.request_id)
            raise
        except Exception:
            self._fail_request_if_active(state_manager, request.request_id)
            ok, reply = False, "桥接请求未能完成；请确认 ST 状态后再试。"

        # Failure notices go to QQ as plain text and never reach TTS.
        if not ok:
            if probe is not None:
                probe.fail("StFailed")
            else:
                self._record_reply_result(self._reply_failure_code(state_manager, request, service, gate), started)
            yield ReplyBatch.of_text(f"{mention}{reply}")
            return

        if probe is not None:
            probe.raw = reply
        parsed = self._parse_reply(reply, speaker.track if speaker is not None else OWNER_TRACK)
        if parsed is None:
            if probe is not None:
                probe.fail("InvalidReply")
            else:
                self._record_reply_result("InvalidReply", started)
            yield ReplyBatch.of_text(f"{mention}{self._reply_text("invalid_reply")}")
            return
        if probe is None:
            self._record_reply_result("ok", started)
            self._count_reply(msg)
            self._record_mood(parsed, owner=(speaker.track if speaker is not None else OWNER_TRACK) == OWNER_TRACK)
            # What the character said last in this session: proactive messages compare theirs with it.
            self._remember_reply(msg.conversation_id, parsed.chinese_text)
        else:
            # A test chat only reports what was parsed.
            probe.emotion_tags = list(parsed.emotion_tags)
            probe.sing = parsed.sing_name
            update = parsed.affinity_update
            if update is not None:
                before = speaker.value if speaker is not None else None
                probe.affinity_change = {
                    "operations": [[operation, number] for operation, number in update.operations],
                    "delta": update.resolve(before) if before is not None else None,
                }
        japanese_text, chinese_text = parsed.japanese_text, parsed.chinese_text
        emotion_tags, affinity_update = parsed.emotion_tags, parsed.affinity_update

        split_on, split_interval, split_max = self._split_settings()
        groups = cap_pairs(parsed.pairs, split_max) if split_on and len(parsed.pairs) > 1 else None
        if len(parsed.pairs) > 1:
            logger.info(
                f"[STBridge] Reply pairs={len(parsed.pairs)} mode={'split' if split_on else 'merged'}"
                + (f" groups={len(groups)}" if groups is not None else "")
            )
        if groups is not None and len(groups) == 1:
            # Capped down to one group: the same as a merged reply.
            groups = None

        sticker_eligible = False
        tts_profile = None
        sticker_tags = emotion_tags
        if groups is not None:
            # The sticker follows the last group's mood.
            last_emotion = groups[-1][2]
            sticker_tags = [last_emotion] if last_emotion is not None else []
        try:
            completed = state_manager.get_request(request.request_id)
            sticker_eligible = (
                probe is None
                and completed.status is RequestStatus.SUCCEEDED
                and bool(completed.reply)
                and completed.reply == reply
            )
            if sticker_eligible and groups is None:
                tts_profile = self._select_tts_profile(emotion_tags)
        except Exception:
            # TTS eligibility is optional and must never block the text reply.
            pass

        if probe is None:
            self._apply_affinity(speaker, affinity_update)
        # Decided (and, when the platform can pre-upload, uploading) while the voice is synthesized below.
        sticker_plan = await self._prepare_sticker(msg, sticker_tags) if sticker_eligible else None
        # A test chat never starts the song flow (the tag is only reported).
        sing_name, sing_preset = (parsed.sing_name if probe is None else None), None
        if sing_name:
            sing_preset, sing_name = self._tag_song_request(msg, request, sing_name)
        song_search = self._start_song_tag_search(speaker, sing_name) if probe is None else None
        if groups is None:
            # The TTS text (japanese_text) stays without the "@name " prefix.
            reply_text = f"{mention}{chinese_text}"
            voiced = None
            if sticker_eligible:
                voiced = await self._voice_reply(reply_text, japanese_text, tts_profile, msg.session_id)
            yield ReplyBatch(tuple(voiced)) if voiced else ReplyBatch.of_text(reply_text)
        else:
            async for batch in self._deliver_groups(msg, groups, mention, sticker_eligible, split_interval):
                yield batch
        # The platform resumes here after voice and text went out, still inside the queue slot.
        if sticker_plan is not None:
            await self._finish_sticker(msg, sticker_plan)
        if song_search is not None and speaker is not None and sing_name:
            await self._finish_song_tag(msg, speaker, sing_name, song_search, sing_preset)

    async def _deliver_groups(
        self,
        msg: IncomingMessage,
        groups: list,
        mention: str,
        voiced: bool,
        interval: float,
    ):
        """Split mode: one batch per group, each with its own voice.

        Probability and cooldown are rolled once for the whole reply. The next group's voice is
        synthesized while the previous group is being sent (never while the previous one is still being
        synthesized), so the gap is the longer of ``interval``
        and what is left of the synthesis.
        """
        allowed = voiced and self._voice_gate(msg.session_id) is None

        async def voice(index: int):
            japanese, chinese, emotion = groups[index]
            text = f"{mention}{chinese}" if index == 0 else chinese
            profile = self._select_tts_profile([emotion] if emotion is not None else [])
            return await self._voice_reply(text, japanese, profile, msg.session_id, gated=True)

        tasks: dict[int, asyncio.Task] = {}
        try:
            if allowed:
                tasks[0] = asyncio.ensure_future(voice(0))
            for index, (_, chinese, _) in enumerate(groups):
                segments = None
                if index == 0:
                    if allowed:
                        segments = await tasks[0]
                elif allowed:
                    segments, _ = await asyncio.gather(tasks[index], self._split_pause(interval))
                else:
                    await self._split_pause(interval)
                # Only now, with this group's synthesis done: at most one synthesis runs at a time
                # (a GPT-SoVITS model switch or Irodori's memory must not be shared), and the next one
                # runs while this group is being sent.
                if allowed and index + 1 < len(groups):
                    tasks[index + 1] = asyncio.ensure_future(voice(index + 1))
                if segments:
                    yield ReplyBatch(tuple(segments))
                else:
                    yield ReplyBatch.of_text(f"{mention}{chinese}" if index == 0 else chinese)
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()

    def accept_private(self, msg: IncomingMessage) -> bool:
        """Is this private message the operator's (the only one bridged)? Remembers its conversation for schedules."""
        # The owner-claim code (private chat only) is the one message a non-owner may send; handle_private answers it.
        if self._claim_try(msg):
            return True
        if not self._is_operator_private_message(msg):
            return False
        self._record_schedule_target(msg, group=False)
        return True

    def accept_group(self, msg: IncomingMessage) -> None:
        """Remember the group's conversation as a place scheduled messages can go."""
        self._record_schedule_target(msg, group=True)

    async def handle_private(self, msg: IncomingMessage):
        """Bridge an accepted private message: the batches to send, in order."""
        claim = self._claim_take(msg)
        if claim is not None:
            # An owner claim: a fixed answer, nothing goes to ST, the queue, the counters or the activity clock.
            async for batch in self._apply_claim(msg, claim):
                yield batch
            return
        async for batch in self._counted_handler(msg, self._handle_private(msg)):
            yield batch

    async def _counted_handler(self, msg: IncomingMessage, handler):
        """Run a message handler while the session counts as "being sent to"; when it ends (the last batch has
        gone out) the session counts as active just now. Proactive messages keep out of the way of both."""
        if msg.probe is not None:
            # A test chat is no activity of any real session.
            try:
                async for batch in handler:
                    yield batch
            finally:
                await handler.aclose()
            return
        conversation = msg.conversation_id
        self._sending_begin(conversation)
        try:
            async for batch in handler:
                yield batch
        finally:
            try:
                await handler.aclose()
            finally:
                self._sending_end(conversation)
                self._touch_conversation(conversation, chat=True)

    async def _handle_private(self, msg: IncomingMessage):
        content = msg.text
        origin = self._message_origin(msg)
        # A test chat is plain chat: the song flow does not see it.
        replies = await self._song_intercept(msg, content, origin) if msg.probe is None else None
        if replies is not None:
            for text in replies:
                yield ReplyBatch.of_text(text)
            return

        processing = self._process_bridge_content(msg, content, origin)
        try:
            async for batch in processing:
                yield batch
        finally:
            await processing.aclose()

    async def handle_group(self, msg: IncomingMessage):
        """Bridge an accepted (awakened) group message: the batches to send, in order."""
        async for batch in self._counted_handler(msg, self._handle_group(msg)):
            yield batch

    async def _handle_group(self, msg: IncomingMessage):
        content = msg.text
        if isinstance(content, str) and content.strip():
            sender_id = msg.sender_id
            if not isinstance(sender_id, str) or not sender_id.strip():
                logger.warning(
                    "[STBridge] Group message rejected (SenderIdentityUnavailable)",
                )
                return

            sender_name = msg.sender_name
            speaker_label = self._group_speaker_label(sender_id, sender_name)
            group_speaker = self._group_speakers[sender_id]
            origin = _Origin(
                msg.platform_id,
                sender_id,
                sender_name,
                group_label=speaker_label,
                mention_name=group_speaker.display_name or group_speaker.fallback_label or "",
            )
        else:
            origin = None

        replies = await self._song_intercept(msg, content, origin) if msg.probe is None else None
        if replies is not None:
            for text in replies:
                yield ReplyBatch.of_text(text)
            return

        processing = self._process_bridge_content(msg, content, origin)
        try:
            async for batch in processing:
                yield batch
        finally:
            await processing.aclose()
