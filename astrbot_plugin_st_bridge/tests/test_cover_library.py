"""P7a: cover settings, RVC folder scanning, song library and cover cache."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import (
    DEREVERB_MODELS,
    F0_METHODS,
    RESERVED_PORTS,
    SEPARATION_MODELS,
    auto_index,
    cover_settings,
    default_cover_settings,
    list_models,
    list_separation_models,
    model_stem,
    resolve_index,
    validate_cover_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_library import (
    SONG_MAX_BYTES,
    CoverCache,
    SongLibrary,
    audio_content_type,
    cache_key,
    detect_song_suffix,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
MP3_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 64
FLAC = b"fLaC" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 32
OGG = b"OggS" + b"\x00" * 64


class CoverSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = default_cover_settings()
        self.assertEqual(settings["rvc_root"], "")
        self.assertEqual(settings["port"], 8099)
        self.assertNotIn(settings["port"], RESERVED_PORTS)
        self.assertEqual(settings["model"], "")
        self.assertEqual(
            (settings["sep_model"], settings["dereverb_model"], settings["vocal_reverb"], settings["keep_stems"]),
            ("vocals-bs-roformer-368", "dereverb-less-aggressive-18.8050", 25, False),
        )
        self.assertNotIn("uvr_model", settings)
        self.assertNotIn("filter_radius", settings)
        self.assertEqual(
            (settings["f0_method"], settings["index_rate"], settings["rms_mix_rate"], settings["protect"]),
            ("rmvpe", 0.75, 0.25, 0.33),
        )
        self.assertEqual(
            (settings["clip_seconds"], settings["default_start_percent"], settings["idle_stop_minutes"], settings["job_timeout_seconds"]),
            (60, 30, 10, 600),
        )
        self.assertEqual(validate_cover_settings({}), settings)

    def test_valid_changes(self):
        settings = validate_cover_settings({"f0_up_key": -12, "clip_seconds": 0, "port": 9100, "model": "", "enabled": False})
        self.assertEqual((settings["f0_up_key"], settings["clip_seconds"], settings["port"]), (-12, 0, 9100))
        self.assertEqual(settings["model"], "")
        self.assertFalse(settings["enabled"])
        self.assertEqual(validate_cover_settings({"rvc_root": '  "E:\\RVC"  '})["rvc_root"], "E:\\RVC")

    def test_invalid_values(self):
        bad = [
            {"enabled": "yes"}, {"port": 80}, {"port": 70000}, {"port": 8088}, {"port": 11452}, {"port": 9880},
            {"port": 8099.5}, {"port": True}, {"rvc_root": "x" * 300}, {"rvc_root": 5},
            {"model": "../x.pth"}, {"model": "a/b.pth"}, {"model": "x.bin"}, {"model": "a\\b.pth"},
            {"index_path": 5}, {"f0_up_key": 13}, {"f0_up_key": -13}, {"f0_up_key": 1.5}, {"f0_method": "magic"},
            {"index_rate": 1.5}, {"index_rate": -0.1}, {"rms_mix_rate": 2}, {"f0_method": "pm2"}, {"vocal_reverb": 101}, {"vocal_reverb": -1}, {"vocal_reverb": 2.5},
            {"protect": 0.51}, {"sep_model": ""}, {"sep_model": "HP5_only_main_vocal"}, {"sep_model": 5}, {"dereverb_model": "x"}, {"dereverb_model": "vocals-bs-roformer-368"}, {"keep_stems": "yes"}, {"clip_seconds": 601}, {"clip_seconds": -1},
            {"default_start_percent": 91}, {"vocal_gain_db": 31}, {"inst_gain_db": -31},
            {"idle_stop_minutes": -1}, {"idle_stop_minutes": 1441}, {"job_timeout_seconds": 29}, {"job_timeout_seconds": 3601},
        ]
        for value in bad:
            with self.assertRaises(EmotionConfigError, msg=str(value)):
                validate_cover_settings(value)
        with self.assertRaises(EmotionConfigError):
            validate_cover_settings([])

    def test_fallback_for_broken_config(self):
        self.assertEqual(cover_settings({"cover": {"port": 1}}), default_cover_settings())
        self.assertEqual(cover_settings(None), default_cover_settings())

    def test_config_store_adds_the_section_to_old_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            ConfigStore(Path(directory))
            path = Path(directory) / "config.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["cover"]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(ConfigStore(Path(directory)).config["cover"], default_cover_settings())

    def test_invalid_cover_section_is_refused_on_save(self):
        async def scenario(directory):
            store = ConfigStore(Path(directory))
            await store.update(lambda config: config["cover"].update(port=80))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Exception):
                asyncio.run(scenario(directory))


class RvcScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        weights = self.root / "assets" / "weights"
        weights.mkdir(parents=True)
        for name in ("b_model.pth", "kotoneV2_e150_s15450.pth", "kotoneV2_e120_s12360.pth", "notes.txt"):
            (weights / name).write_bytes(b"x")

    def test_models_are_scanned_without_the_worker(self):
        self.assertEqual(list_models(str(self.root)), ["b_model.pth", "kotoneV2_e120_s12360.pth", "kotoneV2_e150_s15450.pth"])
        self.assertEqual(list_models(str(self.root / "missing")), [])

    def test_separation_models_are_marked_available_by_their_checkpoint(self):
        weights = self.root / "assets" / "pymss_weights"
        weights.mkdir(parents=True)
        (weights / SEPARATION_MODELS["vocals-bs-roformer-368"][1]).write_bytes(b"x")
        (weights / DEREVERB_MODELS["dereverb-anvuew-19.1729"][1]).write_bytes(b"x")
        (weights / "other.ckpt").write_bytes(b"x")
        found = list_separation_models(str(self.root))
        self.assertEqual(
            [(item["id"], item["available"]) for item in found["separation"]],
            [("vocals-bs-roformer-368", True), ("vocals-bs-roformer-317", False)],
        )
        self.assertEqual(
            [(item["id"], item["available"]) for item in found["dereverb"]],
            [("dereverb-less-aggressive-18.8050", False), ("dereverb-anvuew-19.1729", True)],
        )
        self.assertEqual(found["separation"][0]["label"], "去伴奏")
        self.assertEqual(found["dereverb"][0]["label"], "去混响")
        missing = list_separation_models(str(self.root / "missing"))
        self.assertFalse(any(item["available"] for item in missing["separation"] + missing["dereverb"]))

    def test_model_tables_match_the_worker_whitelists(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "st_bridge_core" / "cover_worker" / "rvc_cover_worker.py"
        spec = importlib.util.spec_from_file_location("worker_for_tables", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(tuple(SEPARATION_MODELS), module.SEPARATION_MODELS)
        self.assertEqual(tuple(DEREVERB_MODELS), module.DEREVERB_MODELS)
        self.assertEqual(F0_METHODS, module.F0_METHODS)

    def test_model_stem(self):
        self.assertEqual(model_stem("kotoneV2_e150_s15450.pth"), "kotoneV2")
        self.assertEqual(model_stem("kotoneV2_e120_s12360.pth"), "kotoneV2")
        self.assertEqual(model_stem("huahuo.pth"), "huahuo")
        self.assertEqual(model_stem("a_e1_s2_extra.pth"), "a_e1_s2_extra")

    def make_index(self, folder, name, age=0, base="logs"):
        directory = self.root / Path(*base.split("/")) / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(b"i")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_auto_index_finds_the_models_added_index_under_logs(self):
        expected = self.make_index("kotoneV2", "kotoneV2_added_IVF2780_Flat_nprobe_1_v2.index")
        self.make_index("kotoneV2", "trained_IVF2780_Flat_nprobe_1_kotoneV2_v2.index")
        self.make_index("other", "other_added_IVF1_v2.index")
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(expected))
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e120_s12360.pth"), str(expected))

    def test_auto_index_matches_the_2026_indices_folder_name(self):
        expected = self.make_index(
            ".", "kotoneV2_IVF2780_Flat_nprobe_1_kotoneV2_v2.index", base="assets/indices"
        )
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(expected))

    def test_auto_index_is_case_insensitive_and_recursive(self):
        expected = self.make_index("a/b", "KOTONEV2_ADDED_x.INDEX")
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(expected))

    def test_auto_index_matches_a_whole_model_stem_as_a_weaker_match(self):
        weak = self.make_index(".", "my_kotoneV2_e150_s15450_copy.index", base="assets/indices")
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(weak))
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e120_s12360.pth"), "")
        standard = self.make_index("kotoneV2", "kotoneV2_added_a.index", age=5000)
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(standard))  # standard beats location

    def test_auto_index_order_standard_then_location_then_newest_then_path(self):
        logs_new = self.make_index("kotoneV2", "kotoneV2_added_new.index", age=1)
        self.make_index("kotoneV2", "kotoneV2_added_old.index", age=1000)
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(logs_new))
        outside_old = self.make_index(".", "kotoneV2_added_old.index", age=5000, base="assets/indices")
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(outside_old))  # assets/indices first
        outside_new = self.make_index(".", "kotoneV2_added_new.index", age=10, base="assets/indices")
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(outside_new))  # then newest
        tie_a = self.make_index("x", "kotoneV2_added_a.index", age=0, base="assets/indices")
        stamp = os.path.getmtime(outside_new)
        os.utime(tie_a, (stamp, stamp))
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), str(outside_new))  # equal times: path order

    def test_auto_index_old_names_without_the_experiment_prefix_do_not_match(self):
        self.make_index("kotoneV2", "added_IVF1_Flat.index")
        self.assertEqual(auto_index(str(self.root), "kotoneV2_e150_s15450.pth"), "")

    def test_auto_index_is_empty_without_a_match(self):
        self.assertEqual(auto_index(str(self.root), "huahuo.pth"), "")
        self.assertEqual(auto_index(str(self.root), ""), "")
        self.assertEqual(auto_index(str(self.root / "missing"), "x.pth"), "")

    def test_resolve_index_prefers_the_manual_path(self):
        auto = self.make_index("kotoneV2", "kotoneV2_added_a.index")
        settings = validate_cover_settings({"rvc_root": str(self.root), "model": "kotoneV2_e150_s15450.pth"})
        self.assertEqual(resolve_index(settings), str(auto))
        settings["index_path"] = r"E:\mine.index"
        self.assertEqual(resolve_index(settings), r"E:\mine.index")


class LegacyConfigTests(unittest.TestCase):
    OLD = {
        "enabled": True, "rvc_root": r"E:\Tools\RVC", "port": 8099, "model": "kotoneV2_e150_s15450.pth",
        "f0_method": "harvest", "filter_radius": 3, "uvr_model": "HP5_only_main_vocal", "inst_gain_db": -5,
        "music_api_key": "SECRETKEY123456",
    }

    def test_old_saved_config_keeps_its_key_and_reads_the_new_defaults(self):
        for old_method in ("harvest", "crepe"):
            settings = cover_settings({"cover": {**self.OLD, "f0_method": old_method}})
            self.assertEqual(settings["music_api_key"], "SECRETKEY123456")
            self.assertEqual(settings["f0_method"], "rmvpe")
            self.assertEqual(settings["inst_gain_db"], -5)
            self.assertEqual(settings["rvc_root"], r"E:\Tools\RVC")  # edited at deploy time, not here
            self.assertEqual((settings["sep_model"], settings["dereverb_model"]), ("vocals-bs-roformer-368", "dereverb-less-aggressive-18.8050"))
            self.assertNotIn("uvr_model", settings)
            self.assertNotIn("filter_radius", settings)

    def test_config_store_accepts_an_old_cover_section(self):
        with tempfile.TemporaryDirectory() as directory:
            ConfigStore(Path(directory))
            path = Path(directory) / "config.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["cover"] = dict(self.OLD)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            store = ConfigStore(Path(directory))
            self.assertEqual(cover_settings(store.config)["music_api_key"], "SECRETKEY123456")

    def test_f0_methods(self):
        self.assertEqual(F0_METHODS, ("rmvpe", "fcpe", "pm"))
        for method in F0_METHODS:
            self.assertEqual(validate_cover_settings({"f0_method": method})["f0_method"], method)


class SongLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.library = SongLibrary(Path(self.directory.name))

    def test_magic_detection(self):
        self.assertEqual(detect_song_suffix(MP3[:16]), ".mp3")
        self.assertEqual(detect_song_suffix(MP3_SYNC[:16]), ".mp3")
        self.assertEqual(detect_song_suffix(FLAC[:16]), ".flac")
        self.assertEqual(detect_song_suffix(WAV[:16]), ".wav")
        self.assertEqual(detect_song_suffix(M4A[:16]), ".m4a")
        self.assertEqual(detect_song_suffix(OGG[:16]), ".ogg")
        for junk in (b"", b"MZ\x90\x00", b"<html>", b"\x89PNG\r\n\x1a\n", b"RIFF\x00\x00\x00\x00AVI "):
            self.assertIsNone(detect_song_suffix(junk), junk)

    def test_add_uses_the_detected_type_and_records_metadata(self):
        song = self.library.add("My Song.exe", FLAC)
        self.assertEqual(song["ext"], ".flac")
        self.assertTrue(song["file"].endswith(".flac"))
        self.assertEqual(song["title"], "My Song")
        self.assertEqual(song["filename"], "My Song.exe")
        self.assertEqual(song["size"], len(FLAC))
        self.assertEqual(song["sha256"], hashlib.sha256(FLAC).hexdigest())
        self.assertIsNone(song["clip_start"])
        self.assertRegex(song["id"], r"^[0-9a-f]{8}$")
        self.assertEqual(self.library.path(song).read_bytes(), FLAC)

    def test_every_supported_format(self):
        for content in (MP3, MP3_SYNC + b"1", FLAC + b"1", WAV + b"1", M4A + b"1", OGG + b"1"):
            self.library.add("s", content)
        self.assertEqual(len(self.library.list()), 6)

    def test_rejections(self):
        with self.assertRaises(EmotionConfigError) as caught:
            self.library.add("a.mp3", b"not audio at all")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(EmotionConfigError):
            self.library.add("a.mp3", b"")
        with self.assertRaises(EmotionConfigError) as caught:
            self.library.add("a.mp3", MP3 + b"\x00" * SONG_MAX_BYTES)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list(Path(self.directory.name, "songs").glob("*.mp3")) if Path(self.directory.name, "songs").exists() else [], [])

    def test_duplicate_content_is_refused(self):
        self.library.add("a.mp3", MP3)
        with self.assertRaises(EmotionConfigError) as caught:
            self.library.add("b.mp3", MP3)
        self.assertEqual(caught.exception.status, 409)

    def test_names_are_sanitised_and_deduplicated(self):
        first = self.library.add("..\\..\\evil:name?.mp3", MP3)
        second = self.library.add("../evil name.mp3", MP3 + b"2")
        third = self.library.add("same.mp3", MP3 + b"3")
        fourth = self.library.add("same.mp3", MP3 + b"4")
        root = Path(self.directory.name, "songs").resolve()
        for song in (first, second, third, fourth):
            self.assertEqual(self.library.path(song).parent, root)
        self.assertNotEqual(third["file"], fourth["file"])
        self.assertEqual(fourth["file"], "same (2).mp3")

    def test_persistence_and_duration(self):
        song = self.library.add("a.mp3", MP3)
        self.library.set_duration(song["id"], 123.456)
        reloaded = SongLibrary(Path(self.directory.name))
        self.assertEqual(reloaded.get(song["id"])["duration"], 123.46)
        self.assertIsNone(reloaded.get("nothere"))

    def test_update_title_and_clip_start(self):
        song = self.library.add("a.mp3", MP3)
        self.library.set_duration(song["id"], 200)
        updated = self.library.update(song["id"], title="  新标题  ", clip_start=75.5)
        self.assertEqual((updated["title"], updated["clip_start"]), ("新标题", 75.5))
        self.assertIsNone(self.library.update(song["id"], clip_start=None)["clip_start"])
        self.assertEqual(self.library.update(song["id"], title="x")["title"], "x")
        for bad in ({"title": ""}, {"title": "x" * 81}, {"title": 5}, {"clip_start": -1}, {"clip_start": 201}, {"clip_start": "5"}, {"clip_start": True}):
            with self.assertRaises(EmotionConfigError, msg=str(bad)):
                self.library.update(song["id"], **bad)
        with self.assertRaises(EmotionConfigError) as caught:
            self.library.update("nothere", title="x")
        self.assertEqual(caught.exception.status, 404)

    def test_delete_removes_the_file(self):
        song = self.library.add("a.mp3", MP3)
        path = self.library.path(song)
        self.library.delete(song["id"])
        self.assertFalse(path.exists())
        self.assertEqual(self.library.list(), [])
        with self.assertRaises(EmotionConfigError):
            self.library.delete(song["id"])

    def test_a_tampered_index_cannot_point_outside_the_library(self):
        song = self.library.add("a.mp3", MP3)
        index = Path(self.directory.name, "songs", "songs.json")
        data = json.loads(index.read_text(encoding="utf-8"))
        data["songs"][0]["file"] = "../../secret.txt"
        index.write_text(json.dumps(data), encoding="utf-8")
        reloaded = SongLibrary(Path(self.directory.name))
        with self.assertRaises(EmotionConfigError):
            reloaded.path(reloaded.get(song["id"]))

    def test_content_types_are_a_fixed_map(self):
        self.assertEqual(audio_content_type(Path("a.mp3")), "audio/mpeg")
        self.assertEqual(audio_content_type(Path("a.flac")), "audio/flac")
        self.assertEqual(audio_content_type(Path("a.wav")), "audio/wav")
        self.assertEqual(audio_content_type(Path("a.m4a")), "audio/mp4")
        self.assertEqual(audio_content_type(Path("a.ogg")), "audio/ogg")
        self.assertEqual(audio_content_type(Path("a.xyz")), "application/octet-stream")


class CacheKeyTests(unittest.TestCase):
    PARAMS = {
        "model": "m.pth", "index_path": "i.index", "f0_up_key": 0, "f0_method": "rmvpe", "index_rate": 0.75,
        "rms_mix_rate": 0.25, "protect": 0.33, "sep_model": "s1", "dereverb_model": "d1", "vocal_reverb": 0, "clip_start": 10.0,
        "clip_seconds": 60.0, "vocal_gain_db": 0, "inst_gain_db": 0,
    }

    def test_same_inputs_same_key(self):
        self.assertEqual(cache_key("abc", self.PARAMS), cache_key("abc", dict(self.PARAMS)))
        self.assertRegex(cache_key("abc", self.PARAMS), r"^[0-9a-f]{16}$")

    def test_every_parameter_and_the_song_change_the_key(self):
        base = cache_key("abc", self.PARAMS)
        self.assertNotEqual(base, cache_key("abd", self.PARAMS))
        changes = {
            "model": "n.pth", "index_path": "", "f0_up_key": 1, "f0_method": "pm", "index_rate": 0.5,
            "rms_mix_rate": 0.5, "protect": 0.2, "sep_model": "s2", "dereverb_model": "", "vocal_reverb": 30, "clip_start": 11.0,
            "clip_seconds": 30.0, "vocal_gain_db": 1, "inst_gain_db": -1,
        }
        for name, value in changes.items():
            self.assertNotEqual(base, cache_key("abc", {**self.PARAMS, name: value}), name)


class CoverCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = CoverCache(Path(self.directory.name))

    def add(self, key, title="歌"):
        self.cache.directory.mkdir(parents=True, exist_ok=True)
        self.cache.path_for(key).write_bytes(b"RIFFwav")
        return self.cache.add(key, song_id="s1", title=title, summary="摘要", duration=12.3, timings={"sep_ms": 1, "vc_ms": 2, "junk": 3})

    def test_add_get_list_delete(self):
        entry = self.add("aaaa1111")
        self.assertEqual((entry["key"], entry["title"], entry["summary"], entry["size"], entry["duration"]), ("aaaa1111", "歌", "摘要", 7, 12.3))
        self.assertEqual(entry["timings"], {"sep_ms": 1, "vc_ms": 2})
        self.assertIsNotNone(self.cache.get("aaaa1111"))
        self.assertIsNone(self.cache.get("bbbb2222"))
        self.add("bbbb2222")
        self.assertEqual({item["key"] for item in self.cache.list()}, {"aaaa1111", "bbbb2222"})
        self.cache.delete("aaaa1111")
        self.assertFalse(self.cache.path_for("aaaa1111").exists())
        self.assertIsNone(self.cache.get("aaaa1111"))
        with self.assertRaises(EmotionConfigError):
            self.cache.delete("aaaa1111")

    def test_survives_a_restart_and_forgets_missing_files(self):
        self.add("aaaa1111")
        self.add("bbbb2222")
        self.cache.path_for("bbbb2222").unlink()
        reloaded = CoverCache(Path(self.directory.name))
        self.assertEqual([item["key"] for item in reloaded.list()], ["aaaa1111"])

    def test_keys_cannot_escape_the_directory(self):
        for key in ("../x", "a/b", "a.b", "", "x" * 40):
            with self.assertRaises(EmotionConfigError, msg=key):
                self.cache.path_for(key)
        self.assertIsNone(self.cache.get("../x"))


if __name__ == "__main__":
    unittest.main()


class PitchPresetSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = default_cover_settings()
        self.assertEqual(settings["inst_pitch"], 0)
        self.assertEqual(
            [(p["name"], p["vocal"], p["inst"]) for p in settings["pitch_presets"]],
            [("男声歌", 12, 0), ("升调", 0, 2), ("降调", 0, -2)],
        )
        self.assertEqual(settings["pitch_presets"][0]["keywords"], ["男声", "男生", "男歌手"])
        self.assertEqual(validate_cover_settings({}), settings)

    def test_old_config_without_the_keys_gets_defaults_and_keeps_its_key(self):
        old = {"rvc_root": r"E:\Tools\RVC", "f0_method": "rmvpe", "f0_up_key": 3, "music_api_key": "SECRETKEY123456"}
        settings = cover_settings({"cover": old})
        self.assertEqual(settings["music_api_key"], "SECRETKEY123456")
        self.assertEqual(settings["f0_up_key"], 3)
        self.assertEqual((settings["inst_pitch"], len(settings["pitch_presets"])), (0, 3))

    def test_valid_presets_are_normalised(self):
        presets = [{"name": " 一 ", "vocal": -12, "inst": 12, "keywords": [" 甲 ", "乙"]}]
        saved = validate_cover_settings({"pitch_presets": presets, "inst_pitch": -5})
        self.assertEqual(saved["pitch_presets"], [{"name": "一", "vocal": -12, "inst": 12, "keywords": ["甲", "乙"]}])
        self.assertEqual(saved["inst_pitch"], -5)
        self.assertEqual(validate_cover_settings({"pitch_presets": []})["pitch_presets"], [])

    def test_invalid_presets(self):
        def preset(**changes):
            return {**{"name": "甲", "vocal": 0, "inst": 0, "keywords": ["x"]}, **changes}

        bad = [
            "x", [5], [preset(name="")], [preset(name="  ")], [preset(name="字" * 21)], [preset(name=5)],
            [preset(vocal=13)], [preset(inst=-13)], [preset(vocal=1.5)], [preset(vocal=True)], [preset(inst="1")],
            [preset(keywords=[])], [preset(keywords="x")], [preset(keywords=[""])], [preset(keywords=["  "])],
            [preset(keywords=["字" * 11])], [preset(keywords=[str(n) for n in range(11)])], [preset(keywords=[5])],
            [preset(), preset()],  # same name
            [preset(name="a", keywords=["同"]), preset(name="b", keywords=["同"])],  # same keyword in two presets
            [preset(name="a", keywords=["Key"]), preset(name="b", keywords=["ｋｅｙ"])],  # equal after NFKC + case
            [preset(keywords=["同", "同"])],  # twice in one preset
            [preset(name=str(n), keywords=[str(n)]) for n in range(11)],  # more than 10
        ]
        for value in bad:
            with self.assertRaises(EmotionConfigError, msg=str(value)):
                validate_cover_settings({"pitch_presets": value})
        for value in ({"inst_pitch": 13}, {"inst_pitch": -13}, {"inst_pitch": 1.5}, {"inst_pitch": True}):
            with self.assertRaises(EmotionConfigError, msg=str(value)):
                validate_cover_settings(value)

    def test_ten_presets_are_allowed(self):
        presets = [{"name": str(n), "vocal": 0, "inst": 0, "keywords": [f"k{n}"]} for n in range(10)]
        self.assertEqual(len(validate_cover_settings({"pitch_presets": presets})["pitch_presets"]), 10)

    def test_vocal_pitch_setting_stays_within_12(self):
        with self.assertRaises(EmotionConfigError):
            validate_cover_settings({"f0_up_key": 13})
