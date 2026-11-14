import copy
import json
import tempfile
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_config,
    default_tts_settings,
    tts_settings,
    validate_config,
    validate_tts_settings,
)


class TtsDefaultsTests(unittest.TestCase):
    def test_defaults_match_the_old_plugin_schema(self):
        settings = default_tts_settings()
        self.assertEqual(settings["engine"], "irodori")
        self.assertEqual(
            settings["common"],
            {
                "auto_start": True, "stop_on_exit": True, "start_timeout": 180,
                "tts_timeout_minutes": 5, "voice_wait_seconds": 60, "prob": 100.0, "text_limit": 0, "cooldown": 0,
                "send_text_with_audio": True,
                "split_messages": True, "split_interval": 1.5, "split_max": 3,
            },
        )
        irodori = settings["irodori"]
        self.assertEqual((irodori["host"], irodori["port"]), ("127.0.0.1", 8088))
        self.assertEqual(irodori["launch_dir"], "")
        self.assertEqual(irodori["server_dir"], "")
        self.assertEqual(irodori["hf_checkpoint"], "Aratako/Irodori-TTS-v4.1-Small")
        self.assertEqual(irodori["lora_adapter"], "")
        self.assertEqual((irodori["num_steps"], irodori["cfg_scale_text"], irodori["cfg_scale_caption"]), (0, 3.0, 4.0))
        self.assertEqual(irodori["default_caption"], "")
        gpt = settings["gpt_sovits"]
        self.assertEqual((gpt["host"], gpt["port"]), ("127.0.0.1", 9880))
        self.assertEqual(gpt["launch_dir"], "")
        self.assertEqual((gpt["prompt_text_lang"], gpt["text_lang"]), ("中文", "中英混合"))
        self.assertEqual(
            (gpt["top_k"], gpt["top_p"], gpt["temperature"], gpt["speed_factor"], gpt["split_sentence"]),
            (10, 1.0, 1.0, 1.0, True),
        )

    def test_defaults_validate_and_are_idempotent(self):
        defaults = default_tts_settings()
        self.assertEqual(validate_tts_settings(defaults), defaults)
        self.assertEqual(validate_tts_settings({}), defaults)

    def test_default_config_carries_a_tts_section(self):
        config = default_config()
        self.assertEqual(config["tts"], default_tts_settings())
        validate_config(config)


class TtsValidationTests(unittest.TestCase):
    def merged(self, **sections):
        settings = default_tts_settings()
        for name, values in sections.items():
            if isinstance(values, dict):
                settings[name].update(values)
            else:
                settings[name] = values
        return settings

    def assert_rejected(self, settings):
        with self.assertRaises(EmotionConfigError) as caught:
            validate_tts_settings(settings)
        self.assertEqual(caught.exception.status, 400)

    def test_partial_sections_take_defaults(self):
        result = validate_tts_settings({"engine": "gpt_sovits", "common": {"prob": 50}, "irodori": {"port": 9000}})
        self.assertEqual(result["engine"], "gpt_sovits")
        self.assertEqual(result["common"]["prob"], 50)
        self.assertEqual(result["common"]["start_timeout"], 180)
        self.assertEqual(result["irodori"]["port"], 9000)
        self.assertEqual(result["irodori"]["host"], "127.0.0.1")

    def test_engine_must_be_known(self):
        for engine in ("GPT", "", None, 1):
            with self.subTest(engine=engine):
                self.assert_rejected(self.merged(engine=engine))

    def test_sections_must_be_objects(self):
        self.assert_rejected(None)
        self.assert_rejected([])
        for name in ("common", "irodori", "gpt_sovits"):
            with self.subTest(section=name):
                self.assert_rejected(self.merged(**{name: "x"}))

    def test_common_ranges_and_types(self):
        for field, value in (
            ("auto_start", "yes"), ("stop_on_exit", 1), ("send_text_with_audio", None),
            ("start_timeout", 29), ("start_timeout", 601), ("tts_timeout_minutes", 2),
            ("tts_timeout_minutes", 16), ("prob", -1), ("prob", 100.5), ("text_limit", -1),
            ("cooldown", -1), ("cooldown", 3601), ("prob", True), ("start_timeout", "soon"),
        ):
            with self.subTest(field=field, value=value):
                self.assert_rejected(self.merged(common={field: value}))
        ok = validate_tts_settings(self.merged(common={"start_timeout": 30, "tts_timeout_minutes": 15, "prob": 0}))
        self.assertEqual((ok["common"]["start_timeout"], ok["common"]["tts_timeout_minutes"]), (30, 15))

    def test_irodori_validation(self):
        for field, value in (
            ("host", "a/b"), ("host", "user@host"), ("host", "a b"), ("host", 5),
            ("port", 0), ("port", 65536), ("port", True),
            ("launch_dir", "x" * 501), ("launch_dir", "a\nb"), ("launch_dir", 3),
            ("hf_checkpoint", "x" * 201), ("lora_adapter", "bad\x00path"),
            ("num_steps", -1), ("num_steps", 201), ("cfg_scale_text", -0.1), ("cfg_scale_caption", 21),
            ("default_caption", "あ" * 301), ("default_caption", 5),
        ):
            with self.subTest(field=field, value=value):
                self.assert_rejected(self.merged(irodori={field: value}))

    def test_irodori_blank_required_text_falls_back_and_optional_may_be_empty(self):
        result = validate_tts_settings(self.merged(irodori={
            "host": "  ", "launch_dir": "", "server_dir": " ", "hf_checkpoint": "", "lora_adapter": "  ",
        }))["irodori"]
        defaults = default_tts_settings()["irodori"]
        for key in ("host", "launch_dir", "server_dir", "hf_checkpoint"):
            self.assertEqual(result[key], defaults[key])
        self.assertEqual(result["lora_adapter"], "")

    def test_gpt_sovits_validation(self):
        for field, value in (
            ("port", 0), ("top_k", 0), ("top_k", 101), ("top_p", 1.1), ("temperature", 2.1),
            ("speed_factor", 0.1), ("speed_factor", 2.5), ("split_sentence", "true"),
            ("prompt_text", "x" * 2001), ("prompt_text", 1), ("prompt_text_lang", "klingon"),
            ("text_lang", ""), ("ref_audio_path", "a\nb"),
        ):
            with self.subTest(field=field, value=value):
                self.assert_rejected(self.merged(gpt_sovits={field: value}))

    def test_gpt_sovits_languages_accept_old_aliases_and_api_labels(self):
        for language in ("日语", "英语", "韩语", "日文", "中英混合", "all_ja"):
            with self.subTest(language=language):
                result = validate_tts_settings(self.merged(gpt_sovits={"text_lang": language, "prompt_text_lang": language}))
                self.assertEqual(result["gpt_sovits"]["text_lang"], language)

    def test_model_paths_must_be_set_together(self):
        self.assert_rejected(self.merged(gpt_sovits={"gpt_model_path": r"D:\a.ckpt"}))
        self.assert_rejected(self.merged(gpt_sovits={"sovits_model_path": r"D:\a.pth"}))
        both = validate_tts_settings(self.merged(gpt_sovits={
            "gpt_model_path": r" D:\a.ckpt ", "sovits_model_path": r"D:\a.pth",
        }))["gpt_sovits"]
        self.assertEqual((both["gpt_model_path"], both["sovits_model_path"]), (r"D:\a.ckpt", r"D:\a.pth"))

    def test_input_is_not_mutated(self):
        settings = self.merged(common={"prob": 20})
        before = copy.deepcopy(settings)
        validate_tts_settings(settings)
        self.assertEqual(settings, before)


class TtsConfigCompatibilityTests(unittest.TestCase):
    def test_config_without_tts_section_loads_with_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config = default_config()
            del config["tts"]
            (Path(directory) / "config.json").write_text(json.dumps(config), encoding="utf-8")
            store = ConfigStore(Path(directory))
            self.assertEqual(store.config["tts"], default_tts_settings())

    def test_partial_tts_section_is_completed_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            config = default_config()
            config["tts"] = {"engine": "gpt_sovits", "common": {"prob": 10}}
            (Path(directory) / "config.json").write_text(json.dumps(config), encoding="utf-8")
            store = ConfigStore(Path(directory))
            self.assertEqual(store.config["tts"]["engine"], "gpt_sovits")
            self.assertEqual(store.config["tts"]["common"]["prob"], 10)
            self.assertEqual(store.config["tts"]["irodori"], default_tts_settings()["irodori"])

    def test_invalid_tts_section_makes_validate_config_fail(self):
        config = default_config()
        config["tts"]["engine"] = "nope"
        with self.assertRaises(EmotionConfigError):
            validate_config(config)

    def test_tts_settings_helper_falls_back_to_defaults(self):
        self.assertEqual(tts_settings({}), default_tts_settings())
        self.assertEqual(tts_settings({"tts": {"engine": "nope"}}), default_tts_settings())
        self.assertEqual(tts_settings(None), default_tts_settings())
        self.assertEqual(tts_settings({"tts": {"engine": "gpt_sovits"}})["engine"], "gpt_sovits")

    def test_existing_config_values_survive_a_save_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            self.assertEqual(json.loads((Path(directory) / "config.json").read_text(encoding="utf-8"))["tts"],
                             default_tts_settings())


if __name__ == "__main__":
    unittest.main()
