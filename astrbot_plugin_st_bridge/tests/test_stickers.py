"""K1: per-emotion stickers (store, settings, console API, send after the reply text)."""

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, FormData, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_sticker_settings,
    sticker_settings,
    validate_sticker_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.sticker_store import (
    MAX_STICKER_BYTES,
    StickerStore,
    detect_image_suffix,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_queue import BridgeQueue
from test_plugin_lifecycle import (
    _bridge_event,
    _collect,
    _load_plugin_module,
    _ready_plugin,
    _wait_for_request,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
HAPPY = "开心"


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StickerSettingsTests(unittest.TestCase):
    def test_defaults_and_fallback(self):
        self.assertEqual(default_sticker_settings(), {"enabled": True, "prob": 30, "cooldown": 60})
        self.assertEqual(sticker_settings({}), default_sticker_settings())
        self.assertEqual(sticker_settings({"stickers": {"prob": 1000}}), default_sticker_settings())
        self.assertEqual(sticker_settings(None), default_sticker_settings())

    def test_validation(self):
        self.assertEqual(
            validate_sticker_settings({"enabled": False, "prob": 0, "cooldown": 3600}),
            {"enabled": False, "prob": 0, "cooldown": 3600},
        )
        self.assertEqual(validate_sticker_settings({"prob": 100})["cooldown"], 60)
        for bad in (
            {"enabled": "yes"},
            {"prob": -1},
            {"prob": 101},
            {"prob": 1.5},
            {"prob": True},
            {"cooldown": 3601},
            {"cooldown": "x"},
        ):
            with self.assertRaises(EmotionConfigError, msg=str(bad)):
                validate_sticker_settings(bad)
        with self.assertRaises(EmotionConfigError):
            validate_sticker_settings([])

    def test_old_config_gets_defaults_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            self.assertEqual(store.config["stickers"], default_sticker_settings())


class StickerStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StickerStore(Path(self.directory.name))

    def test_magic_detection(self):
        self.assertEqual(detect_image_suffix(PNG), ".png")
        self.assertEqual(detect_image_suffix(JPG), ".jpg")
        self.assertEqual(detect_image_suffix(GIF), ".gif")
        self.assertEqual(detect_image_suffix(b"GIF87a"), ".gif")
        self.assertIsNone(detect_image_suffix(b"RIFF....WEBPVP8 "))
        self.assertIsNone(detect_image_suffix(b""))

    def test_add_uses_detected_suffix_and_dedupes(self):
        first = self.store.add(HAPPY, "pic.exe", PNG)
        second = self.store.add(HAPPY, "pic.jpg", PNG)
        self.assertEqual(first.name, "pic.png")
        self.assertEqual(second.name, "pic (2).png")
        self.assertEqual([f.name for f in self.store.list_files(HAPPY)], ["pic (2).png", "pic.png"])

    def test_add_rejects_bad_content(self):
        with self.assertRaises(EmotionConfigError):
            self.store.add(HAPPY, "a.png", b"not an image")
        with self.assertRaises(EmotionConfigError):
            self.store.add(HAPPY, "a.png", b"")
        with self.assertRaises(EmotionConfigError) as caught:
            self.store.add(HAPPY, "a.png", PNG + b"\x00" * MAX_STICKER_BYTES)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.store.list_files(HAPPY), [])

    def test_add_sanitises_filename(self):
        path = self.store.add(HAPPY, "..\\..\\evil:name?.png", PNG)
        self.assertEqual(path.parent, self.store.root / HAPPY)
        self.assertEqual(path.suffix, ".png")
        nameless = self.store.add(HAPPY, "", GIF)
        self.assertEqual(nameless.parent, self.store.root / HAPPY)

    def test_traversal_emotion_names_are_refused(self):
        for bad in ("..", "../x", "a/b", "a\\b", "", " ", "x" * 41):
            with self.assertRaises(EmotionConfigError, msg=bad):
                self.store.add(bad, "a.png", PNG)
            self.assertEqual(self.store.list_files(bad), [])

    def test_resolve_and_delete_names(self):
        path = self.store.add(HAPPY, "a.png", PNG)
        self.assertEqual(self.store.resolve(HAPPY, "a.png"), path.resolve())
        for bad in ("../a.png", "..\\a.png", "x/a.png", "", "missing.png", "a.txt"):
            with self.assertRaises(EmotionConfigError, msg=bad):
                self.store.resolve(HAPPY, bad)
        (self.store.root / HAPPY / "note.txt").write_text("x")
        with self.assertRaises(EmotionConfigError):
            self.store.resolve(HAPPY, "note.txt")

    def test_list_ignores_non_images(self):
        self.store.add(HAPPY, "a.png", PNG)
        (self.store.root / HAPPY / "note.txt").write_text("x")
        self.assertEqual([f.name for f in self.store.list_files(HAPPY)], ["a.png"])

    def test_groups_include_given_emotions_and_populated_extras(self):
        self.store.add(HAPPY, "a.png", PNG)
        self.store.add("其他", "b.gif", GIF)
        (self.store.root / "空").mkdir()
        groups = {g["name"]: g for g in self.store.groups([HAPPY, "生气"])}
        self.assertEqual(groups[HAPPY]["count"], 1)
        self.assertEqual(groups["生气"]["count"], 0)
        self.assertEqual(groups["其他"]["count"], 1)
        self.assertNotIn("空", groups)


class StickerConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.directory.name))
        self.port = _unused_port()
        self.service = ConsoleService(create_console_app(self.store), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        await self.store.update(lambda config: config["characters"].update({
            "琴音": {
                "name": "琴音",
                "model_name": "",
                "default_emotion": "无感情",
                "emotions": {"无感情": {}, HAPPY: {}},
            }
        }) or config.update(default_character="琴音"))

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()
        self.directory.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    async def call(self, path, method="GET", payload=None, headers=None):
        kwargs = {"headers": headers or {}}
        if payload is not None:
            kwargs["json"] = payload
        async with self.client.request(method, self.url(path), **kwargs) as response:
            return response.status, await response.json()

    async def upload(self, emotion, files, headers=None):
        form = FormData()
        form.add_field("emotion", emotion)
        for name, content in files:
            form.add_field("file", content, filename=name)
        async with self.client.post(self.url("/api/stickers/upload"), data=form, headers=headers or {}) as response:
            return response.status, await response.json()

    async def test_list_upload_preview_delete(self):
        status, body = await self.call("/api/stickers")
        self.assertEqual(status, 200)
        self.assertEqual([g["name"] for g in body["groups"]], ["无感情", HAPPY])
        self.assertEqual(body["settings"], default_sticker_settings())
        self.assertEqual(body["max_bytes"], MAX_STICKER_BYTES)

        status, body = await self.upload(HAPPY, [("a.png", PNG), ("b.gif", GIF), ("c.bin", b"junk")])
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["saved"]), ["a.png", "b.gif"])
        self.assertEqual([r["filename"] for r in body["rejected"]], ["c.bin"])

        _, body = await self.call("/api/stickers")
        happy = next(g for g in body["groups"] if g["name"] == HAPPY)
        self.assertEqual(happy["count"], 2)

        for name, mime, data in (("a.png", "image/png", PNG), ("b.gif", "image/gif", GIF)):
            async with self.client.get(self.url(f"/api/stickers/file?emotion={HAPPY}&name={name}")) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], mime)
                self.assertEqual(await response.read(), data)

        status, _ = await self.call(f"/api/stickers?emotion={HAPPY}&name=a.png", "DELETE")
        self.assertEqual(status, 200)
        status, _ = await self.call(f"/api/stickers?emotion={HAPPY}&name=a.png", "DELETE")
        self.assertEqual(status, 404)

    async def test_jpeg_preview_type_is_fixed(self):
        await self.upload(HAPPY, [("p.jpeg", JPG)])
        async with self.client.get(self.url(f"/api/stickers/file?emotion={HAPPY}&name=p.jpg")) as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")

    async def test_upload_all_rejected_is_400(self):
        status, body = await self.upload(HAPPY, [("c.png", b"junk")])
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    async def test_upload_too_large_rejected(self):
        status, body = await self.upload(HAPPY, [("big.png", PNG + b"\x00" * MAX_STICKER_BYTES)])
        self.assertEqual(status, 400)
        self.assertIn("too large", body["error"])

    async def test_traversal_is_refused(self):
        status, _ = await self.upload("../escape", [("a.png", PNG)])
        self.assertEqual(status, 400)
        status, _ = await self.upload("a%2Fb", [("a.png", PNG)])
        self.assertEqual(status, 400)
        await self.upload(HAPPY, [("a.png", PNG)])
        for name in ("..%5Ca.png", "..%2Fa.png", "../a.png"):
            status, _ = await self.call(f"/api/stickers/file?emotion={HAPPY}&name={name}")
            self.assertIn(status, (400, 404), name)
            status, _ = await self.call(f"/api/stickers?emotion={HAPPY}&name={name}", "DELETE")
            self.assertIn(status, (400, 404), name)
        status, _ = await self.call("/api/stickers/file?emotion=..&name=a.png")
        self.assertEqual(status, 400)
        self.assertFalse((Path(self.directory.name) / "escape").exists())

    async def test_filename_with_path_parts_stays_in_group(self):
        status, body = await self.upload(HAPPY, [("..\\\\..\\\\x.png", PNG)])
        self.assertEqual(status, 200)
        root = Path(self.directory.name) / "stickers"
        self.assertEqual([p.parent for p in root.rglob("*") if p.is_file()], [root / HAPPY])
        self.assertTrue(body["saved"][0].endswith(".png"))

    async def test_settings_roundtrip_and_validation(self):
        status, body = await self.call("/api/stickers/settings", "POST", {"enabled": False, "prob": 55, "cooldown": 10})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.config["stickers"], {"enabled": False, "prob": 55, "cooldown": 10})
        status, _ = await self.call("/api/stickers/settings", "POST", {"prob": 101})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.config["stickers"]["prob"], 55)

    async def test_csrf_origin_checked_on_writes(self):
        evil = {"Origin": "http://evil.example"}
        status, _ = await self.call("/api/stickers/settings", "POST", {"prob": 5}, headers=evil)
        self.assertEqual(status, 403)
        status, _ = await self.upload(HAPPY, [("a.png", PNG)], headers=evil)
        self.assertEqual(status, 403)
        status, _ = await self.call(f"/api/stickers?emotion={HAPPY}&name=a.png", "DELETE", headers=evil)
        self.assertEqual(status, 403)


class FakeSendEvent:
    """Bridge event that records what is sent and when, relative to the text yield."""

    def __init__(self, base, log, fail=False):
        self._base = base
        self.log = log
        self.fail = fail
        self.sent = []
        self.session_id = "session-1"

    def __getattr__(self, name):
        return getattr(self._base, name)

    async def send(self, chain):
        self.log.append("send")
        if self.fail:
            raise RuntimeError("upload failed")
        self.sent.append(chain)


class StickerSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = _ready_plugin(self.module)
        self.config_store = ConfigStore(Path(self.directory.name))
        self.plugin._core._emotion_store = self.config_store
        self.plugin._core._sticker_store = StickerStore(Path(self.directory.name))
        self.plugin._core._sticker_random = lambda: 0.0
        self.now = 1000.0
        self.plugin._core._sticker_clock = lambda: self.now
        self.plugin._core._sticker_store.add(HAPPY, "a.png", PNG)
        self.log = []
        self.module.test_logger.infos.clear()

    async def _settings(self, **values):
        await self.config_store.update(lambda config: config["stickers"].update(values))

    async def reply(self, text, *, fail=False, event=None):
        base = _bridge_event(self.module, "hi")
        event = event or FakeSendEvent(base, self.log, fail=fail)
        task = asyncio.create_task(self._drive(event))
        request = await _wait_for_request(self.plugin._core._request_state_manager)
        manager = self.plugin._core._request_state_manager
        self.attempts = getattr(self, "attempts", 0) + 1
        manager.claim_request(request.request_id, f"attempt-{self.attempts}")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, text)
        results = await task
        return event, results

    async def _drive(self, event):
        results = []
        async for item in self.plugin.stbridge_private_message(event):
            self.log.append("text")
            results.append(item)
        return results

    def sticker_logs(self):
        return [line for line in self.module.test_logger.infos if "Sticker" in line]

    async def test_sent_after_text_as_separate_message(self):
        event, results = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(self.log, ["text", "send"])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(event.sent), 1)
        chain = event.sent[0].chain
        self.assertEqual(len(chain), 1)
        self.assertTrue(chain[0].path.endswith("a.png"))
        self.assertRegex(
            self.sticker_logs()[0],
            rf"^\[STBridge\] Sticker emotion={HAPPY} candidates=1 result=sent preupload=none "
            r"upload_ms=0 mode=event_send resume_ms=\d+$",
        )
        self.assertEqual(len(self.sticker_logs()), 1)

    async def test_sent_before_queue_release(self):
        released = []
        original = self.plugin._core._queue.release
        self.plugin._core._queue.release = lambda entry: (released.append(list(self.log)), original(entry))[1]
        await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(released, [["text", "send"]])

    async def test_no_send_when_not_applicable(self):
        cases = {
            "NoTag": "☆‹「やった」›\n\"太好了\"",
            "InvalidTag": "[情绪:../bad]☆‹「やった」›\n\"太好了\"",
            "NoImages": "[情绪:生气]☆‹「やった」›\n\"太好了\"",
            "TraversalName": "[情绪:..]☆‹「やった」›\n\"太好了\"",
        }
        for label, text in cases.items():
            self.log.clear()
            event, results = await self.reply(text)
            self.assertEqual(event.sent, [], label)
            self.assertEqual(self.log, ["text"], label)

    async def test_skip_reasons_are_logged(self):
        await self.reply("☆‹「やった」›\n\"太好了\"")
        await self.reply("[情绪:../x]☆‹「やった」›\n\"太好了\"")
        await self.reply("[情绪:生气]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(self.sticker_logs(), [
            "[STBridge] Sticker emotion=none candidates=0 result=skip:NoTag",
            "[STBridge] Sticker emotion=none candidates=0 result=skip:InvalidTag",
            "[STBridge] Sticker emotion=生气 candidates=0 result=skip:NoImages",
        ])

    async def test_disabled_switch(self):
        await self._settings(enabled=False)
        event, _ = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(event.sent, [])
        self.assertIn("skip:Disabled", self.sticker_logs()[0])

    async def test_probability_uses_injected_rng(self):
        await self._settings(prob=30)
        self.plugin._core._sticker_random = lambda: 0.31
        event, _ = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(event.sent, [])
        self.assertIn("skip:Probability", self.sticker_logs()[0])
        self.plugin._core._sticker_random = lambda: 0.29
        event, _ = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(len(event.sent), 1)

    async def test_probability_zero_and_hundred(self):
        await self._settings(prob=0)
        self.plugin._core._sticker_random = lambda: 0.0
        event, _ = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(event.sent, [])
        await self._settings(prob=100)
        self.plugin._core._sticker_random = lambda: 0.999999
        event, _ = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(len(event.sent), 1)

    async def test_cooldown_per_session(self):
        text = f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\""
        first, _ = await self.reply(text)
        self.assertEqual(len(first.sent), 1)
        self.now += 59
        second, _ = await self.reply(text)
        self.assertEqual(second.sent, [])
        self.assertIn("skip:Cooldown", self.sticker_logs()[-1])
        self.now += 2
        third, _ = await self.reply(text)
        self.assertEqual(len(third.sent), 1)
        other = FakeSendEvent(_bridge_event(self.module, "hi"), self.log)
        other.session_id = "session-2"
        self.now += 1
        fourth, _ = await self.reply(text, event=other)
        self.assertEqual(len(fourth.sent), 1)

    async def test_cooldown_zero_disables_cooldown(self):
        await self._settings(cooldown=0)
        text = f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\""
        await self.reply(text)
        event, _ = await self.reply(text)
        self.assertEqual(len(event.sent), 1)

    async def test_skipped_attempts_do_not_start_cooldown(self):
        self.plugin._core._sticker_random = lambda: 0.99
        text = f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\""
        await self.reply(text)
        self.plugin._core._sticker_random = lambda: 0.0
        event, _ = await self.reply(text)
        self.assertEqual(len(event.sent), 1)

    async def test_send_failure_is_swallowed_and_counts_for_cooldown(self):
        text = f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\""
        event, results = await self.reply(text, fail=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.log, ["text", "send"])
        self.assertRegex(
            self.sticker_logs()[0],
            rf"^\[STBridge\] Sticker emotion={HAPPY} candidates=1 result=failed:RuntimeError "
            r"preupload=none upload_ms=0 mode=event_send resume_ms=\d+$",
        )
        self.assertEqual(len(self.sticker_logs()), 1)
        self.assertEqual(self.module.test_logger.errors, [])
        again, _ = await self.reply(text)
        self.assertIn("skip:Cooldown", self.sticker_logs()[-1])
        self.assertEqual(again.sent, [])

    async def test_failure_notice_and_invalid_reply_never_get_stickers(self):
        # INVALID_REPLY: a reply without the bilingual shape
        event, results = await self.reply(f"[情绪:{HAPPY}]没有引号的回复")
        self.assertEqual(event.sent, [])
        self.assertEqual(len(results), 1)
        # failure notice
        event = FakeSendEvent(_bridge_event(self.module, "hi"), self.log)
        task = asyncio.create_task(self._drive(event))
        request = await _wait_for_request(self.plugin._core._request_state_manager)
        self.plugin._core._request_state_manager.fail_request(request.request_id, "boom")
        await task
        self.assertEqual(event.sent, [])

    async def test_no_log_leaks_paths_or_ids(self):
        await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        line = self.sticker_logs()[0]
        self.assertNotIn(self.directory.name, line)
        self.assertNotIn("a.png", line)
        self.assertNotIn("qq-user", line)

    async def test_store_unavailable_is_silent(self):
        self.plugin._core._sticker_store = None
        event, results = await self.reply(f"[情绪:{HAPPY}]☆‹「やった」›\n\"太好了\"")
        self.assertEqual(event.sent, [])
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
