"""U5-1: the checklist's flags (console.onboarding) and the test chat that sets one of them."""

import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.config_presets import normalize_config, snapshot
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    console_settings,
    default_config,
    default_console_settings,
    validate_config,
    validate_console_settings,
)
from test_console_server import _unused_loopback_port

ALL_OFF = {"tested": False, "voice_skipped": False, "hidden": False, "completed": False}


class OnboardingSettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(default_console_settings(), {"onboarding": ALL_OFF})
        self.assertEqual(default_config()["console"], {"onboarding": ALL_OFF})
        self.assertEqual(validate_console_settings({}), {"onboarding": ALL_OFF})
        self.assertEqual(validate_console_settings({"onboarding": {"tested": True}})["onboarding"]["tested"], True)
        for bad in ([], {"onboarding": []}, {"onboarding": {"tested": "yes"}}, {"onboarding": {"nonsense": True}}, {"onboarding": {"tested": 1}}):
            with self.subTest(bad=bad), self.assertRaises(EmotionConfigError):
                validate_console_settings(bad)
        self.assertEqual(console_settings({}), default_console_settings())
        self.assertEqual(console_settings({"console": {"onboarding": {"tested": 5}}}), default_console_settings())

    def test_an_old_config_without_the_key_gets_the_defaults_and_a_bad_one_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = default_config()
            del old["console"]
            (root / "config.json").write_text(json.dumps(old), encoding="utf-8")
            self.assertEqual(ConfigStore(root).config["console"], {"onboarding": ALL_OFF})
        broken = default_config()
        broken["console"] = {"onboarding": {"tested": "yes"}}
        with self.assertRaises(EmotionConfigError):
            validate_config(broken)

    def test_a_preset_neither_carries_nor_changes_the_flags(self):
        config = default_config()
        config["console"]["onboarding"]["tested"] = True
        self.assertNotIn("console", snapshot(config))
        local = default_config()
        local["console"]["onboarding"]["voice_skipped"] = True
        applied = normalize_config(snapshot(default_config()), local)
        self.assertEqual(applied["console"], local["console"])


class OnboardingApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConfigStore(Path(self.directory.name))
        self.outcome = {"result": "ok", "replies": ["你好"]}

        async def runner(body):
            return self.outcome

        self.port = _unused_loopback_port()
        self.service = ConsoleService(create_console_app(self.store, test_chat=runner), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", payload=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload) as response:
            return response.status, await response.json()

    async def test_read_and_update_the_flags(self):
        self.assertEqual(await self.call("/api/onboarding"), (200, {"onboarding": ALL_OFF}))
        status, body = await self.call("/api/onboarding", "POST", {"voice_skipped": True})
        self.assertEqual((status, body["onboarding"]["voice_skipped"], body["onboarding"]["tested"]), (200, True, False))
        self.assertEqual(self.store.config["console"]["onboarding"]["voice_skipped"], True)
        status, body = await self.call("/api/onboarding", "POST", {"voice_skipped": False, "hidden": True})
        self.assertEqual((body["onboarding"]["voice_skipped"], body["onboarding"]["hidden"]), (False, True))
        for bad in ({}, {"tested": "yes"}, {"nonsense": True}, [], {"hidden": 1}):
            status, _ = await self.call("/api/onboarding", "POST", bad)
            self.assertEqual(status, 400, bad)

    async def test_config_defaults_are_the_factory_settings_and_hold_no_user_data(self):
        self.store.config["owner"] = {"platform_id": "p1", "sender_id": "s1"}
        self.store.config["install"]["sillytavern"] = "C:/mine/ST"
        status, body = await self.call("/api/config/defaults")
        self.assertEqual(status, 200)
        fresh = default_config()
        self.assertEqual(set(body), set(fresh))
        for key in ("queue", "tts", "stickers", "schedule", "texts", "network", "affinity", "mood", "st_injection", "song_request", "cleanup", "chat_platform"):
            self.assertEqual(body[key], fresh[key], key)
        self.assertEqual(body["owner"], fresh["owner"])
        self.assertEqual(body["install"], fresh["install"])
        self.assertEqual(body["models"], {})
        self.assertNotIn("C:/mine", json.dumps(body))
        self.assertNotIn("auth_token", body)
        status, _ = await self.call("/api/config/defaults", "POST", {"character": "", "emotion": "neutral"})
        self.assertEqual(status, 200)

    async def test_a_good_test_chat_marks_the_test_step_done(self):
        status, _ = await self.call("/api/test/chat", "POST", {"chat": "private", "text": "hi"})
        self.assertEqual(status, 200)
        self.assertTrue(self.store.config["console"]["onboarding"]["tested"])

    async def test_a_failed_or_empty_test_chat_does_not(self):
        for outcome in ({"result": "Timeout", "replies": []}, {"result": "ok", "replies": []}, {"result": "StFailed", "replies": ["x"]}):
            self.outcome = outcome
            status, _ = await self.call("/api/test/chat", "POST", {"chat": "private", "text": "hi"})
            self.assertEqual(status, 200)
            self.assertFalse(self.store.config["console"]["onboarding"]["tested"], outcome)


if __name__ == "__main__":
    unittest.main()
