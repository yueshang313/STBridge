import random
import unittest

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import emotion_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    EmotionConfigError,
    default_emotion,
    select_tts_profile,
    validate_config,
    validate_emotion_profile,
)
from test_discovery_and_browser import TempDirTestCase, write_wav


class StyleFieldValidationTests(unittest.TestCase):
    def test_defaults_are_empty_strings(self):
        profile = default_emotion()
        self.assertEqual((profile["caption"], profile["emoji"]), ("", ""))

    def test_valid_style_is_kept_and_stripped(self):
        profile = validate_emotion_profile({"caption": "  嬉しそうに話している。 ", "emoji": " 😊 "})
        self.assertEqual((profile["caption"], profile["emoji"]), ("嬉しそうに話している。", "😊"))

    def test_limits_are_enforced(self):
        validate_emotion_profile({"caption": "あ" * 300, "emoji": "😊" * 16})
        for bad in (
            {"caption": "あ" * 301},
            {"caption": 1},
            {"caption": None},
            {"emoji": "😊" * 17},
            {"emoji": "😊\n😆"},
            {"emoji": "a\rb"},
            {"emoji": 5},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_emotion_profile(bad)
                self.assertEqual(caught.exception.status, 400)

    def test_config_written_before_style_fields_still_loads(self):
        old_profile = {k: v for k, v in default_emotion().items() if k not in ("caption", "emoji")}
        config = emotion_config.default_config()
        config["characters"] = {"琴音": {"emotions": {"开心": old_profile}}}
        validate_config(config)
        migrated = validate_emotion_profile(old_profile)
        self.assertEqual((migrated["caption"], migrated["emoji"]), ("", ""))


class StyleSelectionTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.ktn = self.root / "ktn"
        write_wav(self.ktn / "开心" / "嬉しい.wav", 4)
        write_wav(self.ktn / "认真" / "真面目です.wav", 5)
        (self.ktn / "伤心").mkdir()
        self.manual_audio = write_wav(self.root / "manual.wav", 5)
        self.config = {
            "version": 1,
            "default_character": "琴音",
            "default_emotion": "认真",
            "models": {"ktn": {"name": "ktn", "reference_audio_root": str(self.ktn)}},
            "characters": {
                "琴音": {"name": "琴音", "model_name": "ktn", "default_emotion": "认真", "emotions": {}},
            },
        }

    def emotions(self):
        return self.config["characters"]["琴音"]["emotions"]

    def test_style_only_profile_is_selected_without_audio(self):
        self.emotions()["伤心"] = validate_emotion_profile({"emoji": "🥺", "caption": "悲しそうに話している。"})
        selection = select_tts_profile(self.config, "伤心", rng=random.Random(1))
        self.assertEqual(
            (selection.emotion, selection.reason, selection.source, selection.candidates, selection.pick),
            ("伤心", None, "style", 0, 0),
        )
        self.assertEqual(selection.profile["source"], "style")
        self.assertEqual(selection.profile["ref_audio_path"], "")
        self.assertEqual(selection.profile["emoji"], "🥺")
        self.assertEqual(selection.profile["caption"], "悲しそうに話している。")
        self.assertEqual(selection.profile["character"], "琴音")

    def test_style_works_for_unknown_folder_and_caption_or_emoji_alone(self):
        self.emotions()["疑惑"] = validate_emotion_profile({"caption": "不思議そうに話している。"})
        self.emotions()["害羞"] = validate_emotion_profile({"emoji": "🫣"})
        for name in ("疑惑", "害羞"):
            selection = select_tts_profile(self.config, name)
            self.assertEqual((selection.emotion, selection.source), (name, "style"))

    def test_manual_audio_and_directory_win_over_style(self):
        self.emotions()["开心"] = validate_emotion_profile(
            {"ref_audio_path": str(self.manual_audio), "emoji": "😊", "caption": "楽しげ"}
        )
        manual = select_tts_profile(self.config, "开心")
        self.assertEqual(manual.source, "manual")
        self.assertEqual((manual.profile["emoji"], manual.profile["caption"]), ("😊", "楽しげ"))

        self.emotions()["开心"] = validate_emotion_profile({"emoji": "😊", "caption": "楽しげ"})
        directory = select_tts_profile(self.config, "开心", rng=random.Random(1))
        self.assertEqual((directory.source, directory.candidates, directory.pick), ("directory", 1, 1))
        self.assertEqual((directory.profile["emoji"], directory.profile["caption"]), ("😊", "楽しげ"))

    def test_directory_profile_without_manual_has_empty_style(self):
        selection = select_tts_profile(self.config, "开心", rng=random.Random(1))
        self.assertEqual((selection.profile["emoji"], selection.profile["caption"]), ("", ""))

    def test_manual_without_audio_or_style_still_falls_back(self):
        self.emotions()["伤心"] = validate_emotion_profile({})
        selection = select_tts_profile(self.config, "伤心", rng=random.Random(1))
        self.assertEqual((selection.emotion, selection.reason, selection.source), ("认真", "MissingAudio", "directory"))

    def test_style_candidate_order_follows_the_fallback_chain(self):
        self.emotions()["认真"] = validate_emotion_profile({"caption": "真剣に話している。"})
        # the requested emotion is unknown; the default emotion has audio on disk, so it is a directory pick
        selection = select_tts_profile(self.config, "不存在", rng=random.Random(1))
        self.assertEqual((selection.emotion, selection.reason, selection.source), ("认真", "UnknownEmotion", "directory"))
        self.assertEqual(selection.profile["caption"], "真剣に話している。")

    def test_style_selection_does_not_use_discovery_result_for_audio_check(self):
        self.emotions()["伤心"] = validate_emotion_profile({"emoji": "🥺"})
        calls = []

        def fake_discover(root):
            calls.append(root)
            return {}

        selection = select_tts_profile(self.config, "伤心", discover=fake_discover)
        self.assertEqual(selection.source, "style")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
