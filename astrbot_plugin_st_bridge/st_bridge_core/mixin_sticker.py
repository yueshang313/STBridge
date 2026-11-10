"""Mood stickers sent after the text."""

import asyncio
import time
from pathlib import Path

from .log import logger
from .platform_api import IncomingMessage, Segment
from .emotion_config import parse_emotion_name, sticker_settings
from .bridge_types import _StickerPlan, _console_error_type, _retrieve_task_exception


# Pre-upload of a sticker must finish within this many seconds, else the plain send is used.
STICKER_PREUPLOAD_TIMEOUT_SECONDS = 30.0


class StickerMixin:
    """Mood stickers sent after the text."""

    def _log_sticker(self, emotion: str | None, candidates: int, outcome: str, extra: str = "") -> None:
        try:
            logger.info(
                f"[STBridge] Sticker emotion={emotion or 'none'} "
                f"candidates={candidates} result={outcome}{extra}"
            )
        except Exception:
            pass

    def _decide_sticker(
        self, emotion_tags: list[str], session_id: str, *, force: bool = False
    ) -> tuple[_StickerPlan | None, str | None, str | None, int]:
        """Pick the sticker for a reply: (plan, skip/failure outcome, emotion, candidates).

        ``force`` ignores the probability and the cooldown (the console test button).
        """
        emotion = None
        candidates = 0
        try:
            config_store = self._emotion_store
            stickers = self._sticker_store
            if config_store is None or stickers is None:
                return None, None, None, 0  # nothing to report while stickers are unavailable
            settings = sticker_settings(config_store.config)
            if not settings["enabled"]:
                return None, "skip:Disabled", None, 0
            emotion = parse_emotion_name(emotion_tags[0]) if emotion_tags else None
            if emotion is None:
                return None, ("skip:InvalidTag" if emotion_tags else "skip:NoTag"), None, 0
            files = stickers.list_files(emotion)
            candidates = len(files)
            if not files:
                return None, "skip:NoImages", emotion, 0
            if not force and self._sticker_random() * 100.0 >= settings["prob"]:
                return None, "skip:Probability", emotion, candidates
            now = self._sticker_clock()
            last = self._sticker_session_state.get(session_id)
            if (
                not force
                and settings["cooldown"] > 0
                and last is not None
                and now - last < settings["cooldown"]
            ):
                return None, "skip:Cooldown", emotion, candidates
            # The decision counts for the cooldown even when sending later fails.
            self._sticker_session_state[session_id] = now
            path = files[min(int(self._sticker_random() * len(files)), len(files) - 1)]
            plan = _StickerPlan(
                emotion=emotion, path=path, candidates=candidates, decided_at=time.monotonic()
            )
            return plan, None, emotion, candidates
        except Exception:
            return None, "failed:PrepareError", emotion, candidates

    async def _prepare_sticker(self, msg: IncomingMessage, emotion_tags: list[str]) -> _StickerPlan | None:
        """Decide whether and which sticker to send; start its upload without waiting. Never raises."""
        plan, outcome, emotion, candidates = self._decide_sticker(emotion_tags, msg.session_id)
        if plan is None:
            if outcome is not None:
                self._log_sticker(emotion, candidates, outcome)
            return None
        try:
            try:
                plan.uploadable = bool(await self._platform.can_preupload_image(msg.ref))
            except Exception:
                plan.uploadable = False  # the plain send is the fallback
            if plan.uploadable:
                plan.task = asyncio.create_task(self._preupload_sticker(msg, plan.path))
                plan.task.add_done_callback(_retrieve_task_exception)
            return plan
        except Exception:
            self._log_sticker(emotion, candidates, "failed:PrepareError")
            return None

    async def _preupload_sticker(self, msg: IncomingMessage, path: Path):
        """Upload the image file ahead of time without sending it; returns (handle, milliseconds)."""
        started = time.monotonic()
        handle = await self._platform.preupload_image(msg.ref, path)
        return handle, int((time.monotonic() - started) * 1000)

    async def _finish_sticker(self, msg: IncomingMessage, plan: _StickerPlan) -> None:
        """Send the planned sticker as its own message; never raises (except cancellation)."""
        outcome = "failed:OtherError"
        preupload = "none"
        upload_ms = 0
        resume_ms = int((time.monotonic() - plan.decided_at) * 1000)
        # Filled by the send: mode (passive|event_send).
        info: dict[str, str] = {"mode": "none"}
        try:
            handle = None
            if plan.task is not None:
                try:
                    handle, upload_ms = await asyncio.wait_for(
                        plan.task, STICKER_PREUPLOAD_TIMEOUT_SECONDS
                    )
                    preupload = "ok"
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    preupload = "timeout"
                except Exception as error:
                    preupload = f"failed:{_console_error_type(error)}"
            if handle is not None:
                # Direct media send: only the image message, no text, no second upload.
                # A group sticker as an active message (no msg_id) was tried in K1.2: QQ answered with
                # nothing every time, so it only cost time. Passive replies show an "@" in groups; accepted.
                info["mode"] = "passive"
                await self._platform.send_uploaded_image(msg.ref, handle)
            else:
                info["mode"] = "event_send"
                await self._platform.send(msg.ref, [Segment.image(plan.path)])
            outcome = "sent"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            outcome = f"failed:{_console_error_type(error)}"
        finally:
            self._log_sticker(
                plan.emotion,
                plan.candidates,
                outcome,
                f" preupload={preupload} upload_ms={upload_ms} mode={info['mode']} "
                f"resume_ms={resume_ms}",
            )
