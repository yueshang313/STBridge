import copy
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, default_tts_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_coordinator import TtsCoordinator
from test_console_server import _unused_loopback_port
from test_tts_service import FakeHost, FakeJob, RecordingLogger


class TtsConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-tts-console-")
        root = Path(self.directory.name)
        self.data_dir = root / "plugin_data"
        iro = root / "Irodori-TTS"
        (iro / ".venv" / "Scripts").mkdir(parents=True)
        (iro / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        server = iro / "Irodori-TTS-Server"
        entry = server / "src" / "irodori_openai_tts" / "__main__.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("", encoding="utf-8")
        gpt = root / "GPT-SoVITS"
        (gpt / "runtime").mkdir(parents=True)
        (gpt / "runtime" / "python.exe").write_bytes(b"")
        self.host = FakeHost()
        self.store = ConfigStore(self.data_dir)
        settings = default_tts_settings()
        settings["irodori"].update(launch_dir=str(iro), server_dir=str(server))
        settings["gpt_sovits"].update(launch_dir=str(gpt))
        await self.store.update(lambda config: config.__setitem__("tts", settings))
        self.tts = TtsCoordinator(
            self.store.config["tts"],
            data_dir=self.data_dir,
            logger=RecordingLogger(),
            manager_kwargs=dict(
                launcher=self.host.launcher, probe=self.host.probe, killer=self.host.killer,
                job_factory=FakeJob, clock=self.host.clock, sleep=self.host.sleep,
            ),
        )
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        self.port = _unused_loopback_port()
        self.service = ConsoleService(create_console_app(self.store, tts=self.tts), port=self.port)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        await self.client.close()
        self.directory.cleanup()

    async def request(self, path, method="GET", payload=None, headers=None):
        kwargs = {"headers": dict(headers or {})}
        if payload is not None:
            kwargs["json"] = payload
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", **kwargs) as response:
            return response.status, await response.json()

    def saved_config(self):
        return json.loads((self.data_dir / "config.json").read_text(encoding="utf-8"))

    @staticmethod
    def _wav(seconds=0.5, rate=8000):
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"\x00\x00" * int(seconds * rate))
        return buffer.getvalue()

    async def test_tts_test_synthesizes_one_sentence_and_reports_it(self):
        seen = {}

        async def post(payload):
            seen["payload"] = payload
            return self._wav(0.5)

        self.tts._post_irodori = post
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual(status, 200)
        self.assertEqual((body["ok"], body["engine"], body["duration_seconds"]), (True, "irodori", 0.5))
        self.assertGreater(body["bytes"], 100)
        self.assertIn("elapsed_ms", body)
        self.assertEqual(seen["payload"]["input"].rstrip("😊😆🫣😲🤔😠🥺"), "こんにちは、音声のテストです。")
        # a text, a character and an emotion are accepted; the default character has the emotion's caption
        status, body = await self.request("/api/tts/test", "POST", {"text": "テスト", "emotion": "开心"})
        self.assertEqual((status, body["emotion"]), (200, "开心"))
        self.assertIn("caption", seen["payload"]["irodori"])

    async def test_tts_test_returns_the_audio_only_when_asked(self):
        import base64

        wav = self._wav(0.25)

        async def post(payload):
            return wav

        self.tts._post_irodori = post
        status, body = await self.request("/api/tts/test", "POST")
        self.assertNotIn("audio_base64", body)  # the default reply is unchanged (the selftest depends on it)
        self.assertNotIn("content_type", body)
        status, body = await self.request("/api/tts/test", "POST", {"return_audio": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["content_type"], "audio/wav")
        self.assertEqual(base64.b64decode(body["audio_base64"]), wav)
        status, _ = await self.request("/api/tts/test", "POST", {"return_audio": "yes"})
        self.assertEqual(status, 400)

    async def test_tts_test_can_use_unsaved_emotion_fields_for_one_synthesis(self):
        seen = {}

        async def post(payload):
            seen["payload"] = payload
            return self._wav(0.25)

        self.tts._post_irodori = post
        before = self.saved_config()
        status, body = await self.request("/api/tts/test", "POST", {
            "emotion": "开心", "emotion_profile": {"caption": "臨時のキャプション", "emoji": "😆"},
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(seen["payload"]["irodori"]["caption"], "臨時のキャプション")
        self.assertTrue(seen["payload"]["input"].startswith("😆"))
        # Nothing was written, and the next plain call uses the saved caption again.
        self.assertEqual(self.saved_config(), before)
        await self.request("/api/tts/test", "POST", {"emotion": "开心"})
        self.assertNotEqual(seen["payload"]["irodori"]["caption"], "臨時のキャプション")

    async def test_tts_test_checks_unsaved_emotion_fields_like_a_save(self):
        bad = (
            {"emotion_profile": {"caption": "x"}},
            {"emotion": "开心", "emotion_profile": "x"},
            {"emotion": "开心", "emotion_profile": {"nonsense": 1}},
            {"emotion": "开心", "emotion_profile": {"top_k": 0}},
            {"emotion": "开心", "emotion_profile": {"caption": "x" * 301}},
            {"emotion": "开心", "emotion_profile": {"emoji": "a\nb"}},
        )
        for payload in bad:
            status, _ = await self.request("/api/tts/test", "POST", payload)
            self.assertEqual(status, 400, payload)

    async def test_tts_test_failure_during_the_cooldown_says_how_long(self):
        self.host.exit_code = 2
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual(status, 502)
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual((status, body["error"]), (502, "ServiceStartError"))
        self.assertGreater(body["cooldown_seconds"], 0)

    async def test_tts_test_reports_errors_without_leaking_details(self):
        async def post(payload):
            raise RuntimeError("secret D-path")

        self.tts._post_irodori = post
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual((status, body["ok"], body["error"]), (502, False, "RuntimeError"))
        self.assertNotIn("secret", json.dumps(body))
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import ServiceStartError

        async def not_ready():
            raise ServiceStartError("service did not start")

        self.tts.ensure_service_ready = not_ready
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual((status, body["error"]), (502, "ServiceStartError"))

    async def test_tts_test_failure_carries_the_http_status_and_error_type_but_no_path(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_clients import IrodoriRequestError

        async def irodori_400(payload):
            raise IrodoriRequestError(400, "invalid_request_error")

        self.tts._post_irodori = irodori_400
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual((status, body["ok"], body["error"]), (502, False, "IrodoriRequestError"))
        self.assertEqual((body["status"], body["error_type"]), (400, "invalid_request_error"))

        import httpx

        async def sovits_500(payload):
            raise httpx.HTTPStatusError(
                "Server error for url D:/secret/model.pth",
                request=httpx.Request("POST", "http://127.0.0.1:9880/"),
                response=httpx.Response(500),
            )

        self.tts._post_irodori = sovits_500
        status, body = await self.request("/api/tts/test", "POST")
        self.assertEqual((status, body["error"], body["status"]), (502, "HTTPStatusError", 500))
        self.assertNotIn("error_type", body)
        self.assertNotIn("secret", json.dumps(body))

        async def plain(payload):
            raise RuntimeError("x")

        self.tts._post_irodori = plain
        status, body = await self.request("/api/tts/test", "POST")
        self.assertNotIn("status", body)

    async def test_tts_test_validates_the_input(self):
        for payload in ({"text": ""}, {"text": "あ" * 201}, {"text": 5}, {"emotion": "a/b"}):
            status, _ = await self.request("/api/tts/test", "POST", payload)
            self.assertEqual(status, 400, payload)
        status, _ = await self.request("/api/tts/test", "POST", {"character": "不存在"})
        self.assertEqual(status, 404)

    async def test_tts_test_is_unavailable_without_a_tts_coordinator(self):
        port = _unused_loopback_port()
        service = ConsoleService(create_console_app(self.store), port=port)
        await service.start()
        try:
            async with self.client.post(f"http://127.0.0.1:{port}/api/tts/test") as response:
                self.assertEqual(response.status, 503)
        finally:
            await service.stop()

    async def test_get_returns_settings_and_current_engine(self):
        status, body = await self.request("/api/tts")
        self.assertEqual(status, 200)
        self.assertEqual(body["engine"], "irodori")
        self.assertEqual(body["settings"], self.tts.public_settings())
        self.assertEqual(body["settings"]["irodori"]["port"], 8088)

    async def test_config_endpoint_exposes_the_tts_section(self):
        status, body = await self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["tts"]["engine"], "irodori")

    async def test_saving_settings_persists_and_applies_immediately(self):
        _, body = await self.request("/api/tts")
        settings = copy.deepcopy(body["settings"])
        settings["common"].update(prob=40.0, send_text_with_audio=True)
        settings["irodori"]["num_steps"] = 25
        status, saved = await self.request("/api/tts/settings", "POST", settings)
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"], settings)
        self.assertEqual(saved["stopped"], [])
        self.assertEqual(self.tts.common["prob"], 40.0)
        self.assertEqual(self.tts.settings["irodori"]["num_steps"], 25)
        self.assertEqual(self.saved_config()["tts"], settings)
        self.assertEqual(self.store.config["tts"], settings)

    async def test_switching_engine_stops_the_old_engines_own_service(self):
        status, started = await self.request("/api/tts/service/start", "POST")
        self.assertEqual((status, started["state"], started["engine"]), (200, "running", "irodori"))
        _, body = await self.request("/api/tts")
        settings = copy.deepcopy(body["settings"])
        settings["engine"] = "gpt_sovits"
        status, saved = await self.request("/api/tts/settings", "POST", settings)
        self.assertEqual(status, 200)
        self.assertEqual(saved["stopped"], ["irodori"])
        self.assertEqual(saved["engine"], "gpt_sovits")
        self.assertEqual(self.host.kills, [4321])
        _, service = await self.request("/api/tts/service")
        self.assertEqual((service["engine"], service["service_name"], service["state"]),
                         ("gpt_sovits", "GPT-SoVITS", "stopped"))
        self.assertEqual(self.saved_config()["tts"]["engine"], "gpt_sovits")

    async def test_invalid_settings_are_rejected_without_saving_or_applying(self):
        _, body = await self.request("/api/tts")
        before = self.saved_config()
        for mutate in (
            lambda s: s.__setitem__("engine", "nope"),
            lambda s: s["common"].__setitem__("prob", 101),
            lambda s: s["irodori"].__setitem__("port", 0),
            lambda s: s["gpt_sovits"].__setitem__("gpt_model_path", r"D:\only.ckpt"),
        ):
            settings = copy.deepcopy(body["settings"])
            mutate(settings)
            status, error = await self.request("/api/tts/settings", "POST", settings)
            self.assertEqual(status, 400)
            self.assertIn("error", error)
        self.assertEqual(self.saved_config(), before)
        self.assertEqual(self.tts.settings, body["settings"])

    async def test_service_status_start_and_stop(self):
        status, down = await self.request("/api/tts/service")
        self.assertEqual((status, down["state"], down["service_name"]), (200, "stopped", "Irodori"))
        for key in ("pid", "host", "port", "launch_dir", "log_tail", "message"):
            self.assertIn(key, down)
        status, up = await self.request("/api/tts/service/start", "POST")
        self.assertEqual((status, up["state"], up["pid"]), (200, "running", 4321))
        status, stopped = await self.request("/api/tts/service/stop", "POST")
        self.assertEqual((status, stopped["state"]), (200, "stopped"))
        status, again = await self.request("/api/tts/service/stop", "POST")
        self.assertEqual(status, 409)
        self.assertIn("error", again)

    async def test_stop_refuses_an_external_service(self):
        self.host.port_open = True
        status, body = await self.request("/api/tts/service/stop", "POST")
        self.assertEqual(status, 409)
        self.assertIn("外部", body["error"])
        self.assertEqual(self.host.kills, [])

    async def test_start_failure_reports_the_reason(self):
        self.host.exit_code = 2
        status, body = await self.request("/api/tts/service/start", "POST")
        self.assertEqual(status, 502)
        self.assertIn("退出码 2", body["error"])
        self.assertEqual(body["service"]["state"], "failed")

    async def test_state_changing_routes_enforce_origin_and_host(self):
        status, body = await self.request(
            "/api/tts/service/start", "POST", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual((status, body["error"]), (403, "forbidden_origin"))
        _, current = await self.request("/api/tts")
        status, body = await self.request(
            "/api/tts/settings", "POST", current["settings"], headers={"Origin": "http://evil.example"}
        )
        self.assertEqual((status, body["error"]), (403, "forbidden_origin"))
        status, body = await self.request("/api/tts", headers={"Host": "evil.example"})
        self.assertEqual((status, body["error"]), (403, "forbidden_host"))
        self.assertEqual(self.host.launches, [])


class TtsUnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_answer_503_without_a_coordinator(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "plugin_data")
            port = _unused_loopback_port()
            service = ConsoleService(create_console_app(store), port=port)
            await service.start()
            try:
                async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                    for method, path in (
                        ("GET", "/api/tts"), ("POST", "/api/tts/settings"), ("GET", "/api/tts/service"),
                        ("POST", "/api/tts/service/start"), ("POST", "/api/tts/service/stop"),
                    ):
                        kwargs = {"json": default_tts_settings()} if path.endswith("settings") else {}
                        async with client.request(method, f"http://127.0.0.1:{port}{path}", **kwargs) as response:
                            self.assertEqual(response.status, 503, path)
            finally:
                await service.stop()


class ConsoleFrontendStructureTests(unittest.IsolatedAsyncioTestCase):
    FRONTEND = Path(__file__).resolve().parents[1] / "st_bridge_core" / "console_frontend"

    def test_every_element_id_used_by_app_js_exists_in_the_page(self):
        import re

        app = (self.FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
        page = (self.FRONTEND / "index.html").read_text(encoding="utf-8")
        dynamic = {"copy-preset", "preset-prompt", "character-select"}  # created by the script itself
        used = set(re.findall(r"\$\(\"([A-Za-z0-9_-]+)\"\)", app)) - dynamic
        missing = sorted(name for name in used if f'id="{name}"' not in page)
        self.assertEqual(missing, [])

    def test_navigation_has_every_page_and_the_new_irodori_page(self):
        import re

        page = (self.FRONTEND / "index.html").read_text(encoding="utf-8")
        nav = set(re.findall(r'class="nav-item[^"]*" data-page="([a-z]+)"', page))
        self.assertEqual(nav, {"overview", "install", "voice", "emotions", "stickers", "schedule", "sing", "affinity", "injection", "connect", "system"})
        for name in nav:
            self.assertIn(f'<section id="{name}" class="page', page)

    def test_every_element_id_the_script_looks_up_exists_in_the_page(self):
        import re

        page = (self.FRONTEND / "index.html").read_text(encoding="utf-8")
        script = (self.FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
        looked_up = set(re.findall(r'\$\("([a-z0-9-]+)"\)', script))
        present = set(re.findall(r'id="([a-z0-9-]+)"', page))
        # Created by the script itself (preset prompt box, character picker).
        self.assertEqual(looked_up - present - {"copy-preset", "character-select"}, set())
        self.assertIn("plan-list", looked_up)

    def test_page_text_no_longer_points_at_the_retired_plugin(self):
        page = (self.FRONTEND / "index.html").read_text(encoding="utf-8")
        app = (self.FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
        for stale in ("AstrBot 配置页", "GPT-SoVITS 插件", "gpt_sovits 插件"):
            self.assertNotIn(stale, page)
            self.assertNotIn(stale, app)

    async def test_logic_module_is_served_as_javascript(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "plugin_data")
            port = _unused_loopback_port()
            service = ConsoleService(create_console_app(store), port=port)
            await service.start()
            try:
                async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                    async with client.get(f"http://127.0.0.1:{port}/static/js/logic.js") as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn("javascript", response.headers["Content-Type"])
                        self.assertIn("export function visiblePages", await response.text())
            finally:
                await service.stop()


class StaticTypeAndAudioFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_scripts_keep_a_javascript_type_even_if_mimetypes_says_otherwise(self):
        from unittest.mock import patch

        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import console_server

        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "plugin_data")
            port = _unused_loopback_port()
            service = ConsoleService(create_console_app(store), port=port)
            await service.start()
            try:
                # Windows reads the registry, where ".js" is sometimes registered as text/plain.
                with patch.object(console_server.mimetypes, "guess_type", return_value=("text/plain", None)):
                    async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                        for path, expected in (
                            ("/static/js/app.js", "text/javascript"),
                            ("/static/js/logic.js", "text/javascript"),
                            ("/static/css/theme.css", "text/css"),
                        ):
                            async with client.get(f"http://127.0.0.1:{port}{path}") as response:
                                self.assertEqual(response.status, 200, path)
                                self.assertTrue(response.headers["Content-Type"].startswith(expected), path)
                                await response.read()
            finally:
                await service.stop()

    def test_static_content_type_table_and_fallback(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import _static_content_type

        self.assertEqual(_static_content_type("a.js"), "text/javascript; charset=utf-8")
        self.assertEqual(_static_content_type("a.MJS"), "text/javascript; charset=utf-8")
        self.assertEqual(_static_content_type("a.css"), "text/css; charset=utf-8")
        self.assertEqual(_static_content_type("a.html"), "text/html; charset=utf-8")
        self.assertEqual(_static_content_type("a.svg"), "image/svg+xml")
        self.assertEqual(_static_content_type("a.json"), "application/json")
        self.assertEqual(_static_content_type("a.png"), "image/png")
        self.assertEqual(_static_content_type("a.unknownext"), "application/octet-stream")

    def test_list_audio_reports_the_backends_usable_and_readable_flags(self):
        from test_discovery_and_browser import write_wav

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_root = root / "ktn"
            write_wav(audio_root / "开心" / "良い.wav", 5)
            write_wav(audio_root / "开心" / "長すぎる.wav", 12)
            write_wav(audio_root / "开心" / " (2).wav", 5)  # no reference text left after the suffix is stripped
            (audio_root / "开心" / "壊れた.wav").write_bytes(b"not a wav")
            store = ConfigStore(root / "plugin_data")
            store.config["models"]["ktn"] = {"name": "ktn", "reference_audio_root": str(audio_root)}
            audios = {item["filename"]: item for item in store.list_audio("ktn")}
            self.assertEqual(len(audios), 4)
            good = audios["良い.wav"]
            self.assertEqual((good["usable"], good["readable"], good["out_of_range"]), (True, True, False))
            long_ = audios["長すぎる.wav"]
            self.assertEqual((long_["usable"], long_["readable"], long_["out_of_range"]), (False, True, True))
            no_text = audios[" (2).wav"]
            self.assertEqual((no_text["usable"], no_text["readable"], no_text["prompt_text"]), (False, True, ""))
            broken = audios["壊れた.wav"]
            self.assertEqual((broken["usable"], broken["readable"]), (False, False))
            # the same flag the discovery used for random selection
            from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import discover_emotions

            discovered = {a["relative_path"]: a["usable"] for a in discover_emotions(audio_root)["开心"]}
            for item in audios.values():
                self.assertEqual(discovered[item["relative_path"]], item["usable"])


if __name__ == "__main__":
    unittest.main()
