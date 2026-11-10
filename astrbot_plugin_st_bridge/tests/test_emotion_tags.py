import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    parse_emotion_name,
    select_tts_profile,
    strip_emotion_tags,
)
from test_plugin_lifecycle import submodule
from test_plugin_lifecycle import (
    _bridge_event,
    _complete_bridge_reply,
    _load_plugin_module,
    _ready_plugin,
)


PROFILE_KEY = "astrbot_plugin_st_bridge.tts_profile"
TEXT_KEY = "astrbot_plugin_st_bridge.tts_japanese_text"
ELIGIBLE_KEY = "astrbot_plugin_st_bridge.tts_eligible"
PROFILE_FIELDS = {
    "ref_audio_path", "prompt_text", "prompt_text_lang", "text_lang", "top_k",
    "top_p", "temperature", "speed_factor", "split_sentence", "caption", "emoji",
}


def _profile(audio, **overrides):
    profile = {
        "ref_audio_path": audio,
        "prompt_text": "こんにちは",
        "prompt_text_lang": "日文",
        "text_lang": "日文",
        "top_k": 10,
        "top_p": 1.0,
        "temperature": 1.0,
        "speed_factor": 1.0,
        "split_sentence": True,
    }
    profile.update(overrides)
    return profile


class EmotionTagStripTests(unittest.TestCase):
    def test_tags_are_removed_everywhere_and_names_kept_in_order(self):
        cases = (
            ("[情绪:轻快]☆‹日本語›\"中文\"", ["轻快"]),
            ("☆‹日本語›\"中文\"[情绪：轻快]", ["轻快"]),
            ("☆‹日本[情绪: 轻快 ]語›\"中[情绪：悲伤]文\"", ["轻快", "悲伤"]),
            ("前[情绪：  happy\t]后☆‹日本語›\"中文\"", ["happy"]),
        )
        for reply, names in cases:
            with self.subTest(reply=reply):
                cleaned, found = strip_emotion_tags(reply)
                self.assertEqual(found, names)
                self.assertNotIn("情绪", cleaned)
                self.assertIn("☆‹", cleaned)

    def test_invalid_or_overlong_tag_names_are_still_removed(self):
        reply = '[情绪:a b][情绪:][情绪:' + "x" * 60 + ']☆‹日本語›"中文"'
        cleaned, found = strip_emotion_tags(reply)
        self.assertEqual(cleaned, '☆‹日本語›"中文"')
        self.assertEqual([parse_emotion_name(name) for name in found], [None, None, None])

    def test_non_tags_are_untouched(self):
        for text in ("[情绪]", "【情绪:轻快】", "[心情:轻快]", "[情绪-轻快]"):
            with self.subTest(text=text):
                self.assertEqual(strip_emotion_tags(text), (text, []))


class TtsProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-tags-")
        root = Path(self.directory.name)
        self.audio = {}
        for name in ("happy", "neutral", "calm"):
            path = root / f"{name}.wav"
            path.write_bytes(b"RIFF")
            self.audio[name] = str(path)
        self.missing = str(root / "missing.wav")
        self.config = {
            "version": 1,
            "default_character": "琴音",
            "default_emotion": "calm",
            "models": {
                "kotone": {
                    "name": "kotone",
                    "gpt_model_path": r"D:\GPT\ktn.ckpt",
                    "sovits_model_path": r"D:\SoVITS\ktn.pth",
                    "reference_audio_root": "",
                },
            },
            "characters": {
                "琴音": {
                    "name": "琴音",
                    "model_name": "kotone",
                    "default_emotion": "neutral",
                    "emotions": {
                        "happy": _profile(self.audio["happy"], speed_factor=1.2),
                        "neutral": _profile(self.audio["neutral"]),
                        "calm": _profile(self.audio["calm"]),
                        "broken": _profile(self.missing),
                        "empty": _profile(""),
                    },
                },
            },
        }

    def tearDown(self):
        self.directory.cleanup()

    def emotions(self):
        return self.config["characters"]["琴音"]["emotions"]

    def test_requested_emotion_is_used_with_full_profile_and_models(self):
        selection = select_tts_profile(self.config, "happy")
        self.assertEqual((selection.emotion, selection.reason), ("happy", None))
        self.assertEqual(
            set(selection.profile),
            PROFILE_FIELDS | {"emotion", "character", "source", "gpt_model_path", "sovits_model_path"},
        )
        self.assertEqual(selection.profile["emotion"], "happy")
        self.assertEqual(selection.profile["character"], "琴音")
        self.assertEqual(selection.profile["speed_factor"], 1.2)
        self.assertEqual(selection.profile["ref_audio_path"], self.audio["happy"])
        self.assertEqual(selection.profile["gpt_model_path"], r"D:\GPT\ktn.ckpt")
        self.assertEqual(selection.profile["sovits_model_path"], r"D:\SoVITS\ktn.pth")

    def test_profile_is_a_copy(self):
        selection = select_tts_profile(self.config, "happy")
        selection.profile["top_k"] = 99
        self.assertEqual(self.emotions()["happy"]["top_k"], 10)

    def test_unknown_missing_and_empty_audio_fall_back_to_character_default(self):
        for requested, reason in (
            ("unknown", "UnknownEmotion"),
            ("broken", "MissingAudio"),
            ("empty", "MissingAudio"),
            (None, "NoTag"),
        ):
            with self.subTest(requested=requested):
                selection = select_tts_profile(self.config, requested)
                self.assertEqual((selection.emotion, selection.reason), ("neutral", reason))
                self.assertEqual(selection.profile["emotion"], "neutral")

    def test_falls_back_to_global_default_then_gives_up(self):
        self.emotions()["neutral"]["ref_audio_path"] = self.missing
        selection = select_tts_profile(self.config, "unknown")
        self.assertEqual((selection.emotion, selection.reason), ("calm", "UnknownEmotion"))

        del self.emotions()["calm"]
        selection = select_tts_profile(self.config, "unknown")
        self.assertEqual(selection, type(selection)(None, None, "UnknownEmotion"))
        selection = select_tts_profile(self.config, None, no_tag_reason="InvalidTag")
        self.assertEqual(selection, type(selection)(None, None, "InvalidTag"))

    def test_missing_character_attaches_nothing(self):
        for name in ("", "nobody", None):
            with self.subTest(name=name):
                self.config["default_character"] = name
                selection = select_tts_profile(self.config, "happy")
                self.assertEqual(selection, type(selection)(None, None, "NoCharacter"))

    def test_models_are_only_included_as_a_complete_pair(self):
        self.config["models"]["kotone"]["sovits_model_path"] = ""
        profile = select_tts_profile(self.config, "happy").profile
        self.assertNotIn("gpt_model_path", profile)
        self.assertNotIn("sovits_model_path", profile)
        self.config["characters"]["琴音"]["model_name"] = "deleted-model"
        profile = select_tts_profile(self.config, "happy").profile
        self.assertNotIn("gpt_model_path", profile)


class BridgeEmotionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.logger = self.module.test_logger
        for messages in (self.logger.warnings, self.logger.errors, self.logger.infos):
            messages.clear()
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-emotion-")
        root = Path(self.directory.name)
        self.happy_audio = root / "ktn_轻快.wav"
        self.happy_audio.write_bytes(b"RIFF")
        self.neutral_audio = root / "ktn_neutral.wav"
        self.neutral_audio.write_bytes(b"RIFF")
        config = {
            "version": 1,
            "default_character": "琴音",
            "default_emotion": "neutral",
            "models": {},
            "characters": {
                "琴音": {
                    "name": "琴音",
                    "model_name": "",
                    "default_emotion": "neutral",
                    "emotions": {
                        "轻快": _profile(str(self.happy_audio)),
                        "neutral": _profile(str(self.neutral_audio)),
                    },
                },
            },
        }
        data_dir = root / "plugin_data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.store = ConfigStore(data_dir)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def plugin(self, store=True):
        plugin = _ready_plugin(self.module, voice=True)
        self.last_tts = plugin._core._tts
        plugin._core._emotion_store = self.store if store else None
        return plugin

    async def reply(self, plugin, text):
        event = _bridge_event(self.module, "request")
        results = await _complete_bridge_reply(plugin, event, text)
        return event, results

    def assert_logs_exclude(self, *secrets):
        logs = "".join(self.logger.infos + self.logger.warnings + self.logger.errors)
        for secret in secrets:
            self.assertNotIn(secret, logs)

    async def test_tag_is_removed_and_profile_attached(self):
        event, results = await self.reply(
            self.plugin(),
            '[情绪：轻快]☆‹「やった[情绪:轻快]ね」›\n"太好[情绪:轻快]了"',
        )
        self.assertEqual([result.text for result in results], ["太好了"])
        self.assertEqual(self.last_tts.last_text, "「やったね」")
        self.assertIs(self.last_tts.eligible, True)
        profile = self.last_tts.last_profile
        self.assertEqual(profile["emotion"], "轻快")
        self.assertEqual(profile["character"], "琴音")
        self.assertEqual(profile["ref_audio_path"], str(self.happy_audio))
        self.assertNotIn("gpt_model_path", profile)
        self.assertEqual(
            self.logger.warnings,
            ["[STBridge] Multiple emotion tags (count=3); using the first"],
        )
        self.assertEqual(
            self.logger.infos,
            ["[STBridge] TTS emotion requested=轻快 used=轻快 source=manual candidates=1 pick=1 "
             "fallback=False reason=none"],
        )
        self.assert_logs_exclude("やった", "太好", str(self.happy_audio), "ktn_")

    async def test_console_edits_are_seen_without_reloading(self):
        plugin = self.plugin()

        def rename(config):
            config["characters"]["琴音"]["emotions"]["happy"] = config["characters"]["琴音"]["emotions"].pop("轻快")

        await self.store.update(rename)
        event, _ = await self.reply(plugin, '[情绪:happy]☆‹日本語›"中文"')
        self.assertEqual(self.last_tts.last_profile["emotion"], "happy")

    async def test_unknown_and_missing_audio_fall_back_with_reason(self):
        self.happy_audio.unlink()
        for tag, reason in (("[情绪:未知]", "UnknownEmotion"), ("[情绪:轻快]", "MissingAudio"), ("", "NoTag")):
            with self.subTest(tag=tag):
                self.logger.infos.clear()
                event, results = await self.reply(self.plugin(), f'{tag}☆‹日本語›"中文"')
                self.assertEqual([result.text for result in results], ["中文"])
                self.assertEqual(self.last_tts.last_profile["emotion"], "neutral")
                self.assertIn(f"fallback=True reason={reason}", self.logger.infos[0])
                self.assertIn("used=neutral", self.logger.infos[0])

    async def test_invalid_tag_name_is_treated_as_no_tag(self):
        event, results = await self.reply(self.plugin(), '[情绪:a b]☆‹日本語›"中文"')
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertEqual(self.last_tts.last_profile["emotion"], "neutral")
        self.assertEqual(
            self.logger.infos,
            ["[STBridge] TTS emotion requested=none used=neutral source=manual candidates=1 pick=1 "
             "fallback=True reason=InvalidTag"],
        )

    async def test_no_usable_profile_keeps_text_and_plain_tts(self):
        self.happy_audio.unlink()
        self.neutral_audio.unlink()
        event, results = await self.reply(self.plugin(), '[情绪:轻快]☆‹日本語›"中文"')
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertIs(self.last_tts.eligible, True)
        self.assertEqual(self.last_tts.last_text, "日本語")
        self.assertIsNone(self.last_tts.last_profile)
        self.assertEqual(
            self.logger.infos,
            ["[STBridge] TTS emotion requested=轻快 used=none source=none candidates=0 pick=0 "
             "fallback=True reason=MissingAudio"],
        )

    async def test_missing_store_degrades_to_no_profile(self):
        event, results = await self.reply(self.plugin(store=False), '[情绪:轻快]☆‹日本語›"中文"')
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertIs(self.last_tts.eligible, True)
        self.assertIsNone(self.last_tts.last_profile)
        self.assertEqual(
            self.logger.infos,
            ["[STBridge] TTS emotion requested=轻快 used=none source=none candidates=0 pick=0 "
             "fallback=True reason=StoreUnavailable"],
        )

    async def test_selection_errors_never_block_text_reply(self):
        with patch.object(submodule(self.module, "mixin_tts"), "select_tts_profile", side_effect=RuntimeError("secret path")):
            event, results = await self.reply(self.plugin(), '[情绪:轻快]☆‹日本語›"中文"')
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertIs(self.last_tts.eligible, True)
        self.assertIsNone(self.last_tts.last_profile)
        self.assertEqual(self.logger.warnings, ["[STBridge] TTS profile unavailable (OtherError)"])
        self.assert_logs_exclude("secret path")

    async def test_bilingual_validation_still_applies_after_stripping(self):
        for reply in (
            "[情绪:轻快] only a tag",
            '[情绪:轻快]☆‹日本語›',
            '[情绪:轻快]"中文"',
            '[情绪:轻快]☆‹一›☆‹二›"中文"',
            '[情绪:"轻快"]☆‹日本語›"中文"',
        ):
            with self.subTest(reply=reply):
                self.logger.warnings.clear()
                self.logger.infos.clear()
                event, results = await self.reply(self.plugin(), reply)
                if reply.startswith('[情绪:"'):
                    # The quoted tag is stripped whole, leaving a valid bilingual reply.
                    self.assertEqual([result.text for result in results], ["中文"])
                    continue
                self.assertEqual(
                    [result.text for result in results],
                    [self.module.INVALID_REPLY_TEXT],
                )
                self.assertIsNone(self.last_tts.eligible)
                self.assertIsNone(self.last_tts.last_profile)
                self.assertEqual(
                    self.logger.warnings,
                    ["[STBridge] Reply rejected (InvalidBilingualReply)"],
                )
                self.assertEqual(self.logger.infos, [])


class EmotionStoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_failure_disables_console_and_profiles_only(self):
        module = _load_plugin_module()
        module.test_logger.errors.clear()

        class FakeBridgeService:
            def __init__(self, app, config, gate):
                self.gate = gate
                self.state = module.HttpServiceState.NEW

            async def start(self):
                self.state = module.HttpServiceState.RUNNING

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = module.HttpServiceState.STOPPED

        def failing_data_dir(_name):
            raise RuntimeError("no data dir")

        started = []
        plugin = module.STBridgePlugin(context=None, config={"bridge_token": "a" * 43})
        with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FakeBridgeService), \
                patch.object(submodule(module, "mixin_lifecycle"), "ConsoleService", lambda *a, **k: started.append(1)), \
                patch.object(module.star, "StarTools", type("Tools", (), {
                    "get_data_dir": staticmethod(failing_data_dir),
                }), create=True):
            await plugin.initialize()
        self.assertIsNone(plugin._core._emotion_store)
        self.assertEqual(started, [])
        self.assertEqual(
            module.test_logger.errors,
            [
                "[STBridge] Console unavailable (ConsoleStartFailed: RuntimeError)",
                "[STBridge] Affinity unavailable (AffinityStoreFailed: RuntimeError)",
            ],
        )
        self.assertIsNone(plugin._core._affinity_store)
        self.assertEqual(plugin._core._http_service.state, module.HttpServiceState.RUNNING)
        await plugin.terminate()

    async def test_console_and_lookup_share_one_store(self):
        module = _load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            apps = []

            class FakeConsole:
                def __init__(self, app):
                    apps.append(app)

                async def start(self):
                    pass

                async def stop(self):
                    pass

            class FakeBridgeService:
                def __init__(self, app, config, gate):
                    self.gate = gate
                    self.state = module.HttpServiceState.NEW

                async def start(self):
                    self.state = module.HttpServiceState.RUNNING

                async def stop(self):
                    self.state = module.HttpServiceState.STOPPED

            plugin = module.STBridgePlugin(context=None, config={"bridge_token": "a" * 43})
            with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FakeBridgeService), \
                    patch.object(submodule(module, "mixin_lifecycle"), "ConsoleService", FakeConsole), \
                    patch.object(module.star, "StarTools", type("Tools", (), {
                        "get_data_dir": staticmethod(lambda _name: Path(directory)),
                    }), create=True):
                await plugin.initialize()
                console_module = __import__(
                    "data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server",
                    fromlist=["_STORE_KEY"],
                )
                self.assertIs(apps[0][console_module._STORE_KEY], plugin._core._emotion_store)
                await plugin.terminate()
            self.assertIsNone(plugin._core._emotion_store)


if __name__ == "__main__":
    unittest.main()
