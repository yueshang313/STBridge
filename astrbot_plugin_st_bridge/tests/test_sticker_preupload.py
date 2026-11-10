"""K1.1: the sticker is uploaded while the voice is synthesized, then sent right after the text."""

import asyncio
import gc
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.sticker_store import StickerStore
from test_plugin_lifecycle import submodule
from test_plugin_lifecycle import (
    _bridge_event,
    _group_bridge_event,
    _load_plugin_module,
    _ready_plugin,
    _wait_for_request,
)

HAPPY = "开心"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
REPLY = f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\""
MEDIA = object()


class FakeQQEvent:
    """qq_official-like event: records upload / media sends / plain sends in order."""

    def __init__(self, base, log, *, kind, upload=None):
        self._base = base
        self.log = log
        self.session_id = "session-1"
        self.kind = kind
        self.uploads = []
        self.group_posts = []
        self.c2c_posts = []
        self.sent = []
        self.upload_impl = upload
        self.c2c_result = {"id": "m"}
        self.group_error = None
        self.group_fail_count = None  # None: every call fails; n: only the first n calls
        self.group_calls = []
        self.bot = SimpleNamespace(api=SimpleNamespace(post_group_message=self._post_group))

    def __getattr__(self, name):
        return getattr(self._base, name)

    async def upload_group_and_c2c_image(self, image_base64, file_type, **kwargs):
        self.uploads.append((image_base64, file_type, kwargs))
        self.log.append("upload")
        if self.upload_impl is not None:
            return await self.upload_impl()
        return MEDIA

    async def _post_group(self, **kwargs):
        self.log.append("post_group")
        self.group_calls.append(kwargs)
        if self.group_error is not None and (
            self.group_fail_count is None or len(self.group_calls) <= self.group_fail_count
        ):
            raise self.group_error
        self.group_posts.append(kwargs)
        return {"id": "m"}

    async def post_c2c_message(self, **kwargs):
        self.log.append("post_c2c")
        self.c2c_posts.append(kwargs)
        return self.c2c_result

    async def send(self, chain):
        self.log.append("send")
        self.sent.append(chain)


class PreuploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = _ready_plugin(self.module)
        self.plugin._core._emotion_store = ConfigStore(Path(self.directory.name))
        self.plugin._core._sticker_store = StickerStore(Path(self.directory.name))
        self.plugin._core._sticker_store.add(HAPPY, "a.png", PNG)
        self.plugin._core._sticker_random = lambda: 0.0
        self.log = []
        self.module.test_logger.infos.clear()
        self.attempts = 0
        original = self.plugin._core._preupload_sticker
        self.plugin._core._preupload_sticker = lambda *args: (self.log.append("preupload_created"), original(*args))[1]

    def make_event(self, kind, *, upload=None, platform_name="qq_official"):
        if kind == "group":
            raw = self.module.TestGroupMessage()
            raw.group_openid = "group-openid-1"
            base = _group_bridge_event(self.module, "hi")
        else:
            raw = SimpleNamespace(author=SimpleNamespace(user_openid="user-openid-1"))
            base = _bridge_event(self.module, "hi", platform_name=platform_name, raw_message=raw)
        base.platform_name = platform_name
        base.message_obj = SimpleNamespace(raw_message=raw, message_id="msg-id-1")
        return FakeQQEvent(base, self.log, kind=kind, upload=upload)

    def handler(self, event):
        return self.plugin.stbridge_group_message if event.kind == "group" else self.plugin.stbridge_private_message

    async def start(self, event, reply=REPLY):
        """Run the handler up to its first yield; returns (generator, first item)."""
        generator = self.handler(event)(event)
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            first = asyncio.ensure_future(generator.__anext__())
            request = await _wait_for_request(self.plugin._core._request_state_manager)
            manager = self.plugin._core._request_state_manager
            self.attempts += 1
            manager.claim_request(request.request_id, f"attempt-{self.attempts}")
            manager.mark_generation_started(request.request_id)
            manager.confirm_generation_finished(request.request_id)
            manager.complete_request(request.request_id, reply)
            item = await asyncio.wait_for(first, 5)
        self.log.append("text")
        return generator, item

    async def finish(self, generator):
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(generator.__anext__(), 5)

    async def run_reply(self, event, reply=REPLY):
        generator, _ = await self.start(event, reply)
        await self.finish(generator)

    def sticker_logs(self):
        return [line for line in self.module.test_logger.infos if "Sticker" in line]

    async def test_upload_starts_before_text_and_does_not_block_it(self):
        gate = asyncio.Event()

        async def slow_upload():
            await gate.wait()
            return MEDIA

        event = self.make_event("private", upload=slow_upload)
        generator, item = await self.start(event)  # the text came out although the upload hangs
        for _ in range(200):
            if event.uploads:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(event.uploads), 1)
        self.assertEqual(event.c2c_posts, [])
        gate.set()
        await self.finish(generator)
        self.assertEqual(len(event.c2c_posts), 1)
        self.assertLess(self.log.index("text"), self.log.index("post_c2c"))
        self.assertEqual(event.sent, [])

    async def test_upload_call_starts_before_the_text_is_yielded(self):
        event = self.make_event("private")
        await self.run_reply(event)
        self.assertEqual(self.log[:2], ["preupload_created", "text"])

    async def test_private_uses_c2c_media_post(self):
        event = self.make_event("private")
        await self.run_reply(event)
        encoded, file_type, kwargs = event.uploads[0]
        self.assertTrue(encoded.startswith("iVBOR"))
        self.assertEqual(file_type, 1)
        self.assertEqual(kwargs, {"openid": "user-openid-1"})
        post = event.c2c_posts[0]
        self.assertEqual(post["openid"], "user-openid-1")
        self.assertEqual(post["msg_type"], 7)
        self.assertIs(post["media"], MEDIA)
        self.assertEqual(post["msg_id"], "msg-id-1")
        self.assertTrue(1 <= post["msg_seq"] <= 10000)
        self.assertNotIn("content", post)
        self.assertEqual(event.sent, [])
        self.assertEqual(event.group_posts, [])

    async def test_group_uses_group_media_post(self):
        event = self.make_event("group")
        await self.run_reply(event)
        self.assertEqual(event.uploads[0][2], {"group_openid": "group-openid-1"})
        self.assertEqual(len(event.group_posts), 1)
        post = event.group_posts[0]
        self.assertEqual(post["group_openid"], "group-openid-1")
        self.assertEqual(post["msg_type"], 7)
        self.assertIs(post["media"], MEDIA)
        self.assertEqual(post["msg_id"], "msg-id-1")  # passive reply (the K1.2 active send is gone)
        self.assertTrue(1 <= post["msg_seq"] <= 10000)
        self.assertNotIn("content", post)
        self.assertEqual(event.c2c_posts, [])
        self.assertEqual(event.sent, [])
        self.assertRegex(self.sticker_logs()[0], r"result=sent preupload=ok upload_ms=\d+ mode=passive resume_ms=\d+$")
        self.assertEqual(len(event.group_calls), 1)

    async def test_upload_failure_falls_back_to_plain_send(self):
        async def failing():
            raise RuntimeError("boom")

        event = self.make_event("private", upload=failing)
        await self.run_reply(event)
        self.assertEqual(len(event.sent), 1)
        self.assertTrue(event.sent[0].chain[0].path.endswith("a.png"))
        self.assertEqual(event.c2c_posts, [])
        self.assertRegex(self.sticker_logs()[0], r"result=sent preupload=failed:RuntimeError upload_ms=0 mode=event_send resume_ms=\d+$")

    async def test_upload_timeout_falls_back_to_plain_send(self):
        async def hang():
            await asyncio.sleep(60)

        event = self.make_event("group", upload=hang)
        with patch.object(submodule(self.module, "mixin_sticker"), "STICKER_PREUPLOAD_TIMEOUT_SECONDS", 0.05):
            await self.run_reply(event)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.group_posts, [])
        self.assertRegex(self.sticker_logs()[0], r"result=sent preupload=timeout upload_ms=0 mode=event_send resume_ms=\d+$")

    async def test_non_qq_official_event_sends_directly(self):
        event = self.make_event("private", platform_name="aiocqhttp")
        await self.run_reply(event)
        self.assertEqual(event.uploads, [])
        self.assertEqual(len(event.sent), 1)
        self.assertRegex(self.sticker_logs()[0], r"result=sent preupload=none upload_ms=0 mode=event_send resume_ms=\d+$")

    async def test_guild_message_without_openid_sends_directly(self):
        event = self.make_event("private")
        event.message_obj.raw_message = SimpleNamespace(author=SimpleNamespace(id="guild-user"))
        await self.run_reply(event)
        self.assertEqual(event.uploads, [])
        self.assertEqual(len(event.sent), 1)

    async def test_log_fields_for_successful_preupload(self):
        event = self.make_event("private")
        await self.run_reply(event)
        lines = self.sticker_logs()
        self.assertEqual(len(lines), 1)
        self.assertRegex(
            lines[0],
            rf"^\[STBridge\] Sticker emotion={HAPPY} candidates=1 result=sent preupload=ok upload_ms=\d+ mode=passive resume_ms=\d+$",
        )
        for leaked in ("openid", "a.png", self.directory.name, "msg-id"):
            self.assertNotIn(leaked, lines[0])

    async def test_media_send_error_is_logged_without_second_send(self):
        event = self.make_event("group")
        event.group_error = RuntimeError("rejected")
        generator, _ = await self.start(event)
        await self.finish(generator)  # the reply was not affected
        self.assertEqual(event.sent, [])
        self.assertRegex(
            self.sticker_logs()[0],
            r"result=failed:RuntimeError preupload=ok upload_ms=\d+ mode=passive resume_ms=\d+$",
        )
        self.assertEqual(len(event.group_calls), 1)
        self.assertEqual(self.module.test_logger.errors, [])

    async def test_private_chat_stays_passive(self):
        event = self.make_event("private")
        await self.run_reply(event)
        self.assertEqual(event.group_calls, [])
        self.assertEqual(event.c2c_posts[0]["msg_id"], "msg-id-1")
        self.assertIn("mode=passive resume_ms=", self.sticker_logs()[0])
        self.assertNotIn("active_error", self.sticker_logs()[0])

    async def test_resume_ms_measures_from_the_decision_to_the_resume(self):
        event = self.make_event("private")
        generator, _ = await self.start(event)
        await asyncio.sleep(0.12)  # voice + text are being sent
        await self.finish(generator)
        resume = int(re.search(r"resume_ms=(\d+)", self.sticker_logs()[0]).group(1))
        self.assertGreaterEqual(resume, 100)

    async def test_c2c_media_send_returning_nothing_is_a_failure(self):
        event = self.make_event("private")
        event.c2c_result = None
        await self.run_reply(event)
        self.assertEqual(event.sent, [])
        self.assertIn("result=failed:RuntimeError preupload=ok", self.sticker_logs()[0])

    async def test_unresumed_handler_does_not_leave_task_exception_unretrieved(self):
        async def failing():
            raise RuntimeError("boom")

        reports = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        self.addCleanup(loop.set_exception_handler, None)
        event = self.make_event("private", upload=failing)
        generator, _ = await self.start(event)
        await generator.aclose()  # AstrBot stopped the event: the handler is never resumed
        for _ in range(200):
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual([r for r in reports if "never retrieved" in str(r.get("message", ""))], [])
        self.assertEqual(self.sticker_logs(), [])

    async def test_cooldown_is_taken_when_the_decision_is_made(self):
        event = self.make_event("private")
        generator, _ = await self.start(event)
        # Decided before the voice: a second reply of the session is already in cooldown.
        self.assertIn("session-1", self.plugin._core._sticker_session_state)
        await self.finish(generator)


if __name__ == "__main__":
    unittest.main()
