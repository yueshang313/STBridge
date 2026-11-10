"""The console's test chat (A1): run one message through the normal flow without any side effect.

``POST /api/test/chat`` lands here. The message carries a ``ChatProbe`` (platform_api); the bridge flow checks it
and skips voice, stickers, the song flow, affinity writes, mood records, schedule targets and platform sends.
Everything else (the request queue behind real users, macro escaping, the group prefix on every line, reply
parsing, the split settings) is the real thing.
"""

import time

from .emotion_config import EmotionConfigError
from .log import logger
from .platform_api import TEST_CONVERSATION_PREFIX, ChatProbe, IncomingMessage, PRIVATE, GROUP, ReplyBatch

MAX_TEST_TEXT_CHARS = 2000
MAX_TEST_FIELD_CHARS = 64
MAX_TEST_RAW_CHARS = 20000
DEFAULT_TEST_SENDER_ID = "stbridge-test-1"
DEFAULT_TEST_SENDER_NAME = "测试群友"
DEFAULT_TEST_GROUP_ID = "stbridge-test"
PLATFORM_NAME = "stbridge_test"


def batch_text(batch: ReplyBatch) -> str:
    """What QQ would show for a batch: text as it is, a voice or an image as a placeholder."""
    parts = []
    for segment in batch.segments:
        if segment.kind == "text":
            parts.append(segment.text)
        elif segment.kind == "voice":
            parts.append("[语音]")
        else:
            parts.append("[图片]")
    return "\n".join(parts)


def _text_field(body: dict, key: str, default: str) -> str:
    value = body.get(key)
    if value is None or value == "":
        return default
    if not isinstance(value, str) or len(value) > MAX_TEST_FIELD_CHARS:
        raise EmotionConfigError(400, f"invalid {key}")
    return value


class ChatTestMixin:
    """Console test chat."""

    # Only one test chat at a time.
    _test_busy = False

    async def run_test_chat(self, body: object) -> dict:
        if not isinstance(body, dict):
            raise EmotionConfigError(400, "JSON object required")
        chat = body.get("chat")
        if chat not in (PRIVATE, GROUP):
            raise EmotionConfigError(400, "invalid chat")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise EmotionConfigError(400, "text required")
        if len(text) > MAX_TEST_TEXT_CHARS:
            raise EmotionConfigError(400, "text too long")
        sender_id = _text_field(body, "sender_id", DEFAULT_TEST_SENDER_ID)
        sender_name = _text_field(body, "sender_name", DEFAULT_TEST_SENDER_NAME)
        group_id = _text_field(body, "group_id", DEFAULT_TEST_GROUP_ID)

        platform_id, owner_id = self._operator()
        if not platform_id or not owner_id:
            raise EmotionConfigError(409, "NoOwner")
        if self._test_busy:
            raise EmotionConfigError(409, "Busy")
        self._test_busy = True
        started = time.monotonic()
        probe = ChatProbe()
        try:
            if chat == PRIVATE:
                # The owner's identity: the bridge only answers the owner in private chat.
                conversation = f"{TEST_CONVERSATION_PREFIX}private"
                msg = IncomingMessage(
                    platform=PLATFORM_NAME, platform_id=platform_id, chat_type=PRIVATE,
                    session_id=conversation, conversation_id=conversation,
                    sender_id=owner_id, sender_name="", group_id=None,
                    text=text, message_str=text, ref=conversation, probe=probe,
                )
                handler = self.handle_private(msg)
            else:
                conversation = f"{TEST_CONVERSATION_PREFIX}group:{group_id}"
                msg = IncomingMessage(
                    platform=PLATFORM_NAME, platform_id=platform_id, chat_type=GROUP,
                    session_id=conversation, conversation_id=conversation,
                    sender_id=sender_id, sender_name=sender_name, group_id=group_id,
                    text=text, message_str=text, ref=conversation, probe=probe,
                )
                handler = self.handle_group(msg)
            replies: list[str] = []
            try:
                async for batch in handler:
                    replies.append(batch_text(batch))
            finally:
                await handler.aclose()
        finally:
            self._test_busy = False
        elapsed = int((time.monotonic() - started) * 1000)
        result = probe.result or "ok"
        logger.info(f"[STBridge] TestChat chat={chat} result={result} ms={elapsed}")
        raw = probe.raw
        return {
            "result": result,
            "replies": replies,
            "raw": raw[:MAX_TEST_RAW_CHARS],
            "raw_truncated": len(raw) > MAX_TEST_RAW_CHARS,
            "emotion_tags": list(probe.emotion_tags),
            "affinity_change": probe.affinity_change,
            "sing": probe.sing,
            "elapsed_ms": elapsed,
        }
