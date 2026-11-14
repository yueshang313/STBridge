"""TTS profile selection and the voice that goes with a reply."""

import asyncio
import random
import time
import uuid
from pathlib import Path

from .log import logger
from .platform_api import Segment
from .audio_format import voice_bytes_for_send
from .emotion_config import parse_emotion_name, select_tts_profile
from .tts_clients import IrodoriRequestError, ModelSelectionError, safe_error_type
from .tts_service import ServiceStartError


class TtsMixin:
    """TTS profile selection and the voice that goes with a reply."""

    def _select_tts_profile(self, emotion_tags: list[str]) -> dict | None:
        """The emotion's TTS parameters (None: the plain TTS path); any failure leaves the plain path."""
        profile = None
        try:
            if len(emotion_tags) > 1:
                logger.warning(
                    f"[STBridge] Multiple emotion tags (count={len(emotion_tags)}); "
                    "using the first"
                )
            requested = parse_emotion_name(emotion_tags[0]) if emotion_tags else None
            no_tag_reason = "InvalidTag" if emotion_tags else "NoTag"
            store = self._emotion_store
            if store is None:
                profile, used, reason, source, candidates, pick = (
                    None, None, "StoreUnavailable", None, 0, 0,
                )
            else:
                selection = select_tts_profile(
                    store.config,
                    requested,
                    no_tag_reason=no_tag_reason,
                    default_audio_dir=store.audio_dir,
                )
                profile, used, reason, source, candidates, pick = (
                    selection.profile,
                    selection.emotion,
                    selection.reason,
                    selection.source,
                    selection.candidates,
                    selection.pick,
                )
            logger.info(
                f"[STBridge] TTS emotion requested={requested or 'none'} "
                f"used={used or 'none'} source={source or 'none'} "
                f"candidates={candidates} pick={pick} fallback={reason is not None} "
                f"reason={reason or 'none'}"
            )
        except Exception:
            logger.warning("[STBridge] TTS profile unavailable (OtherError)")
            return None
        return dict(profile) if isinstance(profile, dict) else None

    @staticmethod
    async def _cleanup_tts_file(path: Path, delay: float = 60) -> None:
        await asyncio.sleep(delay)
        try:
            if path.exists():
                path.unlink()
        except Exception as error:
            logger.warning(
                "[STBridge] Temporary audio cleanup failed "
                f"({safe_error_type(error)})"
            )

    def _split_settings(self) -> tuple[bool, float, int]:
        """(split on, seconds between groups, most groups) from the voice settings; off without TTS."""
        tts = self._tts
        if tts is None:
            return False, 0.0, 1
        try:
            common = tts.common
            return (
                bool(common.get("split_messages", False)),
                float(common.get("split_interval", 1.5)),
                int(common.get("split_max", 3)),
            )
        except Exception:
            return False, 0.0, 1

    @staticmethod
    async def _split_pause(seconds: float) -> None:
        """The gap between two groups of a split reply (a seam for tests)."""
        if seconds > 0:
            await asyncio.sleep(seconds)

    def _voice_gate(self, session_id: str, *, forced: bool = False, rand=None) -> str | None:
        """Probability and cooldown, decided once for a whole reply: None = go ahead, else why not.

        Stamps the cooldown when it says yes. ``forced`` (console test sends) ignores both.
        """
        tts = self._tts
        if tts is None:
            return "skip:TtsUnavailable"
        common = tts.common
        if not forced and (rand or random.random)() > float(common["prob"]) / 100.0:
            return "skip:Probability"
        now = time.time()
        cooldown = int(common["cooldown"])
        if not forced and cooldown > 0 and now - self._tts_session_state.get(session_id, 0) < cooldown:
            return "skip:Cooldown"
        self._tts_session_state[session_id] = now
        return None

    async def _voice_reply(
        self,
        text: str,
        tts_source_text: str,
        profile: dict | None,
        session_id: str,
        *,
        gated: bool = False,
    ) -> list[Segment] | None:
        """Voice a successful bridge reply: the segments to send instead of the plain text.

        ``text`` is the Chinese reply (with any "@name " prefix), ``tts_source_text`` the Japanese
        part to synthesize (``gated``: skip the probability/cooldown check). None means "send the text only": no TTS, not eligible, probability or
        cooldown said no, or the synthesis failed (logged).
        """
        tts = self._tts
        if tts is None:
            return None
        file_path = None
        wait_seconds = 0.0
        try:
            tts_source_text = tts_source_text.strip() if isinstance(tts_source_text, str) else ""
            if not tts_source_text:
                return None

            full_text = text.strip()
            if not full_text:
                return None

            common = tts.common
            # ``gated``: the caller already rolled probability and cooldown for the whole reply.
            if not gated and self._voice_gate(session_id) is not None:
                return None

            tts_text = tts_source_text
            text_limit = int(common["text_limit"])
            if text_limit > 0 and len(tts_text) > text_limit:
                logger.warning("[STBridge] TTS text exceeded the configured limit")
                tts_text = tts_text[:text_limit]

            # The text must not wait forever for a slow or broken voice: past the limit it goes alone,
            # and the late audio is dropped (wait_for cancels the synthesis; a service start goes on).
            wait_seconds = float(common.get("voice_wait_seconds", 60))
            audio_data = await asyncio.wait_for(
                tts.synthesize(tts_text, dict(profile) if isinstance(profile, dict) else None),
                timeout=wait_seconds,
            )

            temp_dir = tts.temp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)
            file_path = temp_dir / f"{uuid.uuid4()}.wav"
            file_path.write_bytes(voice_bytes_for_send(audio_data, tts, logger))

            voice = Segment.voice(file_path)
            if common["send_text_with_audio"]:
                segments = [Segment.of_text(full_text), voice]
            else:
                segments = [voice]

            asyncio.create_task(self._cleanup_tts_file(file_path, delay=60))
            file_path = None
            return segments
        except asyncio.TimeoutError:
            logger.warning(
                "[STBridge] TTS wait timed out, sending the text only "
                f"(VoiceWaitTimeout after {wait_seconds:g}s)"
            )
            return None
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
            if file_path is not None:
                try:
                    file_path.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    logger.warning(
                        "[STBridge] Temporary audio cleanup failed "
                        f"({safe_error_type(cleanup_error)})"
                    )
            return None
