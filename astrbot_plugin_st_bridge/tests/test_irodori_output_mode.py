"""V1c: Irodori output_mode (qq_24k default / native) in settings, presets and the apply path."""

import asyncio
import copy
import unittest

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.audio_format import voice_bytes_for_send
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    EmotionConfigError,
    default_config,
    default_tts_settings,
    validate_tts_settings,
)
from test_tts_coordinator import CoordinatorCase


class FakeTts:
    def __init__(self, settings):
        self.settings = settings


class SettingsTests(unittest.TestCase):
    def test_default_and_both_values(self):
        self.assertEqual(default_tts_settings()["irodori"]["output_mode"], "qq_24k")
        for mode in ("qq_24k", "native"):
            self.assertEqual(validate_tts_settings({"irodori": {"output_mode": mode}})["irodori"]["output_mode"], mode)

    def test_other_values_are_a_400(self):
        for bad in ("48k", "", "NATIVE", None, 24000, True, ["native"]):
            with self.subTest(bad=bad):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_tts_settings({"irodori": {"output_mode": bad}})
                self.assertEqual(caught.exception.status, 400)

    def test_an_old_irodori_section_gets_the_default(self):
        old = default_tts_settings()
        del old["irodori"]["output_mode"]
        self.assertEqual(validate_tts_settings(old)["irodori"]["output_mode"], "qq_24k")

    def test_an_old_preset_gets_the_default_and_a_saved_native_is_kept(self):
        old = default_config()
        del old["tts"]["irodori"]["output_mode"]
        self.assertEqual(config_presets.normalize_config(old, default_config())["tts"]["irodori"]["output_mode"], "qq_24k")
        native = default_config()
        native["tts"]["irodori"]["output_mode"] = "native"
        self.assertEqual(config_presets.normalize_config(native, default_config())["tts"]["irodori"]["output_mode"], "native")


class HelperTests(unittest.TestCase):
    def test_only_irodori_native_skips_the_conversion(self):
        garbage = b"not-a-wav"  # converting garbage returns it unchanged too, so use a probe on the call
        calls = []
        import data.plugins.astrbot_plugin_st_bridge.st_bridge_core.audio_format as module

        original = module.to_qq_voice_wav
        module.to_qq_voice_wav = lambda data, logger=None: calls.append(data) or b"converted"
        try:
            cases = (
                ({"engine": "irodori", "irodori": {"output_mode": "native"}}, garbage),
                ({"engine": "irodori", "irodori": {"output_mode": "qq_24k"}}, b"converted"),
                ({"engine": "gpt_sovits", "irodori": {"output_mode": "native"}}, b"converted"),
                ({"engine": "irodori"}, b"converted"),
            )
            for settings, expected in cases:
                with self.subTest(settings=settings):
                    self.assertEqual(voice_bytes_for_send(garbage, FakeTts(settings)), expected)
            self.assertEqual(voice_bytes_for_send(garbage, object()), b"converted")
        finally:
            module.to_qq_voice_wav = original


class NoRestartTests(CoordinatorCase):
    def started(self, tts):
        async def go():
            await tts.manager().ensure_started()

        asyncio.run(go())

    def test_changing_only_the_output_mode_keeps_the_running_service(self):
        tts = self.make("irodori")
        self.started(tts)
        manager = tts.manager()
        new = copy.deepcopy(tts.settings)
        new["irodori"]["output_mode"] = "native"
        self.assertEqual(asyncio.run(tts.apply_settings(new)), {"stopped": []})
        self.assertEqual(self.host.kills, [])
        self.assertIs(tts.manager(), manager)
        self.assertEqual(tts.settings["irodori"]["output_mode"], "native")


if __name__ == "__main__":
    unittest.main()
