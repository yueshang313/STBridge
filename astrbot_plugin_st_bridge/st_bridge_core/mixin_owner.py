"""Claiming the owner by a one-time code (console button + a private message with the 6 digits)."""

import time

from .affinity_store import user_hash
from .chat_platform import ASTRBOT, NAPCAT, NAPCAT_PLATFORM_ID, chat_platform_settings
from .log import logger
from .owner_claim import CLAIMED, FAILED, claim_candidate, mask_id
from .platform_api import PRIVATE, IncomingMessage, ReplyBatch

# accept_private spends the code; handle_private (the same message, a moment later) answers and writes the owner.
# An entry nobody picked up in this time is dropped.
CLAIM_HANDOFF_SECONDS = 30.0


class OwnerMixin:
    """Owner claim: the console starts it, a private message with the code finishes it."""

    def owner_claim_start(self) -> dict:
        """A new 6-digit code (the old one stops working); says whether it will replace an owner."""
        self._claim_pending.clear()
        started = self._owner_claim.start()
        platform_id, sender_id = self._operator()
        return {**started, "replaces": bool(platform_id and sender_id)}

    def owner_claim_status(self) -> dict:
        """What the console polls: the claim state plus the owner now in use on the active platform."""
        status = self._owner_claim.status()
        platform_id, sender_id = self._operator()
        current = None
        if platform_id and sender_id:
            current = {"platform_id": platform_id, "masked_id": mask_id(sender_id)}
        return {**status, "active_platform": self.active_platform(), "current": current}

    def _claim_platform(self, msg: IncomingMessage) -> str:
        return NAPCAT if msg.platform_id == NAPCAT_PLATFORM_ID else ASTRBOT

    def _claim_candidate(self, msg: IncomingMessage) -> str | None:
        prefixes = msg.wake_prefixes
        return claim_candidate(msg.text, prefixes) or claim_candidate(msg.message_str, prefixes)

    def _claim_try(self, msg: IncomingMessage) -> bool:
        """Is this private message the claim code? Spends the code (once) and remembers who sent it."""
        if msg.probe is not None or not self._owner_claim.active():
            return False
        if msg.chat_type != PRIVATE or not msg.identity_ok:
            return False
        sender_id = msg.sender_id.strip() if isinstance(msg.sender_id, str) else ""
        if not sender_id or not msg.conversation_id or not msg.platform_id:
            return False
        platform = self._claim_platform(msg)
        if platform != self.active_platform():
            return False  # the other platform's messages are ignored, claim or not
        if platform == NAPCAT and not (sender_id.isascii() and sender_id.isdigit()):
            return False
        candidate = self._claim_candidate(msg)
        if candidate is None or not self._owner_claim.consume(candidate):
            return False
        self._claim_pending[msg.conversation_id] = {
            "platform": platform,
            "platform_id": msg.platform_id,
            "sender_id": sender_id,
            "code": candidate,
            "at": time.monotonic(),
        }
        return True

    def _claim_take(self, msg: IncomingMessage) -> dict | None:
        """The remembered claim this very message belongs to (None for every other message)."""
        if msg.probe is not None:
            return None
        pending = self._claim_pending.pop(msg.conversation_id, None)
        if pending is None:
            return None
        fresh = time.monotonic() - pending["at"] <= CLAIM_HANDOFF_SECONDS
        same = (
            pending["sender_id"] == msg.sender_id.strip()
            and pending["platform_id"] == msg.platform_id
            and self._claim_candidate(msg) == pending["code"]
        )
        return pending if fresh and same else None

    async def _apply_claim(self, msg: IncomingMessage, pending: dict):
        """Write the owner (live at once, like a manual save) and answer in the fixed words; no ST, no queue, no counts."""
        platform, sender_id = pending["platform"], pending["sender_id"]
        store = self._emotion_store
        try:
            if store is None:
                raise RuntimeError("config store unavailable")
            if platform == NAPCAT:
                def update(config):
                    settings = chat_platform_settings(config)
                    settings["napcat_owner"] = sender_id
                    config["chat_platform"] = settings
            else:
                def update(config):
                    config["owner"] = {"platform_id": pending["platform_id"], "sender_id": sender_id, "imported": True}
            await store.update(update)
        except Exception as error:
            logger.error(f"[STBridge] Owner claim failed ({type(error).__name__})")
            self._owner_claim.finish(FAILED)
            return
        # The owner's own chat is a place scheduled messages can go from now on.
        self._record_schedule_target(msg, group=False)
        logger.info(f"[STBridge] Owner claimed platform={platform} user={user_hash(sender_id)}")
        self._owner_claim.finish(CLAIMED, platform=platform, platform_id=pending["platform_id"], masked_id=mask_id(sender_id))
        yield ReplyBatch.of_text(self._reply_text("owner_claimed"))
