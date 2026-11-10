"""Scheduled (proactive) messages."""

import asyncio
import time
import uuid
from datetime import datetime

from .chat_platform import conversation_platform
from .log import logger
from .platform_api import IncomingMessage, Segment
from .audio_format import voice_bytes_for_send
from .emotion_config import OWNER_TRACK
from .http_service import HttpServiceState
from .request_queue import QueueEntry, QueueFullError, QueueTimeoutError, QueueUnavailableError
from .request_state import RequestBusyError
from .proactive_chat import delivered, is_duplicate
from .proactive_chat_config import DUPLICATE_NOTE, ELAPSED_PLACEHOLDER, UNANSWERED_NOTE, proactive_chat_settings
from .schedule_config import schedule_section
from .tts_clients import IrodoriRequestError, ModelSelectionError, safe_error_type
from .tts_service import ServiceStartError
from .bridge_types import REQUEST_TIMEOUT_SECONDS, _ParsedReply, _console_error_type, cap_pairs


class _ProactiveYield(Exception):
    """A proactive turn gave way: someone started talking after it joined the queue."""


class ScheduleMixin:
    """Scheduled (proactive) messages."""

    # ---- what is going on in a session (for the proactive messages to keep out of the way) ----
    def _sending_begin(self, conversation: str | None) -> None:
        if isinstance(conversation, str) and conversation:
            self._sending[conversation] = self._sending.get(conversation, 0) + 1

    def _sending_end(self, conversation: str | None) -> None:
        if isinstance(conversation, str) and conversation and conversation in self._sending:
            left = self._sending[conversation] - 1
            if left > 0:
                self._sending[conversation] = left
            else:
                del self._sending[conversation]

    def _touch_conversation(self, conversation: str | None, *, chat: bool = False, force_save: bool = False) -> None:
        """A reply or proactive message just finished going out: that is activity in the session.

        ``chat``: it was real chat (a reply, a song, a proactive chat message), which also moves the owner's
        chat base; a plan going out is activity only."""
        targets = self._schedule_targets
        if targets is not None and isinstance(conversation, str) and conversation:
            try:
                targets.touch(conversation)
                if chat:
                    targets.touch_chat(conversation, force_save=force_save)
            except Exception:
                pass

    def _remember_reply(self, conversation: str | None, chinese_text: str) -> None:
        targets = self._schedule_targets
        if targets is not None and isinstance(conversation, str) and conversation:
            try:
                targets.set_last_text(conversation, chinese_text)
            except Exception:
                pass

    def _proactive_busy_reason(self, conversation: str) -> str | None:
        """Why a proactive message should wait right now (None: nothing in the way)."""
        if self._queue.busy:
            return "Queue"
        if self._sending.get(conversation):
            return "Sending"
        chat = self._chat_scheduler
        if chat is not None and chat.running:
            return "Chat"
        return None

    def _dedupe_ratio(self) -> float:
        store = self._emotion_store
        return schedule_section(store.config if store is not None else {})["settings"]["dedupe_ratio"]

    def _record_schedule_target(self, msg: IncomingMessage, *, group: bool) -> None:
        """Remember the session of an owner/group message as a place scheduled messages can go."""
        targets = self._schedule_targets
        if targets is None:
            return
        try:
            conversation = msg.conversation_id
            if not isinstance(conversation, str) or not conversation:
                return
            if group:
                targets.record_group(conversation)
            else:
                targets.record_owner(conversation)
        except Exception:
            pass

    @staticmethod
    def _proactive_text(plan: dict, now: datetime, group: bool) -> str:
        weekday = "一二三四五六日"[now.weekday()]
        text = f"（系统提示：现在是 {now:%Y-%m-%d} 星期{weekday} {now:%H:%M}。{plan['prompt']}{DUPLICATE_NOTE}）"
        if group:
            text += "（这条消息会发到群里）"
        return text

    async def _run_proactive_chat(self, forced: bool = False, elapsed_minutes: int = 0, unanswered: int = 0) -> dict:
        """One owner-chat message (3a): the plan is made up from the chat settings, the turn is the same as a plan's."""
        store = self._emotion_store
        settings = proactive_chat_settings(store.config if store is not None else {})
        prompt = settings["prompt"].replace(ELAPSED_PLACEHOLDER, self._elapsed_text(elapsed_minutes))
        if unanswered >= 1:
            prompt += UNANSWERED_NOTE
        plan = {
            "id": "chat",
            "name": "主动搭话",
            "target": {"type": "private"},
            "prompt": prompt,
            "fallback_texts": [],
        }
        return await self._run_proactive(plan, forced, chat=True)

    @staticmethod
    def _elapsed_text(minutes: int) -> str:
        minutes = max(1, int(minutes))
        if minutes < 60:
            return f"{minutes} 分钟"
        if minutes < 24 * 60:
            return f"{minutes // 60} 小时"
        return f"{minutes // (24 * 60)} 天"

    async def _run_proactive(self, plan: dict, forced: bool = False, *, chat: bool = False) -> dict:
        """One scheduled turn: ST writes the message, then voice, text and sticker go out in turn.

        Returns the result fields (also used by the console test button). ``forced`` ignores the
        voice/sticker probability and cooldown (and the give-way and duplicate rules); time and quiet
        rules are the scheduler's business. ``chat``: the owner-chat message (no fallback text, own log line).
        """
        started = time.monotonic()
        target = plan["target"]
        label = "private" if target["type"] == "private" else f"group:{target.get('key')}"
        out = {
            "result": "failed:OtherError",
            "voice": "skip:NotAttempted",
            "sticker": "skip:NotAttempted",
            "st_ms": 0,
        }
        targets = self._schedule_targets
        conversation = targets.conversation_for(target) if targets is not None else None
        self._sending_begin(conversation)
        try:
            await self._proactive_turn(plan, forced, out, chat=chat)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            out["result"] = f"failed:{_console_error_type(error)}"
        finally:
            self._sending_end(conversation)
            out["total_ms"] = int((time.monotonic() - started) * 1000)
            out.pop("reason", None)
            if delivered(out["result"]):
                # Going out counts as activity in the session and starts the minimum gap.
                self._touch_conversation(conversation, chat=chat, force_save=chat)
                state = self._proactive_state
                if state is not None:
                    try:
                        state.record_sent(self._schedule_now())
                    except Exception:
                        pass
            try:
                head = "ProactiveChat" if chat else f"Schedule plan={plan['id']} name={plan['name']} target={label}"
                logger.info(
                    f"[STBridge] {head} "
                    f"result={out['result']} voice={out['voice']} sticker={out['sticker']} "
                    f"st_ms={out['st_ms']} total_ms={out['total_ms']}"
                )
            except Exception:
                pass
        return out

    async def _proactive_turn(self, plan: dict, forced: bool, out: dict, chat: bool = False) -> None:
        target = plan["target"]
        group = target["type"] == "group"
        targets = self._schedule_targets
        conversation = targets.conversation_for(target) if targets is not None else None
        if conversation is None:
            out["result"] = "skip:UnknownGroup" if group else "skip:NoOwnerSession"
            return
        if conversation_platform(conversation) != self.active_platform():
            # A session of the platform that is not in use now: skipped, not an error and not retried.
            out["result"] = "skip:OtherPlatform"
            return
        parsed = await self._proactive_reply(plan, group, out, conversation, forced)
        reason = out.get("reason")
        if reason == "Requeued":
            # Nothing was asked of ST and nothing went out: the scheduler takes the occurrence back.
            out["result"] = "deferred:Requeued"
            return
        if parsed is None:
            if chat:
                out["result"] = f"failed:{reason or 'StUnavailable'}"
                return
            await self._proactive_fallback(plan, conversation, out)
            if reason == "Duplicate" and out["result"] == "fallback":
                out["result"] = "sent:fallback(duplicate)"
            return
        await self._proactive_send(conversation, parsed, forced, out)
        if out["result"] == "sent":
            self._remember_reply(conversation, parsed.chinese_text)

    async def _proactive_reply(
        self, plan: dict, group: bool, out: dict, conversation: str | None = None, forced: bool = False
    ) -> "_ParsedReply | None":
        """Ask ST (through the Q1 queue); None means "use the fallback text" (``out["reason"]`` says why)."""
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
            out["reason"] = "StUnavailable"
            return None
        content = self._proactive_text(plan, self._schedule_now(), group)
        # The owner's value goes to ST like for a private turn; a reply's affinity changes are never applied.
        speaker = self._speaker(
            self._operator_platform_id, self._operator_sender_id, "", group=False, song_permission=False
        )
        writes = speaker.writes if speaker is not None else None
        if writes is not None:
            # Nobody asked for a song in a scheduled message: the world book must not offer one.
            writes = [*writes, {"path": self._song_permission_path(), "value": 0}]

        # Nobody asked for a song in a scheduled message: the macro says "否" like the written permission.
        macros = self._turn_macros(speaker, None, song=False)

        queue = self._queue
        settings = self._queue_settings()
        targets = self._schedule_targets
        seen = targets.user_seq(conversation) if targets is not None and conversation else 0
        try:
            entry = queue.enqueue(None, content, max_waiting=settings["max_waiting"], proactive=True)
        except (QueueFullError, QueueUnavailableError) as error:
            out["reason"] = "QueueFull" if isinstance(error, QueueFullError) else "QueueUnavailable"
            return None

        def dispatch(current: QueueEntry):
            # Joined while nobody talked; if somebody has started since (a person waiting behind this turn, or a
            # new message in this session), give way: nothing is asked of ST.
            if not forced and (
                queue.user_waiting
                or (targets is not None and conversation and targets.user_seq(conversation) != seen)
            ):
                raise _ProactiveYield
            try:
                return state_manager.create_request(
                    content,
                    timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                    affinity_writes=writes,
                    kind="proactive",
                    macros=macros,
                )
            except RequestBusyError:
                return None

        def available() -> bool:
            return (
                service.state is HttpServiceState.RUNNING
                and gate.allows_business_requests() is True
            )

        st_started = time.monotonic()
        try:
            request = await queue.wait_turn(
                entry,
                max_wait_seconds=settings["max_wait_seconds"],
                dispatch=dispatch,
                available=available,
            )
        except asyncio.CancelledError:
            raise
        except _ProactiveYield:
            out["reason"] = "Requeued"
            return None
        except Exception as error:
            out["reason"] = "QueueTimeout" if isinstance(error, QueueTimeoutError) else "QueueUnavailable"
            return None
        try:
            try:
                ok, reply = await self._wait_for_reply(state_manager, request, service, gate)
            except asyncio.CancelledError:
                self._fail_request_if_active(state_manager, request.request_id)
                raise
            except Exception:
                self._fail_request_if_active(state_manager, request.request_id)
                out["reason"] = "StFailed"
                self._record_reply_result(self._reply_failure_code(state_manager, request, service, gate), st_started)
                return None
            out["st_ms"] = int((time.monotonic() - st_started) * 1000)
            if not ok:
                out["reason"] = "StFailed"
                self._record_reply_result(self._reply_failure_code(state_manager, request, service, gate), st_started)
                return None
            parsed = self._parse_reply(reply, OWNER_TRACK)
            if parsed is None:
                out["reason"] = "InvalidReply"
                self._record_reply_result("InvalidReply", st_started)
                return None
            # ST wrote a usable reply (even one that is dropped below as a repeat).
            self._record_reply_result("ok", st_started)
            previous = targets.last_text(conversation) if targets is not None and conversation else ""
            if not forced and is_duplicate(parsed.chinese_text, previous, self._dedupe_ratio()):
                # Dropped: ST repeated itself. Not recorded as a mood, not sent.
                logger.info("[STBridge] Proactive reply dropped (duplicate of the last message)")
                out["reason"] = "Duplicate"
                return None
            # Sent or not, ST wrote it: a private plan reaches the owner, a group plan only the room.
            self._record_mood(parsed, owner=not group)
            return parsed
        finally:
            queue.release(entry)

    async def _proactive_send_chain(self, conversation: str, segments: list[Segment]) -> None:
        sent = await self._platform.send_to(conversation, segments)
        if sent is False:
            raise RuntimeError("platform not found")

    async def _proactive_fallback(self, plan: dict, conversation: str, out: dict) -> None:
        out["voice"] = "skip:Fallback"
        out["sticker"] = "skip:Fallback"
        texts = [text for text in plan.get("fallback_texts", []) if text]
        if not texts:
            out["result"] = "skip:NoFallbackText"
            return
        try:
            chosen = self._schedule_choice(texts)
            await self._proactive_send_chain(conversation, [Segment.of_text(chosen)])
            out["result"] = "fallback"
            self._remember_reply(conversation, chosen)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            out["result"] = f"failed:{_console_error_type(error)}"

    async def _proactive_send(self, conversation: str, parsed: "_ParsedReply", forced: bool, out: dict) -> None:
        """Voice, then text, then sticker as separate messages; one failing never stops the rest.

        With "分条发" on and several groups, each group goes out as its own voice + text.
        """
        split_on, interval, limit = self._split_settings()
        pairs = parsed.pairs
        if split_on and len(pairs) > 1:
            groups = cap_pairs(pairs, limit)
            logger.info(
                f"[STBridge] Reply pairs={len(pairs)} mode=split groups={len(groups)}"
            )
        else:
            if len(pairs) > 1:
                logger.info(f"[STBridge] Reply pairs={len(pairs)} mode=merged")
            groups = None
        if groups is None or len(groups) == 1:
            emotion_tags = parsed.emotion_tags
            await self._proactive_group(
                conversation, parsed.japanese_text, parsed.chinese_text,
                emotion_tags, forced, out, gate=True,
            )
        else:
            # One probability/cooldown decision for the whole reply.
            gate = None if self._tts is None else self._voice_gate(conversation, forced=forced, rand=self._schedule_random)
            results, voices = [], []
            for index, (japanese, chinese, emotion) in enumerate(groups):
                if index:
                    await self._split_pause(interval)
                part: dict = {}
                await self._proactive_group(
                    conversation, japanese, chinese,
                    [emotion] if emotion is not None else [], forced, part, gate=False, skip=gate,
                )
                results.append(part["result"])
                voices.append(part["voice"])
            failed = [result for result in results if result != "sent"]
            out["result"] = failed[0] if failed else "sent"
            out["voice"] = "sent" if "sent" in voices else voices[0]
            emotion_tags = [groups[-1][2]] if groups[-1][2] is not None else []
        plan, outcome, _emotion, _candidates = self._decide_sticker(emotion_tags, conversation, force=forced)
        if plan is None:
            out["sticker"] = outcome or "skip:Unavailable"
            return
        try:
            await self._proactive_send_chain(conversation, [Segment.image(plan.path)])
            out["sticker"] = "sent"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            out["sticker"] = f"failed:{_console_error_type(error)}"

    async def _proactive_group(
        self,
        conversation: str,
        japanese: str,
        chinese: str,
        emotion_tags: list[str],
        forced: bool,
        out: dict,
        *,
        gate: bool,
        skip: str | None = None,
    ) -> None:
        """One group's voice then text. ``gate``: roll probability/cooldown here; else ``skip`` is the
        reply-wide verdict (None = go ahead)."""
        out["voice"] = await self._proactive_voice(
            conversation, japanese, emotion_tags, forced, gate=gate, skip=skip
        )
        tts = self._tts
        text_with_voice = True if tts is None else bool(tts.common["send_text_with_audio"])
        if out["voice"] == "sent" and not text_with_voice:
            out["result"] = "sent"
            return
        try:
            await self._proactive_send_chain(conversation, [Segment.of_text(chinese)])
            out["result"] = "sent"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            out["result"] = f"failed:{_console_error_type(error)}"

    async def _proactive_voice(
        self,
        conversation: str,
        japanese: str,
        emotion_tags: list[str],
        forced: bool,
        *,
        gate: bool = True,
        skip: str | None = None,
    ) -> str:
        tts = self._tts
        if tts is None:
            return "skip:TtsUnavailable"
        file_path = None
        try:
            common = tts.common
            if gate:
                reason = self._voice_gate(conversation, forced=forced, rand=self._schedule_random)
                if reason is not None:
                    return reason
            elif skip is not None:
                return skip
            text = japanese.strip()
            if not text:
                return "skip:NoText"
            text_limit = int(common["text_limit"])
            if text_limit > 0 and len(text) > text_limit:
                text = text[:text_limit]
            profile = self._select_tts_profile(emotion_tags)
            audio_data = await tts.synthesize(text, profile)
            temp_dir = tts.temp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)
            file_path = temp_dir / f"{uuid.uuid4()}.wav"
            file_path.write_bytes(voice_bytes_for_send(audio_data, tts, logger))
            await self._proactive_send_chain(conversation, [Segment.voice(file_path)])
            return "sent"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, IrodoriRequestError):
                logger.error(
                    "[STBridge] Irodori synthesis failed "
                    f"(status={error.status} type={error.error_type})"
                )
            elif isinstance(error, ModelSelectionError):
                logger.error("[STBridge] Model selection failed (ModelSelectionError)")
            elif isinstance(error, ServiceStartError):
                logger.error("[STBridge] TTS service unavailable (ServiceStartError)")
            else:
                logger.error(f"[STBridge] TTS conversion failed ({safe_error_type(error)})")
            return f"failed:{_console_error_type(error)}"
        finally:
            if file_path is not None:
                try:
                    file_path.unlink(missing_ok=True)
                except Exception:
                    pass
