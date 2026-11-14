import io
import json
import os
import random
import socket
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from aiohttp import ClientSession, FormData, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import emotion_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    discover_emotions,
    safe_audio_stem,
    select_tts_profile,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.fs_browser import (
    browse,
    find_gpt_sovits_root,
    start_directory,
)
from test_plugin_lifecycle import (
    _bridge_event,
    _complete_bridge_reply,
    _load_plugin_module,
    _ready_plugin,
)


def write_wav(path: Path, seconds: float, rate: int = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def wav_bytes(seconds: float, rate: int = 1000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return output.getvalue()


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-p25-")
        self.root = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()


class DiscoverEmotionsTests(TempDirTestCase):
    def test_folders_files_and_durations(self):
        ktn = self.root / "ktn"
        write_wav(ktn / "开心" / "やったね！.wav", 5)
        write_wav(ktn / "开心" / "短すぎ.wav", 2)
        write_wav(ktn / "开心" / "長すぎ.wav", 11)
        (ktn / "开心" / "notes.txt").write_text("ignored", encoding="utf-8")
        (ktn / "开心" / "broken.wav").write_bytes(b"not a wav")
        (ktn / "开心" / "compressed.mp3").write_bytes(b"ID3")
        (ktn / "伤心").mkdir()
        write_wav(ktn / "bad name" / "x.wav", 5)
        write_wav(ktn / "nested" / "deeper" / "x.wav", 5)
        write_wav(ktn / "loose.wav", 5)

        found = discover_emotions(ktn)
        self.assertEqual(set(found), {"伤心", "开心", "nested"})
        self.assertEqual(found["伤心"], [])
        self.assertEqual(found["nested"], [])
        audios = {audio["filename"]: audio for audio in found["开心"]}
        self.assertEqual(
            sorted(audios),
            ["broken.wav", "compressed.mp3", "やったね！.wav", "短すぎ.wav", "長すぎ.wav"],
        )
        happy = audios["やったね！.wav"]
        self.assertEqual(happy["prompt_text"], "やったね！")
        self.assertEqual(happy["relative_path"], "开心/やったね！.wav")
        self.assertEqual(happy["duration"], 5.0)
        self.assertEqual((happy["out_of_range"], happy["usable"]), (False, True))
        for name in ("短すぎ.wav", "長すぎ.wav"):
            self.assertEqual((audios[name]["out_of_range"], audios[name]["usable"]), (True, False))
        self.assertEqual(
            (audios["broken.wav"]["readable"], audios["broken.wav"]["usable"], audios["broken.wav"]["duration"]),
            (False, False, None),
        )
        self.assertEqual((audios["compressed.mp3"]["duration"], audios["compressed.mp3"]["usable"]), (None, True))

    def test_boundaries_and_prompt_text(self):
        write_wav(self.root / "认真" / "  丁度三秒  .wav", 3)
        write_wav(self.root / "认真" / "丁度十秒.wav", 10)
        write_wav(self.root / "认真" / "同じ文 (2).wav", 5)
        audios = {audio["prompt_text"]: audio for audio in discover_emotions(self.root)["认真"]}
        self.assertEqual(sorted(audios), ["丁度三秒", "丁度十秒", "同じ文"])
        self.assertTrue(all(audio["usable"] for audio in audios.values()))

    def test_missing_or_invalid_roots_return_empty(self):
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        for root in (None, "", "   ", self.root / "missing", self.root / "file.txt", 123):
            with self.subTest(root=root):
                self.assertEqual(discover_emotions(root), {})
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assertEqual(discover_emotions(self.root), {})


class DirectorySelectionTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.ktn = self.root / "ktn"
        self.happy = [write_wav(self.ktn / "开心" / f"開心{index}.wav", 4 + index) for index in range(3)]
        write_wav(self.ktn / "开心" / "長い.wav", 12)
        write_wav(self.ktn / "认真" / "真面目です.wav", 5)
        (self.ktn / "伤心").mkdir()
        self.manual_audio = write_wav(self.root / "manual.wav", 5)
        self.config = {
            "version": 1,
            "default_character": "琴音",
            "default_emotion": "认真",
            "models": {
                "ktn": {
                    "name": "ktn",
                    "gpt_model_path": r"D:\GPT\ktn.ckpt",
                    "sovits_model_path": r"D:\SoVITS\ktn.pth",
                    "reference_audio_root": str(self.ktn),
                },
            },
            "characters": {
                "琴音": {"name": "琴音", "model_name": "ktn", "default_emotion": "伤心", "emotions": {}},
            },
        }

    def emotions(self):
        return self.config["characters"]["琴音"]["emotions"]

    def test_directory_profile_uses_random_file_and_defaults(self):
        selection = select_tts_profile(self.config, "开心", rng=random.Random(1))
        self.assertEqual((selection.emotion, selection.reason, selection.source, selection.candidates),
                         ("开心", None, "directory", 3))
        profile = selection.profile
        self.assertIn(profile["ref_audio_path"], {str(path) for path in self.happy})
        self.assertEqual(profile["prompt_text"], Path(profile["ref_audio_path"]).stem)
        self.assertEqual(profile["source"], "directory")
        self.assertEqual(profile["top_k"], emotion_config.default_emotion()["top_k"])
        self.assertEqual(profile["gpt_model_path"], r"D:\GPT\ktn.ckpt")

    def test_seeded_choice_is_reproducible_and_covers_every_file(self):
        first = [select_tts_profile(self.config, "开心", rng=random.Random(42)).profile["ref_audio_path"]
                 for _ in range(3)]
        self.assertEqual(len(set(first)), 1)
        rng = random.Random(7)
        seen = {select_tts_profile(self.config, "开心", rng=rng).profile["ref_audio_path"] for _ in range(60)}
        self.assertEqual(seen, {str(path) for path in self.happy})

    def test_manual_profile_with_audio_overrides_directory(self):
        self.emotions()["开心"] = emotion_config.validate_emotion_profile(
            {"ref_audio_path": str(self.manual_audio), "prompt_text": "手動", "top_k": 30}
        )
        selection = select_tts_profile(self.config, "开心", rng=random.Random(1))
        self.assertEqual((selection.source, selection.candidates), ("manual", 1))
        self.assertEqual(selection.profile["ref_audio_path"], str(self.manual_audio))
        self.assertEqual(selection.profile["prompt_text"], "手動")

    def test_manual_parameters_apply_to_directory_audio_when_manual_audio_is_missing(self):
        self.emotions()["开心"] = emotion_config.validate_emotion_profile(
            {"ref_audio_path": "", "prompt_text": "unused", "top_k": 30, "speed_factor": 1.3}
        )
        selection = select_tts_profile(self.config, "开心", rng=random.Random(1))
        self.assertEqual(selection.source, "directory")
        self.assertEqual(selection.profile["top_k"], 30)
        self.assertEqual(selection.profile["speed_factor"], 1.3)
        self.assertNotEqual(selection.profile["prompt_text"], "unused")

    def test_fallback_chain_across_empty_and_missing_folders(self):
        selection = select_tts_profile(self.config, "生气", rng=random.Random(1))
        # 生气 does not exist, the character default 伤心 is empty, the global default 认真 works.
        self.assertEqual((selection.emotion, selection.reason, selection.source, selection.candidates),
                         ("认真", "UnknownEmotion", "directory", 1))
        selection = select_tts_profile(self.config, "伤心", rng=random.Random(1))
        self.assertEqual((selection.emotion, selection.reason), ("认真", "MissingAudio"))
        selection = select_tts_profile(self.config, None, rng=random.Random(1))
        self.assertEqual((selection.emotion, selection.reason), ("认真", "NoTag"))

    def test_out_of_range_only_folder_is_missing_audio(self):
        for path in self.happy:
            path.unlink()
        self.config["default_emotion"] = "伤心"
        selection = select_tts_profile(self.config, "开心", rng=random.Random(1))
        self.assertEqual(selection, type(selection)(None, None, "MissingAudio"))

    def test_default_audio_dir_is_used_when_model_root_is_empty(self):
        self.config["models"]["ktn"]["reference_audio_root"] = ""
        self.assertIsNone(select_tts_profile(self.config, "开心").profile)
        default_dir = self.root / "plugin_audio"
        write_wav(default_dir / "ktn" / "开心" / "既定.wav", 5)
        selection = select_tts_profile(self.config, "开心", default_audio_dir=default_dir)
        self.assertEqual(selection.profile["prompt_text"], "既定")

    def test_discovery_runs_once_and_only_when_needed(self):
        calls = []

        def counting_discover(root):
            calls.append(root)
            return discover_emotions(root)

        self.emotions()["开心"] = emotion_config.validate_emotion_profile({"ref_audio_path": str(self.manual_audio)})
        select_tts_profile(self.config, "开心", discover=counting_discover)
        self.assertEqual(calls, [])
        select_tts_profile(self.config, "生气", discover=counting_discover)
        self.assertEqual(len(calls), 1)


class SafeStemAndRetryTests(TempDirTestCase):
    def test_safe_audio_stem(self):
        cases = {
            'a\\b/c:d*e?f"g<h>i|j': "abcdefghij",
            "  末尾の点... ": "末尾の点",
            "line\nbreak\ttab": "linebreaktab",
            "": "reference",
            "???": "reference",
            "CON": "_CON",
            "nul.txt": "_nul.txt",
            "x" * 300: "x" * 120,
            "そんなすごいアイドルが……あたしを見てくれてるんです。": "そんなすごいアイドルが……あたしを見てくれてるんです。",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw[:20]):
                self.assertEqual(safe_audio_stem(raw), expected)

    def test_replace_retries_transient_permission_errors(self):
        store = ConfigStore(self.root)
        real_replace = os.replace
        attempts = []
        sleeps = []

        def flaky_replace(source, target):
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError("locked")
            return real_replace(source, target)

        def add_model(config):
            config["models"]["m"] = {"name": "m"}

        with patch.object(emotion_config.os, "replace", flaky_replace), \
                patch.object(emotion_config.time, "sleep", sleeps.append):
            import asyncio
            asyncio.run(store.update(add_model))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [0.05, 0.1])
        self.assertIn("m", json.loads((self.root / "config.json").read_text(encoding="utf-8"))["models"])
        self.assertFalse(list(self.root.glob("config.*.tmp")))

    def test_replace_gives_up_after_five_attempts(self):
        store = ConfigStore(self.root)
        before = (self.root / "config.json").read_bytes()
        attempts = []
        sleeps = []

        def locked(source, target):
            attempts.append(1)
            raise PermissionError("locked")

        def add_model(config):
            config["models"]["m"] = {"name": "m"}

        import asyncio
        with patch.object(emotion_config.os, "replace", locked), \
                patch.object(emotion_config.time, "sleep", sleeps.append):
            with self.assertRaises(PermissionError):
                asyncio.run(store.update(add_model))
        self.assertEqual(len(attempts), 5)
        self.assertLessEqual(sum(sleeps), 1.0)
        self.assertEqual((self.root / "config.json").read_bytes(), before)
        self.assertEqual(store.config["models"], {})
        self.assertFalse(list(self.root.glob("config.*.tmp")))

    def test_other_errors_are_not_retried(self):
        store = ConfigStore(self.root)
        attempts = []

        def broken(source, target):
            attempts.append(1)
            raise OSError("disk")

        import asyncio
        with patch.object(emotion_config.os, "replace", broken):
            with self.assertRaises(OSError):
                asyncio.run(store.update(lambda config: None))
        self.assertEqual(len(attempts), 1)


class FileBrowserTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.install = self.root / "GPT-SoVITS-v2pro"
        (self.install / "api.py").parent.mkdir(parents=True)
        (self.install / "api.py").write_text("# api", encoding="utf-8")
        (self.install / "GPT_weights_v2ProPlus").mkdir()
        (self.install / "GPT_weights_v2ProPlus" / "kotone.ckpt").write_bytes(b"x")
        (self.install / "SoVITS_weights_v2ProPlus").mkdir()
        (self.install / "SoVITS_weights_v2ProPlus" / "kotone.pth").write_bytes(b"x")
        (self.install / "b_dir").mkdir()
        (self.install / "A_dir").mkdir()
        (self.install / "voice.WAV").write_bytes(b"x")
        (self.install / "voice.ogg").write_bytes(b"x")
        (self.install / "model.CKPT").write_bytes(b"x")
        (self.install / "weights.pth").write_bytes(b"x")
        (self.install / "readme.txt").write_text("x", encoding="utf-8")

    def names(self, mode):
        return [entry["name"] for entry in browse(str(self.install), mode)["entries"]]

    def test_modes_filter_and_sort_directories_first(self):
        dirs = ["A_dir", "b_dir", "GPT_weights_v2ProPlus", "SoVITS_weights_v2ProPlus"]
        self.assertEqual(self.names("dir"), dirs)
        self.assertEqual(self.names("ckpt"), dirs + ["model.CKPT"])
        self.assertEqual(self.names("pth"), dirs + ["weights.pth"])
        self.assertEqual(self.names("audio"), dirs + ["voice.ogg", "voice.WAV"])
        listing = browse(str(self.install), "dir")
        self.assertEqual(listing["path"], str(self.install))
        self.assertEqual(listing["parent"], str(self.install.parent))
        self.assertTrue(all(entry["is_dir"] for entry in listing["entries"]))
        self.assertEqual(listing["entries"][0]["path"], str(self.install / "A_dir"))
        self.assertTrue(listing["drives"])

    def test_file_path_lists_its_directory_and_marks_selection(self):
        ckpt = self.install / "GPT_weights_v2ProPlus" / "kotone.ckpt"
        listing = browse(str(ckpt), "ckpt")
        self.assertEqual(listing["path"], str(ckpt.parent))
        self.assertEqual(listing["selected"], str(ckpt))
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["kotone.ckpt"])

    def test_drive_root_has_no_parent(self):
        anchor = Path(self.root.anchor)
        self.assertIsNone(browse(str(anchor), "dir")["parent"])

    def test_errors_are_client_errors(self):
        cases = (
            (str(self.root / "missing"), "dir", 404),
            ("relative\\path", "dir", 400),
            (str(self.install), "exe", 400),
            ("bad\x00path", "dir", 400),
        )
        for path, mode, status in cases:
            with self.subTest(path=path, mode=mode):
                with self.assertRaises(EmotionConfigError) as caught:
                    browse(path, mode)
                self.assertEqual(caught.exception.status, status)
        with patch("os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(EmotionConfigError) as caught:
                browse(str(self.install), "dir")
        self.assertEqual(caught.exception.status, 403)

    def test_empty_path_uses_start_directory_or_drives(self):
        listing = browse("", "dir")
        self.assertEqual((listing["path"], listing["entries"]), ("", []))
        self.assertTrue(listing["drives"])
        self.assertEqual(browse("", "dir", start=self.install)["path"], str(self.install))

    def test_gpt_sovits_root_is_inferred_from_model_paths(self):
        ckpt = self.install / "GPT_weights_v2ProPlus" / "kotone.ckpt"
        self.assertEqual(find_gpt_sovits_root([None, "", str(ckpt)]), self.install)
        self.assertEqual(find_gpt_sovits_root([str(self.install / "missing" / "x.pth")]), self.install)
        self.assertIsNone(find_gpt_sovits_root([str(self.root / "elsewhere" / "x.ckpt")]))
        config = {"models": {"ktn": {"gpt_model_path": str(ckpt), "sovits_model_path": ""}}}
        self.assertEqual(start_directory(config), self.install)
        config["gpt_sovits_root"] = str(self.install / "A_dir")
        self.assertEqual(start_directory(config), self.install / "A_dir")
        config["gpt_sovits_root"] = str(self.root / "deleted")
        self.assertEqual(start_directory(config), self.install)
        self.assertIsNone(start_directory({"models": {}}))

    def test_legacy_config_without_gpt_sovits_root_loads(self):
        legacy = {"version": 1, "default_character": "", "default_emotion": "neutral", "models": {}, "characters": {}}
        (self.root / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        store = ConfigStore(self.root)
        self.assertNotIn("gpt_sovits_root", store.config)
        bad = dict(legacy, gpt_sovits_root=5)
        (self.root / "config.json").write_text(json.dumps(bad), encoding="utf-8")
        # 101e-3: an unusable config is set aside and the defaults are used instead of failing to start.
        store = ConfigStore(self.root)
        self.assertEqual(store.recovery["used"], "default")
        self.assertNotEqual(store.config.get("gpt_sovits_root"), 5)
        self.assertEqual(len(store.recovery["broken"]), 1)


class ConsoleP25RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-p25-routes-")
        self.root = Path(self._directory.name)
        self.ktn = self.root / "host" / "ktn"
        self.logger = _ListLogger()
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        self.port = _unused_port()
        self.store = ConfigStore(self.root / "plugin_data")
        app = create_console_app(self.store, logger=self.logger)
        self.service = ConsoleService(app, port=self.port)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        await self.client.close()
        self._directory.cleanup()

    async def request(self, path, method="GET", payload=None, headers=None):
        request_headers = dict(headers or {})
        async with self.client.request(
            method, f"http://127.0.0.1:{self.port}{path}", json=payload, headers=request_headers,
        ) as response:
            return response.status, await response.json()

    async def bind_character(self, audio_root=""):
        await self.request("/api/models", "POST", {
            "name": "ktn",
            "gpt_model_path": r"C:\models\ktn.ckpt",
            "sovits_model_path": r"C:\models\ktn.pth",
            "reference_audio_root": audio_root,
        })
        await self.request("/api/characters", "POST", {"name": "琴音", "model_name": "ktn"})

    async def upload(self, emotion, file_name, content, prompt_text=None):
        form = FormData()
        form.add_field("character", "琴音")
        form.add_field("emotion", emotion)
        if prompt_text is not None:
            form.add_field("prompt_text", prompt_text)
        form.add_field("file", content, filename=file_name, content_type="audio/wav")
        async with self.client.post(
            f"http://127.0.0.1:{self.port}/api/audio/upload",
            data=form,
        ) as response:
            return response.status, await response.json()

    async def test_upload_uses_emotion_folders_and_clean_names(self):
        await self.bind_character(str(self.ktn))
        emotions_before = (await self.request("/api/config"))[1]["characters"]["琴音"]["emotions"]
        status, body = await self.upload("开心", "orig.wav", wav_bytes(5), 'そう?<なの>: "本当"|*')
        self.assertEqual(status, 200)
        self.assertEqual(body["relative_path"], "开心/そうなの 本当.wav")
        self.assertTrue((self.ktn / "开心" / "そうなの 本当.wav").is_file())
        status, body = await self.upload("开心", "orig.wav", wav_bytes(5), "そう?<なの>: \"本当\"|*")
        self.assertEqual(body["relative_path"], "开心/そうなの 本当 (2).wav")
        status, body = await self.upload("开心", "orig-name.wav", wav_bytes(5), "   ")
        self.assertEqual(body["relative_path"], "开心/orig-name.wav")
        status, body = await self.upload("../x", "a.wav", wav_bytes(5), "t")
        self.assertEqual(status, 400)

        status, discovered = await self.request(f"/api/emotions/discovered?character={quote('琴音')}")
        happy = discovered["emotions"][0]
        self.assertEqual(happy["name"], "开心")
        self.assertEqual(happy["available"], 3)
        # The de-duplication suffix is not part of the reference text.
        self.assertEqual(
            sorted(audio["prompt_text"] for audio in happy["audios"]),
            ["orig-name", "そうなの 本当", "そうなの 本当"],
        )
        # A new config ships a default 琴音 with default emotions (101b-B); uploading must not touch them,
        # and the files really are in the emotion folders.
        status, config = await self.request("/api/config")
        self.assertEqual(config["characters"]["琴音"]["emotions"], emotions_before)
        self.assertEqual(sorted(path.name for path in (self.ktn / "开心").iterdir()), ["orig-name.wav", "そうなの 本当 (2).wav", "そうなの 本当.wav"])

    async def test_discovered_endpoint_lists_empty_and_out_of_range(self):
        await self.bind_character(str(self.ktn))
        write_wav(self.ktn / "开心" / "良い.wav", 5)
        write_wav(self.ktn / "开心" / "長い.wav", 12)
        (self.ktn / "伤心").mkdir()
        status, body = await self.request(f"/api/emotions/discovered?character={quote('琴音')}")
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "ktn")
        emotions = {item["name"]: item for item in body["emotions"]}
        self.assertEqual(emotions["伤心"], {"name": "伤心", "available": 0, "audios": []})
        self.assertEqual(emotions["开心"]["available"], 1)
        long_audio = next(audio for audio in emotions["开心"]["audios"] if audio["prompt_text"] == "長い")
        self.assertEqual((long_audio["duration"], long_audio["out_of_range"]), (12.0, True))
        self.assertNotIn("path", long_audio)

        stream = f"/api/audio/stream?model=ktn&path={quote(long_audio['relative_path'])}"
        async with self.client.get(f"http://127.0.0.1:{self.port}{stream}") as response:
            self.assertEqual(response.status, 200)

        status, listing = await self.request(f"/api/audio?character={quote('琴音')}")
        item = next(audio for audio in listing["audios"] if audio["prompt_text"] == "長い")
        self.assertEqual((item["emotion"], item["duration"], item["out_of_range"]), ("开心", 12.0, True))

        status, _ = await self.request(f"/api/emotions/discovered?character={quote('nobody')}")
        self.assertEqual(status, 404)
        await self.request("/api/characters", "POST", {"name": "unbound"})
        status, body = await self.request("/api/emotions/discovered?character=unbound")
        self.assertEqual((status, body["emotions"]), (200, []))

    async def test_unexpected_errors_log_only_the_type(self):
        with patch.object(self.store, "models", side_effect=ValueError(r"secret D:\path")):
            status, body = await self.request("/api/models")
        self.assertEqual((status, body), (500, {"error": "internal_error"}))
        self.assertEqual(self.logger.errors, ["[STBridge] Console request failed (ValueError)"])

    async def test_fs_list_route(self):
        install = self.root / "install"
        (install / "GPT_weights").mkdir(parents=True)
        (install / "api.py").write_text("#", encoding="utf-8")
        (install / "GPT_weights" / "k.ckpt").write_bytes(b"x")
        (install / "x.pth").write_bytes(b"x")

        status, body = await self.request(f"/api/fs/list?path={quote(str(install))}&mode=pth")
        self.assertEqual(status, 200)
        self.assertEqual([entry["name"] for entry in body["entries"]], ["GPT_weights", "x.pth"])
        status, body = await self.request(f"/api/fs/list?path={quote(str(install / 'nope'))}")
        self.assertEqual((status, body), (404, {"error": "directory not found"}))
        status, _ = await self.request(f"/api/fs/list?path={quote(str(install))}&mode=zip")
        self.assertEqual(status, 400)

        status, body = await self.request("/api/fs/list?mode=dir")
        self.assertEqual(body["path"], "")
        await self.request("/api/models", "POST", {
            "name": "m", "gpt_model_path": str(install / "GPT_weights" / "k.ckpt"), "sovits_model_path": "",
        })
        status, body = await self.request("/api/fs/list?mode=dir")
        self.assertEqual(body["path"], str(install))

        status, body = await self.request("/api/config/gpt_sovits_root", "POST", {"path": str(install / "GPT_weights")})
        self.assertEqual(status, 200)
        status, body = await self.request("/api/fs/list")
        self.assertEqual(body["path"], str(install / "GPT_weights"))
        status, _ = await self.request("/api/config/gpt_sovits_root", "POST", {"path": str(install / "missing")})
        self.assertEqual(status, 400)
        status, body = await self.request("/api/config/gpt_sovits_root", "POST", {"path": ""})
        self.assertEqual((status, body), (200, {"gpt_sovits_root": ""}))

        async with self.client.get(
            f"http://127.0.0.1:{self.port}/api/fs/list?path={quote(str(install))}",
            headers={"Host": "evil.example"},
        ) as response:
            self.assertEqual(response.status, 403)
        status, body = await self.request(
            "/api/config/gpt_sovits_root", "POST", {"path": ""},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual((status, body), (403, {"error": "forbidden_origin"}))


class _ListLogger:
    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append(message)


class BridgeDirectoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_profile_reaches_event_and_log(self):
        module = _load_plugin_module()
        logger = module.test_logger
        for messages in (logger.warnings, logger.errors, logger.infos):
            messages.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ktn = root / "ktn"
            write_wav(ktn / "开心" / "やった.wav", 5)
            write_wav(ktn / "开心" / "嬉しい.wav", 6)
            config = {
                "version": 1, "default_character": "琴音", "default_emotion": "开心",
                "models": {"ktn": {"name": "ktn", "gpt_model_path": "", "sovits_model_path": "",
                                   "reference_audio_root": str(ktn)}},
                "characters": {"琴音": {"name": "琴音", "model_name": "ktn", "default_emotion": "开心",
                                       "emotions": {}}},
            }
            (root / "data").mkdir()
            (root / "data" / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            plugin = _ready_plugin(module, voice=True)
            plugin._core._emotion_store = ConfigStore(root / "data")
            event = _bridge_event(module, "request")
            results = await _complete_bridge_reply(plugin, event, '[情绪:开心]☆‹日本語›"中文"')

            self.assertEqual([result.text for result in results], ["中文"])
            profile = plugin._core._tts.last_profile
            self.assertEqual(profile["source"], "directory")
            self.assertIn(profile["prompt_text"], {"やった", "嬉しい"})
            self.assertEqual(
                logger.infos,
                [f"[STBridge] TTS emotion requested=开心 used=开心 source=directory candidates=2 "
                 f"pick={1 if profile['prompt_text'] == 'やった' else 2} fallback=False reason=none"],
            )
            logs = "".join(logger.infos + logger.warnings)
            for secret in ("やった", "嬉しい", str(ktn), "日本語", "中文"):
                self.assertNotIn(secret, logs)


if __name__ == "__main__":
    unittest.main()
