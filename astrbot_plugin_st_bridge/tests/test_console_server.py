import asyncio
import functools
import io
import json
import socket
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from aiohttp import ClientConnectorError, ClientSession, FormData, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import console_server
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import (
    ConsoleService,
    ConsoleServiceState,
    create_console_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import BridgeRecoveryGate
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from test_plugin_lifecycle import submodule
from test_plugin_lifecycle import _load_plugin_module


LEGACY_TOKEN = "c" * 43


def _unused_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 16_000)
    return output.getvalue()


class ConsoleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-console-")
        self.data_dir = Path(self.directory.name) / "plugin_data"
        self.host_dir = Path(self.directory.name) / "host"
        self.host_dir.mkdir()
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        self.service = None
        await self.start_console()

    async def asyncTearDown(self):
        await self.stop_console()
        await self.client.close()
        self.directory.cleanup()

    async def start_console(self):
        self.port = _unused_loopback_port()
        self.store = ConfigStore(self.data_dir)
        app = create_console_app(self.store)
        self.service = ConsoleService(app, port=self.port)
        await self.service.start()

    async def stop_console(self):
        if self.service is not None:
            await self.service.stop()
            self.service = None

    async def restart_console(self):
        await self.stop_console()
        await self.start_console()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    async def request(self, path, method="GET", payload=None, headers=None):
        kwargs = {"headers": dict(headers or {})}
        if payload is not None:
            kwargs["json"] = payload
        async with self.client.request(method, self.url(path), **kwargs) as response:
            return response.status, await response.json()

    async def create_model_and_character(self, model="audio-model", character="AudioRole"):
        status, _ = await self.request("/api/models", "POST", {
            "name": model,
            "gpt_model_path": r"C:\models\audio.ckpt",
            "sovits_model_path": r"C:\models\audio.pth",
        })
        self.assertEqual(status, 200)
        status, _ = await self.request("/api/characters", "POST", {
            "name": character,
            "model_name": model,
            "default_emotion": "neutral",
            "emotions": {},
        })
        self.assertEqual(status, 200)

    async def upload(self, fields, file_name, content, headers=None):
        form = FormData()
        for key, value in fields.items():
            form.add_field(key, value)
        form.add_field("file", content, filename=file_name, content_type="audio/wav")
        async with self.client.post(
            self.url("/api/audio/upload"),
            data=form,
            headers=headers or {},
        ) as response:
            return response.status, await response.json()

    async def raw_upload(self, raw_file_name):
        boundary = "stbridgeboundary"
        fields = {"character": "AudioRole", "language": "Japanese", "emotion": "happy"}
        chunks = []
        for key, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{raw_file_name}"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode()
        )
        chunks.append(make_wav())
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        async with self.client.post(
            self.url("/api/audio/upload"),
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        ) as response:
            return response.status, await response.json()

    async def test_listener_is_loopback_only(self):
        sockets = self.service.site._server.sockets
        self.assertTrue(sockets)
        self.assertEqual(sockets[0].getsockname()[0], "127.0.0.1")
        self.assertEqual(sockets[0].getsockname()[1], self.port)

    async def test_page_and_static_assets_are_served(self):
        async with self.client.get(self.url("/")) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn("ST Bridge", await response.text())
        for asset in ("/static/css/theme.css", "/static/css/components.css", "/static/js/app.js"):
            async with self.client.get(self.url(asset)) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(await response.read()), 100)

    async def raw_get(self, request_head: bytes) -> tuple[int, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(request_head)
            await writer.drain()
            data = await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
        status = int(data.split(b" ", 2)[1])
        return status, data.split(b"\r\n\r\n", 1)[1]

    async def test_allowed_hosts_are_served(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}"):
            with self.subTest(host=host):
                status, body = await self.request("/api/config", headers={"Host": host})
                self.assertEqual(status, 200)
                self.assertEqual(body["version"], 1)

    async def test_disallowed_hosts_are_forbidden_on_every_path(self):
        hosts = (
            "evil.example:11453",
            f"evil.example:{self.port}",
            "127.0.0.1",
            "localhost",
            f"127.0.0.1:{self.port + 1}",
            f"localhost:{self.port}.evil.example",
        )
        for path in ("/", "/static/js/app.js", "/api/health", "/api/config", "/api/missing"):
            for host in hosts:
                with self.subTest(path=path, host=host):
                    async with self.client.get(
                        self.url(path),
                        headers={"Host": host},
                    ) as response:
                        self.assertEqual(response.status, 403)
                        self.assertEqual(await response.json(), {"error": "forbidden_host"})

    async def test_missing_or_duplicate_host_is_forbidden(self):
        status, body = await self.raw_get(b"GET /api/health HTTP/1.0\r\n\r\n")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "forbidden_host"})
        status, body = await self.raw_get(
            b"GET /api/health HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.port}\r\n".encode()
            + b"Host: evil.example\r\nConnection: close\r\n\r\n"
        )
        self.assertIn(status, (400, 403))
        status, _ = await self.raw_get(
            f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nConnection: close\r\n\r\n".encode()
        )
        self.assertEqual(status, 200)

    async def test_api_needs_no_token_and_ignores_stale_credentials(self):
        status, body = await self.request("/api/health")
        self.assertEqual((status, body), (200, {"ok": True}))
        for headers in (
            {},
            {"Authorization": f"Bearer {LEGACY_TOKEN}"},
            {"Authorization": "Bearer stale"},
            {"X-Console-Token": "stale"},
        ):
            with self.subTest(headers=headers):
                status, body = await self.request("/api/config", headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(body["version"], 1)
        status, body = await self.request("/api/does-not-exist")
        self.assertEqual((status, body), (404, {"error": "not found"}))

    async def test_cross_site_writes_are_forbidden(self):
        await self.create_model_and_character()
        bad_origins = (
            {"Origin": "http://evil.example"},
            {"Origin": f"http://evil.example:{self.port}"},
            {"Origin": f"http://127.0.0.1:{self.port + 1}"},
            {"Origin": f"https://127.0.0.1:{self.port}"},
            {"Origin": "null"},
        )
        writes = (
            ("POST", "/api/models", {"name": "evil"}),
            ("PUT", "/api/characters/AudioRole/emotions/evil", {}),
            ("POST", "/api/config/defaults", {"character": "", "emotion": "neutral"}),
            ("DELETE", "/api/characters/AudioRole", None),
            ("DELETE", "/api/models/audio-model", None),
        )
        for headers in bad_origins:
            for method, path, payload in writes:
                with self.subTest(origin=headers["Origin"], method=method, path=path):
                    status, body = await self.request(path, method, payload, headers=headers)
                    self.assertEqual((status, body), (403, {"error": "forbidden_origin"}))
            with self.subTest(origin=headers["Origin"], upload=True):
                status, body = await self.upload(
                    {"character": "AudioRole", "emotion": "evil"}, "evil.wav", make_wav(), headers=headers,
                )
                self.assertEqual((status, body), (403, {"error": "forbidden_origin"}))

        status, config = await self.request("/api/config")
        self.assertEqual(set(config["models"]), {"audio-model"})
        self.assertEqual(config["characters"]["AudioRole"]["emotions"], {})
        self.assertEqual(list((self.data_dir / "audio").rglob("*.wav")), [])

    async def test_duplicate_origin_headers_are_forbidden(self):
        async with self.client.post(
            self.url("/api/models"),
            json={"name": "dup"},
            headers=[("Origin", f"http://127.0.0.1:{self.port}"), ("Origin", "http://evil.example")],
        ) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual(await response.json(), {"error": "forbidden_origin"})

    async def test_same_origin_and_originless_writes_are_allowed(self):
        for index, headers in enumerate((
            {"Origin": f"http://127.0.0.1:{self.port}"},
            {"Origin": f"http://localhost:{self.port}"},
            {"Origin": f"HTTP://LOCALHOST:{self.port}"},
            {},
        )):
            with self.subTest(headers=headers):
                status, _ = await self.request(
                    "/api/models", "POST", {"name": f"model-{index}"}, headers=headers,
                )
                self.assertEqual(status, 200)
        await self.create_model_and_character()
        status, _ = await self.upload(
            {"character": "AudioRole", "emotion": "happy"},
            "ok.wav",
            make_wav(),
            headers={"Origin": f"http://localhost:{self.port}"},
        )
        self.assertEqual(status, 200)

    async def test_cross_site_reads_get_no_cors_headers(self):
        async with self.client.get(
            self.url("/api/config"), headers={"Origin": "http://evil.example"},
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def test_model_character_emotion_and_persistence(self):
        status, body = await self.request("/api/models", "POST", {
            "name": "kotone-test",
            "gpt_model_path": r"C:\models\kotone.ckpt",
            "sovits_model_path": r"C:\models\kotone.pth",
            "reference_audio_root": "",
        })
        self.assertEqual(status, 200)
        self.assertFalse(body["model"]["files"]["gpt_weights"])
        status, _ = await self.request("/api/characters", "POST", {
            "name": "琴音",
            "model_name": "kotone-test",
            "default_emotion": "neutral",
            "emotions": {},
        })
        self.assertEqual(status, 200)
        status, body = await self.request("/api/characters/%E7%90%B4%E9%9F%B3/emotions/neutral", "PUT", {
            "prompt_text": "こんにちは",
            "prompt_text_lang": "日文",
            "text_lang": "日文",
            "top_k": 10,
            "top_p": 1,
            "temperature": 1,
            "speed_factor": 1,
            "split_sentence": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["emotion"]["text_lang"], "日文")
        status, _ = await self.request("/api/characters/%E7%90%B4%E9%9F%B3/emotions/%E8%BD%BB%E5%BF%AB", "POST", {})
        self.assertEqual(status, 200)
        status, _ = await self.request("/api/characters/%E7%90%B4%E9%9F%B3/emotions/%E8%BD%BB%E5%BF%AB", "DELETE")
        self.assertEqual(status, 200)
        status, body = await self.request("/api/characters/%E7%90%B4%E9%9F%B3/emotions/neutral", "DELETE")
        self.assertEqual((status, body), (400, {"error": "cannot delete the default emotion"}))
        status, data = await self.request("/api/characters")
        self.assertEqual(status, 200)
        character = next(item for item in data["characters"] if item["name"] == "琴音")
        self.assertEqual(set(character["emotions"]), {"neutral"})
        status, _ = await self.request("/api/config/defaults", "POST", {"character": "琴音", "emotion": "neutral"})
        self.assertEqual(status, 200)
        self.assertTrue((self.data_dir / "config.json.bak").exists())

        await self.restart_console()
        status, config = await self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(config["default_character"], "琴音")
        self.assertNotIn("auth_token", config)
        self.assertEqual(config["characters"]["琴音"]["emotions"]["neutral"]["prompt_text"], "こんにちは")
        self.assertFalse(list(self.data_dir.glob("config.*.tmp")))

        status, _ = await self.request("/api/characters/%E7%90%B4%E9%9F%B3", "DELETE")
        self.assertEqual(status, 200)
        status, config = await self.request("/api/config")
        self.assertEqual(config["default_character"], "")
        status, _ = await self.request("/api/characters/%E7%90%B4%E9%9F%B3", "DELETE")
        self.assertEqual(status, 404)

    async def test_model_edit_and_delete(self):
        status, _ = await self.request("/api/models", "POST", {
            "name": "editable-model",
            "gpt_model_path": r"C:\models\old.ckpt",
            "sovits_model_path": r"C:\models\old.pth",
        })
        self.assertEqual(status, 200)
        status, body = await self.request("/api/models", "POST", {
            "name": "editable-model",
            "gpt_model_path": r"C:\models\new.ckpt",
            "sovits_model_path": r"C:\models\new.pth",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["model"]["gpt_model_path"], r"C:\models\new.ckpt")
        await self.request("/api/characters", "POST", {
            "name": "BoundRole",
            "model_name": "editable-model",
            "default_emotion": "neutral",
            "emotions": {},
        })
        status, _ = await self.request("/api/models/editable-model", "DELETE")
        self.assertEqual(status, 200)
        status, characters = await self.request("/api/characters")
        role = next(item for item in characters["characters"] if item["name"] == "BoundRole")
        self.assertEqual(role["model_name"], "")
        status, _ = await self.request("/api/models/editable-model", "DELETE")
        self.assertEqual(status, 404)

    async def test_model_status_reports_existing_host_files(self):
        gpt = self.host_dir / "ktn.ckpt"
        sovits = self.host_dir / "ktn.pth"
        gpt.write_bytes(b"gpt")
        sovits.write_bytes(b"sovits")
        status, body = await self.request("/api/models", "POST", {
            "name": "host-model",
            "gpt_model_path": str(gpt),
            "sovits_model_path": str(sovits),
            "reference_audio_root": str(self.host_dir),
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            body["model"]["files"],
            {"gpt_weights": True, "sovits_weights": True, "reference_audios": True},
        )
        status, body = await self.request("/api/models")
        self.assertEqual(status, 200)
        self.assertTrue(body["models"][0]["valid"])

    async def test_invalid_paths_and_parameters_fail_closed(self):
        status, _ = await self.request("/api/models", "POST", {
            "name": "bad",
            "gpt_model_path": r"C:\models\bad.txt",
            "sovits_model_path": r"C:\models\bad.pth",
        })
        self.assertEqual(status, 400)
        status, _ = await self.request("/api/models", "POST", {"name": "bad/name"})
        self.assertEqual(status, 400)
        await self.create_model_and_character("invalid-model", "InvalidRole")
        status, _ = await self.request("/api/characters/Missing/emotions/neutral", "PUT", {})
        self.assertEqual(status, 404)
        for emotion, profile in (
            ("neutral", {"top_k": 0}),
            ("happy", {"prompt_text_lang": "not-a-gpt-sovits-language"}),
            ("sad", {"split_sentence": "true"}),
        ):
            with self.subTest(emotion=emotion):
                status, _ = await self.request(
                    f"/api/characters/InvalidRole/emotions/{emotion}", "PUT", profile,
                )
                self.assertEqual(status, 400)
        status, _ = await self.request("/api/characters/InvalidRole/emotions/a%20b", "PUT", {})
        self.assertEqual(status, 400)
        status, _ = await self.request("/api/characters", "POST", {
            "name": "Other", "model_name": "missing-model",
        })
        self.assertEqual(status, 400)
        async with self.client.post(
            self.url("/api/models"),
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        ) as response:
            self.assertEqual(response.status, 400)
            self.assertEqual(await response.json(), {"error": "invalid JSON"})
        status, body = await self.request("/api/models", "POST", [1])
        self.assertEqual((status, body), (400, {"error": "JSON object required"}))

    async def test_audio_upload_stream_and_delete(self):
        await self.create_model_and_character()
        status, uploaded = await self.upload(
            {"character": "AudioRole", "language": "Japanese", "emotion": "happy", "prompt_text": "こんにちは"},
            "happy_hello.wav",
            make_wav(),
        )
        self.assertEqual(status, 200)
        status, listing = await self.request("/api/audio?character=AudioRole")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["audios"]), 1)
        relative = listing["audios"][0]["relative_path"]
        self.assertEqual(uploaded["relative_path"], relative)
        self.assertEqual(relative, "happy/こんにちは.wav")
        self.assertTrue(Path(uploaded["path"]).is_relative_to(self.data_dir / "audio"))
        audio = listing["audios"][0]
        self.assertEqual(audio["emotion"], "happy")
        self.assertEqual(audio["prompt_text"], "こんにちは")
        self.assertEqual(audio["duration"], 1.0)
        self.assertTrue(audio["out_of_range"])

        # Directory discovery replaces writing ref_audio_path into the config.
        status, config = await self.request("/api/config")
        self.assertEqual(config["characters"]["AudioRole"]["emotions"], {})

        stream = f"/api/audio/stream?model=audio-model&path={quote(relative)}"
        async with self.client.get(self.url(stream)) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.read())[:4], b"RIFF")

        status, _ = await self.request(f"/api/audio?model=audio-model&path={quote(relative)}", "DELETE")
        self.assertEqual(status, 200)
        status, listing = await self.request("/api/audio?character=AudioRole")
        self.assertEqual(listing["audios"], [])

    async def test_upload_rejects_invalid_requests(self):
        await self.create_model_and_character()
        cases = (
            ({"character": "Missing", "language": "Japanese", "emotion": "happy"}, "a.wav", 400),
            ({"character": "AudioRole", "language": "Japanese", "emotion": "a b"}, "a.wav", 400),
            ({"character": "AudioRole", "language": "Japanese", "emotion": "happy"}, "a.exe", 400),
        )
        for fields, file_name, expected in cases:
            with self.subTest(fields=fields, file_name=file_name):
                status, _ = await self.upload(fields, file_name, make_wav())
                self.assertEqual(status, expected)
        async with self.client.post(
            self.url("/api/audio/upload"),
            json={"character": "AudioRole"},
        ) as response:
            self.assertEqual(response.status, 415)

    async def test_upload_size_limit(self):
        await self.create_model_and_character()
        with patch.object(console_server, "MAX_BODY_BYTES", 1024):
            status, body = await self.upload(
                {"character": "AudioRole", "language": "Japanese", "emotion": "happy"},
                "big.wav",
                b"\x00" * 4096,
            )
        self.assertEqual((status, body), (413, {"error": "request body too large"}))
        self.assertEqual(list((self.data_dir / "audio").rglob("*.wav")), [])

    async def test_path_traversal_is_rejected(self):
        await self.create_model_and_character()
        secret = self.data_dir / "secret.wav"
        secret.write_bytes(b"RIFF-secret")

        for raw_name, expected in (
            ("../../../secret.wav", "happy/secret.wav"),
            # Content-Disposition unescapes backslashes, so send them escaped.
            ("..\\\\..\\\\..\\\\other.wav", "happy/other.wav"),
        ):
            with self.subTest(raw_name=raw_name):
                status, uploaded = await self.raw_upload(raw_name)
                self.assertEqual(status, 200)
                self.assertEqual(uploaded["relative_path"], expected)
        self.assertEqual(secret.read_bytes(), b"RIFF-secret")
        self.assertFalse((self.data_dir / "other.wav").exists())

        for relative in ("../../secret.wav", "..\\..\\secret.wav", str(secret), "../../config.json"):
            with self.subTest(relative=relative):
                stream = f"/api/audio/stream?model=audio-model&path={quote(relative)}"
                async with self.client.get(self.url(stream)) as response:
                    self.assertEqual(response.status, 400)
                status, _ = await self.request(
                    f"/api/audio?model=audio-model&path={quote(relative)}", "DELETE",
                )
                self.assertEqual(status, 400)
        self.assertTrue(secret.exists())

        for path in (
            "/static/../console_server.py",
            "/static/%2e%2e/console_server.py",
            "/static/..%5Cconsole_server.py",
            "/static/css/../../console_server.py",
        ):
            with self.subTest(path=path):
                async with self.client.get(self.url(path)) as response:
                    self.assertEqual(response.status, 404)
                    self.assertNotIn(b"ConsoleService", await response.read())


class ConsoleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_port_in_use_raises_and_leaves_nothing_running(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
                busy.bind(("127.0.0.1", 0))
                busy.listen()
                port = busy.getsockname()[1]
                service = ConsoleService(create_console_app(store), port=port)
                with self.assertRaises(OSError):
                    await service.start()
                self.assertEqual(service.state, ConsoleServiceState.NEW)
                self.assertIsNone(service.site)

    async def test_stop_closes_listener_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            port = _unused_loopback_port()
            service = ConsoleService(
                create_console_app(ConfigStore(Path(directory))),
                port=port,
            )
            await service.start()
            await service.stop()
            await service.stop()
            self.assertEqual(service.state, ConsoleServiceState.STOPPED)
            with self.assertRaises(RuntimeError):
                await service.start()
            async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                with self.assertRaises(ClientConnectorError):
                    await client.get(f"http://127.0.0.1:{port}/api/health")


class _FakeBridgeService:
    def __init__(self, app, config, gate):
        self.gate = gate
        self.state = HttpServiceState.NEW

    async def start(self):
        self.state = HttpServiceState.RUNNING

    async def stop(self):
        self.gate.begin_shutdown()
        self.gate.mark_stopped()
        self.state = HttpServiceState.STOPPED


class PluginConsoleLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.module.test_logger.errors.clear()
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-plugin-data-")
        self.data_root = Path(self.directory.name)
        self.requested_names = []

        def get_data_dir(name):
            self.requested_names.append(name)
            path = self.data_root / "plugin_data" / name
            path.mkdir(parents=True, exist_ok=True)
            return path

        self.star_tools = SimpleNamespace(get_data_dir=get_data_dir)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def plugin(self):
        # Legacy token values left in a saved config must be ignored.
        config = {
            "bridge_token": "a" * 43,
            "console_token": LEGACY_TOKEN,
            "operator_platform_id": "qq-platform",
            "operator_sender_id": "qq-user",
        }
        return self.module.STBridgePlugin(context=None, config=config)

    def patches(self, port, star_tools=None):
        module = self.module
        return (
            patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", _FakeBridgeService),
            patch.object(submodule(module, "mixin_lifecycle"), "ConsoleService", functools.partial(ConsoleService, port=port)),
            patch.object(module.star, "StarTools", star_tools or self.star_tools, create=True),
        )

    async def test_console_serves_before_bridge_recovery_and_stops_with_plugin(self):
        port = _unused_loopback_port()
        plugin = self.plugin()
        bridge_patch, console_patch, tools_patch = self.patches(port)
        with bridge_patch, console_patch, tools_patch:
            await plugin.initialize()
            # The emotion config and the affinity store each resolve the plugin data dir.
            self.assertEqual(self.requested_names, ["astrbot_plugin_st_bridge"] * 2)
            self.assertEqual(plugin._core._recovery_gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
            async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                async with client.get(f"http://127.0.0.1:{port}/api/config") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())["version"], 1)
            self.assertTrue(
                (self.data_root / "plugin_data" / "astrbot_plugin_st_bridge" / "config.json").exists()
            )
            await plugin.terminate()
            self.assertIsNone(plugin._core._console_service)
            self.assertIsNone(plugin._core._http_service)
            async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                with self.assertRaises(ClientConnectorError):
                    await client.get(f"http://127.0.0.1:{port}/api/health")
        self.assertEqual(self.module.test_logger.errors, [])

    async def assert_bridge_unaffected(self, plugin):
        self.assertIsNone(plugin._core._console_service)
        self.assertEqual(plugin._core._http_service.state, HttpServiceState.RUNNING)
        self.assertEqual(plugin._core._recovery_gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
        self.assertIsNotNone(plugin._core._request_state_manager)
        await plugin.terminate()
        self.assertIsNone(plugin._core._http_service)

    async def test_console_port_in_use_does_not_affect_bridge(self):
        plugin = self.plugin()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            bridge_patch, console_patch, tools_patch = self.patches(busy.getsockname()[1])
            with bridge_patch, console_patch, tools_patch:
                await plugin.initialize()
                self.assertEqual(
                    self.module.test_logger.errors,
                    ["[STBridge] Console unavailable (ConsoleStartFailed: OSError)"],
                )
                await self.assert_bridge_unaffected(plugin)

    async def test_corrupt_console_config_does_not_affect_bridge(self):
        data_dir = self.data_root / "plugin_data" / "astrbot_plugin_st_bridge"
        data_dir.mkdir(parents=True)
        (data_dir / "config.json").write_bytes(b"{ invalid json\n")
        plugin = self.plugin()
        bridge_patch, console_patch, tools_patch = self.patches(_unused_loopback_port())
        with bridge_patch, console_patch, tools_patch:
            await plugin.initialize()
            self.assertEqual(
                self.module.test_logger.errors,
                ["[STBridge] Console unavailable (ConsoleStartFailed: RuntimeError)"],
            )
            await self.assert_bridge_unaffected(plugin)
        self.assertEqual((data_dir / "config.json").read_bytes(), b"{ invalid json\n")

    async def test_data_dir_failure_does_not_affect_bridge_or_log_secrets(self):
        def failing_get_data_dir(name):
            raise ValueError(f"cannot resolve {LEGACY_TOKEN}")

        plugin = self.plugin()
        bridge_patch, console_patch, tools_patch = self.patches(
            _unused_loopback_port(),
            SimpleNamespace(get_data_dir=failing_get_data_dir),
        )
        with bridge_patch, console_patch, tools_patch:
            await plugin.initialize()
            self.assertEqual(
                self.module.test_logger.errors,
                [
                    "[STBridge] Console unavailable (ConsoleStartFailed: OtherError)",
                    "[STBridge] Affinity unavailable (AffinityStoreFailed: OtherError)",
                ],
            )
            self.assertNotIn(LEGACY_TOKEN, "".join(self.module.test_logger.errors))
            await self.assert_bridge_unaffected(plugin)

    async def test_console_is_not_started_when_bridge_start_fails(self):
        class FailingBridgeService(_FakeBridgeService):
            async def start(self):
                raise OSError("bridge port in use")

        started = []
        plugin = self.plugin()
        with patch.object(submodule(self.module, "mixin_lifecycle"), "LocalHttpService", FailingBridgeService), \
                patch.object(submodule(self.module, "mixin_lifecycle"), "ConsoleService", lambda *a, **k: started.append(1)), \
                patch.object(self.module.star, "StarTools", self.star_tools, create=True):
            with self.assertRaises(OSError):
                await plugin.initialize()
        self.assertEqual(started, [])
        self.assertIsNone(plugin._core._console_service)

    async def test_console_stop_failure_still_stops_bridge(self):
        class BrokenConsole:
            async def stop(self):
                raise OSError("cleanup failed")

        plugin = self.plugin()
        with patch.object(submodule(self.module, "mixin_lifecycle"), "LocalHttpService", _FakeBridgeService), \
                patch.object(self.module.star, "StarTools", self.star_tools, create=True), \
                patch.object(submodule(self.module, "mixin_lifecycle"), "ConsoleService", functools.partial(
                    ConsoleService, port=_unused_loopback_port(),
                )):
            await plugin.initialize()
            real_console = plugin._core._console_service
            plugin._core._console_service = BrokenConsole()
            try:
                await plugin.terminate()
            finally:
                await real_console.stop()
        self.assertEqual(
            self.module.test_logger.errors,
            ["[STBridge] Console stop failed (ConsoleStopFailed: OSError)"],
        )
        self.assertIsNone(plugin._core._http_service)
        self.assertIsNone(plugin._core._console_service)


if __name__ == "__main__":
    unittest.main()
