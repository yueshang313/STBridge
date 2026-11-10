"""The bridge core driven through a minimal fake platform, without any AstrBot (or fake AstrBot) module."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import BridgeRecoveryGate
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import (
    GROUP,
    PRIVATE,
    IncomingMessage,
    PlatformAPI,
    ReplyBatch,
    Segment,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import ScheduleTargets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.sticker_store import StickerStore

OPERATOR = ("qq-platform", "owner-1")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
REPLY = '[情绪:开心]☆‹「やった」›\n"太好了"'


class FakePlatform(PlatformAPI):
    def __init__(self):
        self.sent = []  # (ref, segments)
        self.sent_to = []  # (conversation_id, segments)
        self.send_to_result = True

    async def send(self, ref, segments):
        self.sent.append((ref, list(segments)))

    async def send_to(self, conversation_id, segments):
        self.sent_to.append((conversation_id, list(segments)))
        return self.send_to_result

    def wake_prefixes(self):
        return ["/"]


class FakeTts:
    """Records what it is asked to synthesize and returns a tiny audio blob (Irodori native: sent as it is)."""

    def __init__(self, directory):
        self.settings = {"engine": "irodori", "irodori": {"output_mode": "native"}}
        self.common = {"prob": 100, "cooldown": 0, "text_limit": 0, "send_text_with_audio": True}
        self.temp_dir = Path(directory) / "temp_audio"
        self.calls = []

    async def synthesize(self, text, profile):
        self.calls.append((text, profile))
        return b"audio"


def message(chat_type=PRIVATE, text="你好", sender_id=OPERATOR[1], sender_name="主人", **fields):
    values = dict(
        platform="qq_official",
        platform_id=OPERATOR[0],
        chat_type=chat_type,
        session_id="session-1",
        conversation_id="conversation-1",
        sender_id=sender_id,
        sender_name=sender_name,
        group_id="group-1" if chat_type == GROUP else None,
        text=text,
        message_str=text,
        ref="ref-1",
    )
    values.update(fields)
    return IncomingMessage(**values)


class CoreWithFakePlatformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.platform = FakePlatform()
        self.core = BridgeCore(
            self.platform,
            lambda: self.root,
            operator_platform_id=OPERATOR[0],
            operator_sender_id=OPERATOR[1],
        )
        self.manager = RequestStateManager(timeout_seconds=30)
        self.core._request_state_manager = self.manager
        self.core._recovery_gate = BridgeRecoveryGate()
        self.core._recovery_gate.confirm_recovery()
        self.core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)

    async def next_request(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            request = self.manager.peek_pending_request()
            if request is not None:
                return request
            if time.monotonic() >= deadline:
                raise AssertionError("the core did not create a request")
            await asyncio.sleep(0.005)

    def answer(self, request, reply):
        self.manager.claim_request(request.request_id, "attempt")
        self.manager.mark_generation_started(request.request_id)
        self.manager.confirm_generation_finished(request.request_id)
        self.manager.complete_request(request.request_id, reply)

    async def drive(self, batches, reply):
        """Collect the batches of one turn while the (fake) ST answers with ``reply``."""
        collecting = asyncio.create_task(self._collect(batches))
        self.answer(await self.next_request(), reply)
        return await collecting

    @staticmethod
    async def _collect(batches):
        return [batch async for batch in batches]

    async def test_private_turn_yields_the_chinese_text_as_one_batch(self):
        msg = message()
        self.assertTrue(self.core.accept_private(msg))
        results = await self.drive(self.core.handle_private(msg), REPLY)
        self.assertEqual(results, [ReplyBatch.of_text("太好了")])

    async def test_private_turn_with_voice_yields_text_then_voice_in_one_batch(self):
        tts = self.core._tts = FakeTts(self.root)
        msg = message()
        results = await self.drive(self.core.handle_private(msg), REPLY)
        batch, = results
        text, voice = batch.segments
        self.assertEqual((text.kind, text.text), ("text", "太好了"))
        self.assertEqual(voice.kind, "voice")
        self.assertEqual(voice.path.read_bytes(), b"audio")
        self.assertEqual([call[0] for call in tts.calls], ["「やった」"])

    async def test_only_the_operator_is_accepted_in_private_chat(self):
        self.assertFalse(self.core.accept_private(message(sender_id="stranger")))
        self.assertFalse(self.core.accept_private(message(platform_id="other-platform")))
        self.assertFalse(self.core.accept_private(message(chat_type=GROUP)))

    async def test_group_turn_prefixes_the_sender_label_and_mentions_nobody_without_a_wait(self):
        msg = message(GROUP, text="大家好", sender_id="member-1", sender_name="小明")
        collecting = asyncio.create_task(self._collect(self.core.handle_group(msg)))
        request = await self.next_request()
        self.assertEqual(request.content, "【群友】小明（1）：大家好")
        self.answer(request, REPLY)
        self.assertEqual(await collecting, [ReplyBatch.of_text("太好了")])

    async def test_group_message_without_a_sender_id_is_rejected_without_a_request(self):
        msg = message(GROUP, text="大家好", sender_id="")
        self.assertEqual(await self._collect(self.core.handle_group(msg)), [])
        self.assertIsNone(self.manager.peek_pending_request())

    async def test_commands_run_through_the_same_message_type(self):
        msg = message(command="recover")
        self.core._recovery_gate = BridgeRecoveryGate()
        batches = await self._collect(self.core.handle_command(msg))
        self.assertEqual(len(batches), 1)
        self.assertIn("人工恢复确认已记录", batches[0].segments[0].text)
        self.assertEqual(await self._collect(self.core.handle_command(message(command="unknown"))), [])

    async def test_extra_send_goes_to_the_message_ref(self):
        self.core._emotion_store = ConfigStore(self.root)
        self.core._sticker_store = StickerStore(self.root)
        self.core._sticker_store.add("开心", "a.png", PNG)
        self.core._sticker_random = lambda: 0.0
        msg = message(ref="the-ref")
        await self.drive(self.core.handle_private(msg), REPLY)
        (ref, segments), = self.platform.sent
        self.assertEqual(ref, "the-ref")
        self.assertEqual([segment.kind for segment in segments], ["image"])
        self.assertEqual(segments[0].path.name, self.core._sticker_store.list_files("开心")[0].name)

    async def test_proactive_send_goes_to_the_conversation_and_reports_failure(self):
        await self.core._proactive_send_chain("conversation-9", [Segment.of_text("早安")])
        self.assertEqual(self.platform.sent_to, [("conversation-9", [Segment.of_text("早安")])])
        self.platform.send_to_result = False
        with self.assertRaises(RuntimeError):
            await self.core._proactive_send_chain("conversation-9", [Segment.of_text("早安")])

    async def test_scheduled_turn_asks_st_and_sends_to_the_remembered_conversation(self):
        self.core._schedule_targets = ScheduleTargets(self.root)
        self.assertTrue(self.core.accept_private(message(conversation_id="owner-conversation")))
        plan = {
            "id": "p1", "name": "早安", "prompt": "说早安", "fallback_texts": [],
            "target": {"type": "private"},
        }
        running = asyncio.create_task(self.core._run_proactive(plan, forced=True))
        self.answer(await self.next_request(), REPLY)
        out = await running
        self.assertEqual(out["result"], "sent")
        self.assertEqual(self.platform.sent_to, [("owner-conversation", [Segment.of_text("太好了")])])


if __name__ == "__main__":
    unittest.main()
