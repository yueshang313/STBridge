"""P7a: /api/cover* routes of the console (fake worker, real service, library and cache)."""

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, FormData, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import cover_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_library import SONG_MAX_BYTES
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import CoverService
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_request import default_song_request_settings
from test_cover_service import FakeLogger, FakeManager

FLAC = b"fLaC" + b"\x00" * 64


def song_bytes(n):
    return FLAC + bytes([n]) * 16


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CoverApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        rvc = self.root / "rvc"
        (rvc / "assets" / "weights").mkdir(parents=True)
        (rvc / "assets" / "pymss_weights").mkdir(parents=True)
        (rvc / "logs" / "kotoneV2").mkdir(parents=True)
        for name in ("kotoneV2_e120_s12360.pth", "kotoneV2_e150_s15450.pth"):
            (rvc / "assets" / "weights" / name).write_bytes(b"w")
        (rvc / "assets" / "pymss_weights" / "model_bs_roformer_ep_368_sdr_12.9628.ckpt").write_bytes(b"w")
        (rvc / "logs" / "kotoneV2" / "kotoneV2_added_IVF1_v2.index").write_bytes(b"i")
        self.store = ConfigStore(self.root / "data")
        await self.store.update(lambda config: config["cover"].update(rvc_root=str(rvc), model="kotoneV2_e150_s15450.pth"))
        self.logger = FakeLogger()
        self.managers = []
        self.posts = []
        self.post_status = 200
        self.post_body = None

        def factory(**kwargs):
            manager = FakeManager(**kwargs)
            self.managers.append(manager)
            return manager

        async def post(url, payload, timeout):
            self.posts.append(payload)
            if self.post_status == 200:
                Path(payload["output_path"]).write_bytes(b"RIFFcover")
            return self.post_status, self.post_body or {"ok": True, "sep_ms": 1, "vc_ms": 2, "mix_ms": 3, "total_ms": 6, "duration": 4.5}

        self.cover = CoverService(
            data_dir=self.root / "data",
            get_settings=lambda: cover_settings(self.store.config),
            logger=self.logger,
            worker_script=self.root / "worker.py",
            manager_factory=factory,
            post=post,
            probe=lambda rvc_root, path: 100.0,
        )
        self.port = _unused_port()
        self.service = ConsoleService(create_console_app(self.store, cover=self.cover), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()
        self.directory.cleanup()

    async def call(self, path, method="GET", payload=None, headers=None):
        kwargs = {"headers": headers or {}}
        if payload is not None:
            kwargs["json"] = payload
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", **kwargs) as response:
            return response.status, await response.json()

    async def upload(self, files, headers=None):
        form = FormData()
        for name, content in files:
            form.add_field("file", content, filename=name)
        async with self.client.post(
            f"http://127.0.0.1:{self.port}/api/cover/songs/upload", data=form, headers=headers or {}
        ) as response:
            return response.status, await response.json()

    async def raw(self, path):
        async with self.client.get(f"http://127.0.0.1:{self.port}{path}") as response:
            return response.status, response.headers.get("Content-Type"), await response.read()

    async def test_overview_lists_models_without_the_worker_running(self):
        status, body = await self.call("/api/cover")
        self.assertEqual(status, 200)
        self.assertEqual(body["models"], ["kotoneV2_e120_s12360.pth", "kotoneV2_e150_s15450.pth"])
        self.assertEqual(
            [(m["id"], m["available"]) for m in body["separation_models"]],
            [("vocals-bs-roformer-368", True), ("vocals-bs-roformer-317", False)],
        )
        self.assertEqual([m["available"] for m in body["dereverb_models"]], [False, False])
        self.assertNotIn("uvr_models", body)
        self.assertEqual(body["settings"]["sep_model"], "vocals-bs-roformer-368")
        self.assertEqual((body["settings"]["vocal_reverb"], body["settings"]["keep_stems"]), (25, False))
        self.assertEqual(body["auto_index"], "kotoneV2_added_IVF1_v2.index")
        self.assertEqual(body["service"]["state"], "stopped")
        self.assertEqual((body["songs"], body["covers"]), ([], []))
        self.assertEqual(body["max_song_bytes"], SONG_MAX_BYTES)
        self.assertEqual(body["settings"]["model"], "kotoneV2_e150_s15450.pth")
        self.assertTrue(all(manager.started == 0 for manager in self.managers))  # looking starts nothing

    async def test_settings_are_saved_validated_and_launch_changes_stop_the_worker(self):
        await self.call("/api/cover/service/start", "POST")
        status, body = await self.call("/api/cover/settings", "POST", {**self.store.config["cover"], "f0_up_key": 4, "model": "kotoneV2_e120_s12360.pth"})
        self.assertEqual(status, 200)
        self.assertFalse(body["stopped"])
        self.assertEqual(self.store.config["cover"]["f0_up_key"], 4)
        status, body = await self.call("/api/cover/settings", "POST", {**self.store.config["cover"], "port": 8100})
        self.assertTrue(body["stopped"])
        self.assertEqual(self.managers[0].stops, 1)
        for bad in ({"port": 8088}, {"f0_up_key": 99}, {"model": "../x.pth"}, {"sep_model": ""}, {"dereverb_model": "bogus"}, {"vocal_reverb": 101}, {"f0_method": "magic"}):
            status, _ = await self.call("/api/cover/settings", "POST", {**self.store.config["cover"], **bad})
            self.assertEqual(status, 400, str(bad))
        self.assertEqual(self.store.config["cover"]["port"], 8100)

    async def test_new_separation_settings_round_trip(self):
        new = {"sep_model": "vocals-bs-roformer-317", "dereverb_model": "", "vocal_reverb": 25, "keep_stems": True, "f0_method": "fcpe"}
        status, body = await self.call("/api/cover/settings", "POST", {**self.store.config["cover"], **new})
        self.assertEqual(status, 200)
        saved = self.store.config["cover"]
        self.assertEqual({name: saved[name] for name in new}, new)
        self.assertNotIn("uvr_model", saved)
        self.assertNotIn("filter_radius", saved)
        _, overview = await self.call("/api/cover")
        self.assertEqual((overview["settings"]["vocal_reverb"], overview["settings"]["keep_stems"]), (25, True))

    async def test_old_keys_in_a_saved_body_are_dropped_and_old_f0_is_mapped(self):
        body = {**self.store.config["cover"], "uvr_model": "HP5_only_main_vocal", "filter_radius": 3, "f0_method": "harvest"}
        status, _ = await self.call("/api/cover/settings", "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.config["cover"]["f0_method"], "rmvpe")
        self.assertNotIn("uvr_model", self.store.config["cover"])

    async def test_service_start_stop(self):
        status, body = await self.call("/api/cover/service/start", "POST")
        self.assertEqual((status, body["state"]), (200, "running"))
        status, body = await self.call("/api/cover/service")
        self.assertEqual(body["state"], "running")
        status, body = await self.call("/api/cover/service/stop", "POST")
        self.assertEqual((status, body["state"]), (200, "stopped"))
        status, body = await self.call("/api/cover/service/stop", "POST")
        self.assertEqual(status, 409)

    async def test_start_failure_is_a_502_with_the_service_state(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import ServiceStartError

        self.cover.manager().start_error = ServiceStartError("启动超时")
        status, body = await self.call("/api/cover/service/start", "POST")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "启动超时")

    async def test_upload_list_stream_update_delete(self):
        status, body = await self.upload([("a.flac", song_bytes(1)), ("b.flac", song_bytes(2)), ("c.exe", b"MZ junk"), ("a2.flac", song_bytes(1))])
        self.assertEqual(status, 200)
        self.assertEqual([song["title"] for song in body["saved"]], ["a", "b"])
        self.assertEqual({item["filename"]: item["error"] for item in body["rejected"]}, {
            "c.exe": "unsupported audio format", "a2.flac": "song already in the library"})
        song = body["saved"][0]
        self.assertEqual(set(song), {"id", "title", "filename", "ext", "size", "duration", "clip_start", "added"})
        self.assertEqual(song["duration"], 100.0)

        status, content_type, content = await self.raw(f"/api/cover/songs/{song['id']}/audio")
        self.assertEqual((status, content_type, content), (200, "audio/flac", song_bytes(1)))

        status, body = await self.call(f"/api/cover/songs/{song['id']}", "POST", {"title": "新标题", "clip_start": 42.5})
        self.assertEqual((status, body["song"]["title"], body["song"]["clip_start"]), (200, "新标题", 42.5))
        status, body = await self.call(f"/api/cover/songs/{song['id']}", "POST", {"clip_start": None})
        self.assertIsNone(body["song"]["clip_start"])
        for bad in ({"title": ""}, {"clip_start": -5}, {"clip_start": 101}, {"clip_start": "x"}):
            status, _ = await self.call(f"/api/cover/songs/{song['id']}", "POST", bad)
            self.assertEqual(status, 400, str(bad))
        status, _ = await self.call("/api/cover/songs/ffffffff", "POST", {"title": "x"})
        self.assertEqual(status, 404)

        status, _ = await self.call(f"/api/cover/songs/{song['id']}", "DELETE")
        self.assertEqual(status, 200)
        status, _ = await self.call(f"/api/cover/songs/{song['id']}", "DELETE")
        self.assertEqual(status, 404)
        status, _, _ = await self.raw(f"/api/cover/songs/{song['id']}/audio")
        self.assertEqual(status, 404)
        _, overview = await self.call("/api/cover")
        self.assertEqual([item["title"] for item in overview["songs"]], ["b"])

    async def test_upload_when_everything_is_rejected_is_400(self):
        status, body = await self.upload([("x.mp3", b"nothing")])
        self.assertEqual(status, 400)
        self.assertIn("unsupported", body["error"])

    async def test_oversized_upload_is_rejected(self):
        status, body = await self.upload([("big.flac", FLAC + b"\x00" * SONG_MAX_BYTES)])
        self.assertEqual(status, 400)
        self.assertIn("too large", body["error"])

    async def test_song_responses_never_expose_paths_or_hashes(self):
        await self.upload([("a.flac", song_bytes(1))])
        _, overview = await self.call("/api/cover")
        text = json.dumps(overview, ensure_ascii=False)
        self.assertNotIn("sha256", text)
        self.assertNotIn('"file"', text)
        self.assertNotIn(str(self.root), text.replace(str(self.root / "rvc"), ""))

    async def test_test_sing_then_cache_then_play_and_delete(self):
        _, body = await self.upload([("a.flac", song_bytes(1))])
        song_id = body["saved"][0]["id"]
        status, result = await self.call("/api/cover/test", "POST", {"song_id": song_id, "f0_up_key": 3})
        self.assertEqual(status, 200)
        self.assertFalse(result["cached"])
        self.assertEqual(result["timings"], {"sep_ms": 1, "vc_ms": 2, "mix_ms": 3, "total_ms": 6})
        self.assertEqual(self.posts[0]["f0_up_key"], 3)
        key = result["cover"]["key"]

        status, again = await self.call("/api/cover/test", "POST", {"song_id": song_id, "f0_up_key": 3})
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.posts), 1)

        status, content_type, content = await self.raw(f"/api/cover/covers/{key}/audio")
        self.assertEqual((status, content_type, content), (200, "audio/wav", b"RIFFcover"))
        _, overview = await self.call("/api/cover")
        self.assertEqual([item["key"] for item in overview["covers"]], [key])
        self.assertIn("kotoneV2", overview["covers"][0]["summary"])
        status, _ = await self.call(f"/api/cover/covers/{key}", "DELETE")
        self.assertEqual(status, 200)
        status, _ = await self.call(f"/api/cover/covers/{key}", "DELETE")
        self.assertEqual(status, 404)
        status, _, _ = await self.raw(f"/api/cover/covers/{key}/audio")
        self.assertEqual(status, 404)

    async def test_test_sing_errors(self):
        status, body = await self.call("/api/cover/test", "POST", {"song_id": "ffffffff"})
        self.assertEqual((status, body["error"]), (404, "SongNotFound"))
        _, uploaded = await self.upload([("a.flac", song_bytes(1))])
        song_id = uploaded["saved"][0]["id"]
        for bad in (99, 1.5, "3", True):
            status, _ = await self.call("/api/cover/test", "POST", {"song_id": song_id, "f0_up_key": bad})
            self.assertEqual(status, 400, str(bad))
        self.post_status, self.post_body = 500, {"error": "SeparationFailed"}
        status, body = await self.call("/api/cover/test", "POST", {"song_id": song_id})
        self.assertEqual((status, body["error"]), (502, "SeparationFailed"))

    async def test_queue_full_is_429(self):
        _, uploaded = await self.upload([(f"s{n}.flac", song_bytes(n)) for n in range(1, 7)])
        ids = [song["id"] for song in uploaded["saved"]]
        gate = asyncio.Event()
        original = self.cover._post

        async def slow(url, payload, timeout):
            await gate.wait()
            return await original(url, payload, timeout)

        self.cover._post = slow
        tasks = [asyncio.create_task(self.call("/api/cover/test", "POST", {"song_id": song_id})) for song_id in ids[:4]]
        for _ in range(100):
            await asyncio.sleep(0.01)
            if (await self.cover.status())["waiting"] == 3:
                break
        status, body = await self.call("/api/cover/test", "POST", {"song_id": ids[4]})
        self.assertEqual((status, body["error"]), (429, "QueueFull"))
        gate.set()
        self.assertTrue(all(status == 200 for status, _ in await asyncio.gather(*tasks)))

    async def test_overview_carries_the_song_request_settings(self):
        status, body = await self.call("/api/cover")
        self.assertEqual(status, 200)
        self.assertEqual(body["song_request"], default_song_request_settings())

    async def test_song_request_settings_are_saved_and_validated(self):
        text_fields = ("limit_text", "accept_texts", "refuse_texts", "fail_texts")
        wanted = {**default_song_request_settings(), "enabled": False, "group_min_affinity": -10, "refuse_percent": 25,
                  "delivery_mode": "proactive", "daily_limit": 8, "limit_owner": True, "limit_text": "满了{次数}首",
                  "accept_texts": ["好，《{歌名}》", "来啦"], "permission_variable": "点歌权限"}
        stored = {key: value for key, value in wanted.items() if key not in text_fields}
        status, body = await self.call("/api/cover/song-request/settings", "POST", wanted)
        self.assertEqual(status, 200)
        # the reply texts live in the texts section now: a body that carries them does not edit them
        self.assertEqual(self.store.config["song_request"], stored)
        self.assertEqual(body["settings"], {**stored, **{key: default_song_request_settings()[key] for key in text_fields}})
        # it survives a reload from disk
        self.assertEqual(ConfigStore(self.root / "data").config["song_request"], stored)
        status, body = await self.call("/api/cover")
        self.assertEqual(body["song_request"], {**stored, **{key: default_song_request_settings()[key] for key in text_fields}})
        # a partial body keeps the defaults for what it leaves out
        status, body = await self.call("/api/cover/song-request/settings", "POST", {"per_source": 3})
        self.assertEqual((status, body["settings"]), (200, {**default_song_request_settings(), "per_source": 3}))

    async def test_bad_song_request_settings_are_400_and_change_nothing(self):
        before = dict(self.store.config["song_request"])
        bad = [{"daily_limit": 101}, {"daily_limit": -1}, {"daily_limit": float("inf")}, {"limit_owner": "yes"},
               {"limit_text": ""}, {"limit_text": "x" * 101}, {"delivery_mode": "active"}, {"delivery_mode": None}, {"refuse_percent": 101}, {"per_source": 0}, {"group_min_affinity": -51}, {"enabled": "yes"},
               {"refuse_percent": float("inf")}, {"owner_min_affinity": float("nan")},
               {"list_timeout_seconds": True}, {"accept_texts": []}, {"fail_texts": ["x" * 101]},
               {"refuse_texts": ["ok"] * 11}, {"accept_texts": "text"}]
        for payload in bad:
            status, _ = await self.call("/api/cover/song-request/settings", "POST", payload)
            self.assertEqual(status, 400, str(payload))
        status, _ = await self.call("/api/cover/song-request/settings", "POST", [1])
        self.assertEqual(status, 400)
        self.assertEqual(self.store.config["song_request"], before)

    async def test_infinity_and_nan_are_400_not_500_in_the_older_settings(self):
        for path, field in (("/api/queue/settings", "max_waiting"), ("/api/schedule/settings", "quiet_minutes"),
                            ("/api/stickers/settings", "prob"), ("/api/affinity/settings", "max_delta")):
            for number in (float("inf"), float("-inf"), float("nan")):
                status, _ = await self.call(path, "POST", {field: number})
                self.assertEqual(status, 400, f"{path} {field} {number}")

    async def test_origin_is_checked_on_every_write(self):
        evil = {"Origin": "http://evil.example"}
        for method, path, payload in (
            ("POST", "/api/cover/settings", {}),
            ("POST", "/api/cover/song-request/settings", {}),
            ("POST", "/api/cover/service/start", None),
            ("POST", "/api/cover/service/stop", None),
            ("POST", "/api/cover/songs/abcd1234", {"title": "x"}),
            ("DELETE", "/api/cover/songs/abcd1234", None),
            ("POST", "/api/cover/test", {"song_id": "abcd1234"}),
            ("DELETE", "/api/cover/covers/abcd1234", None),
        ):
            status, _ = await self.call(path, method, payload, headers=evil)
            self.assertEqual(status, 403, f"{method} {path}")
        status, _ = await self.upload([("a.flac", song_bytes(1))], headers=evil)
        self.assertEqual(status, 403)
        self.assertEqual(self.managers, [])
        self.assertEqual(self.cover.library.list(), [])

    async def test_without_a_cover_service_the_routes_answer_503(self):
        port = _unused_port()
        service = ConsoleService(create_console_app(self.store), port=port)
        await service.start()
        try:
            async with self.client.get(f"http://127.0.0.1:{port}/api/cover") as response:
                self.assertEqual(response.status, 503)
        finally:
            await service.stop()


if __name__ == "__main__":
    unittest.main()
