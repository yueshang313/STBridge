import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_emotion,
    safe_emotion,
    safe_name,
    validate_emotion_profile,
)


class EmotionProfileValidationTests(unittest.TestCase):
    def test_empty_profile_uses_defaults(self):
        self.assertEqual(validate_emotion_profile({}), default_emotion())

    def test_invalid_profiles_fail_closed(self):
        cases = (
            None,
            [],
            {"top_k": 0},
            {"top_k": True},
            {"top_p": 1.5},
            {"temperature": "hot"},
            {"speed_factor": 3},
            {"split_sentence": "true"},
            {"prompt_text_lang": "not-a-gpt-sovits-language"},
            {"text_lang": ""},
            {"prompt_text": "x" * 2001},
            {"prompt_text": 1},
            {"ref_audio_path": "bad\x00path"},
            {"ref_audio_path": 1},
        )
        for profile in cases:
            with self.subTest(profile=profile):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_emotion_profile(profile)
                self.assertEqual(caught.exception.status, 400)

    def test_host_paths_and_languages_are_kept(self):
        profile = validate_emotion_profile({
            "ref_audio_path": r"  E:\Tools\GPT-SoVITS\ckyp\ktn_轻快.wav ",
            "prompt_text_lang": "日文",
            "text_lang": "日英混合",
            "top_k": "12",
        })
        self.assertEqual(
            profile["ref_audio_path"],
            r"E:\Tools\GPT-SoVITS\ckyp\ktn_轻快.wav",
        )
        self.assertEqual(profile["text_lang"], "日英混合")
        self.assertEqual(profile["top_k"], 12)

    def test_names_reject_path_characters(self):
        for value in ("a/b", "a\\b", "..\\x", "a:b", "", "x" * 81, None):
            with self.subTest(value=value):
                with self.assertRaises(EmotionConfigError):
                    safe_name(value, "character name")
        self.assertEqual(safe_name(" 琴音 ", "character name"), "琴音")
        self.assertEqual(safe_emotion("轻快"), "轻快")
        for value in ("../x", "a b", "", "x" * 41):
            with self.subTest(value=value):
                with self.assertRaises(EmotionConfigError):
                    safe_emotion(value)


class StarterCharacterTests(unittest.TestCase):
    def test_a_new_config_starts_with_the_default_character_and_no_private_data(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import default_config, validate_config

        config = default_config()
        validate_config(config)
        self.assertEqual(config["default_character"], "琴音")
        character = config["characters"]["琴音"]
        self.assertEqual(
            (character["name"], character["nickname"], character["st_name"], character["model_name"], character["default_emotion"]),
            ("琴音", "琴音", "藤田琴音", "", "无感情"),
        )
        self.assertEqual(
            list(character["emotions"]), ["无感情", "开心", "激动", "害羞", "惊讶", "疑惑", "认真", "生气", "伤心"]
        )
        for profile in character["emotions"].values():
            self.assertTrue(profile["caption"])
            self.assertEqual((profile["ref_audio_path"], profile["prompt_text"]), ("", ""))
        self.assertEqual(config["models"], {})

    def test_an_old_config_without_the_character_does_not_get_one(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.json").write_text(
                json.dumps({"version": 1, "default_character": "", "models": {}, "characters": {}}), encoding="utf-8"
            )
            store = ConfigStore(Path(directory))
            self.assertEqual((store.config["characters"], store.config["default_character"]), ({}, ""))


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-emotion-")
        self.data_dir = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_new_store_writes_default_config_without_token(self):
        store = ConfigStore(self.data_dir)
        stored = json.loads((self.data_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 1)
        self.assertNotIn("auth_token", stored)
        public = store.public()
        # The public view hides the music API key (key_set / key_tail instead).
        self.assertNotIn("music_api_key", public["cover"])
        self.assertEqual(public["cover"]["key_set"], False)
        for section in (public, stored):
            for name in ("music_api_key", "key_set", "key_tail"):
                section["cover"].pop(name, None)
        self.assertEqual(public, stored)
        self.assertTrue((self.data_dir / "audio").is_dir())

    def test_update_is_atomic_and_keeps_backup(self):
        store = ConfigStore(self.data_dir)

        def add_model(config):
            config["models"]["m"] = {"name": "m"}

        asyncio.run(store.update(add_model))
        self.assertTrue((self.data_dir / "config.json.bak").exists())
        self.assertFalse(list(self.data_dir.glob("config.*.tmp")))
        reloaded = ConfigStore(self.data_dir)
        self.assertIn("m", reloaded.config["models"])

    def test_failed_update_does_not_change_memory_or_disk(self):
        store = ConfigStore(self.data_dir)
        before = (self.data_dir / "config.json").read_bytes()

        def corrupt(config):
            config["characters"]["bad/name"] = {"emotions": {}}

        with self.assertRaises(EmotionConfigError):
            asyncio.run(store.update(corrupt))
        self.assertEqual((self.data_dir / "config.json").read_bytes(), before)
        self.assertEqual(set(store.config["characters"]), {"琴音"})

    def test_corrupt_or_invalid_config_is_set_aside_and_replaced_by_defaults(self):
        cases = (
            b"{ invalid json\n",
            json.dumps({"version": 2, "models": {}, "characters": {}}).encode(),
            json.dumps({
                "version": 1,
                "models": {},
                "characters": {"a/b": {"emotions": {}}},
            }).encode(),
        )
        for original in cases:
            with self.subTest(original=original):
                config_path = self.data_dir / "config.json"
                config_path.write_bytes(original)
                store = ConfigStore(self.data_dir)
                # 101e: the broken file is kept under a new name, never deleted or overwritten.
                broken = store.recovery["broken"]
                self.assertEqual(store.recovery["used"], "default")
                self.assertEqual((self.data_dir / broken[0]).read_bytes(), original)
                self.assertTrue(config_path.exists())
                self.assertFalse((self.data_dir / "config.json.bak").exists())
                config_path.unlink()
                (self.data_dir / broken[0]).unlink()

    def test_legacy_console_token_is_never_published(self):
        legacy = {
            "version": 1,
            "auth_token": "legacy-secret",
            "default_character": "",
            "default_emotion": "neutral",
            "models": {},
            "characters": {},
        }
        (self.data_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        store = ConfigStore(self.data_dir)
        self.assertNotIn("auth_token", store.public())

    def test_model_status_checks_host_files(self):
        store = ConfigStore(self.data_dir)
        gpt = self.data_dir / "weights" / "kotone.ckpt"
        gpt.parent.mkdir()
        gpt.write_bytes(b"gpt")
        status = store.model_status({
            "name": "m",
            "gpt_model_path": str(gpt),
            "sovits_model_path": str(self.data_dir / "weights" / "missing.pth"),
            "reference_audio_root": str(gpt.parent),
        })
        self.assertTrue(status["files"]["gpt_weights"])
        self.assertFalse(status["files"]["sovits_weights"])
        self.assertTrue(status["files"]["reference_audios"])
        self.assertTrue(status["valid"])

    def test_resolve_audio_rejects_paths_outside_model_root(self):
        store = ConfigStore(self.data_dir)

        def add_model(config):
            config["models"]["m"] = {"name": "m", "reference_audio_root": ""}

        asyncio.run(store.update(add_model))
        root = store.model_audio_root("m")
        (root / "Japanese").mkdir()
        (root / "Japanese" / "happy_a.wav").write_bytes(b"RIFF")
        outside = self.data_dir / "outside.wav"
        outside.write_bytes(b"RIFF")

        self.assertEqual(
            store.resolve_audio("m", "Japanese/happy_a.wav"),
            (root / "Japanese" / "happy_a.wav").resolve(),
        )
        for relative in ("../../outside.wav", "..\\..\\outside.wav", str(outside)):
            with self.subTest(relative=relative):
                with self.assertRaises(EmotionConfigError) as caught:
                    store.resolve_audio("m", relative)
                self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(EmotionConfigError) as caught:
            store.resolve_audio("m", "../../config.json")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(EmotionConfigError) as caught:
            store.resolve_audio("missing", "x.wav")
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
