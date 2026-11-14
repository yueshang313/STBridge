"""P7a: the cover service (worker process, queue, cache, idle stop) with a fake worker."""

import asyncio
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import aiohttp

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import validate_cover_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import (
    MAX_WAITING,
    CoverError,
    CoverService,
    clip_window,
    effective_pitch,
    job_params,
    params_summary,
    stems_dir_for,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import EmotionConfigError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import ServiceStartError

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def song_bytes(n):
    return MP3 + bytes([n]) * 16


class FakeLogger:
    def __init__(self):
        self.infos, self.warnings, self.errors = [], [], []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)


class FakeManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.state = "stopped"
        self.started = 0
        self.stops = 0
        self.terminated = 0
        self.start_error = None

    async def ensure_started(self):
        if self.start_error is not None:
            raise self.start_error
        self.state = "running"
        self.started += 1
        return "running"

    async def stop(self):
        if self.state != "running":
            return {"ok": False, "message": "服务未在运行"}
        self.state = "stopped"
        self.stops += 1
        return {"ok": True, "message": "服务已关闭"}

    async def status(self):
        return {"state": self.state, "message": "", "pid": None, "log_tail": []}

    async def terminate(self):
        self.terminated += 1
        if self.state == "running":
            self.state = "stopped"


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = validate_cover_settings({"rvc_root": str(self.root / "rvc"), "model": "kotoneV2_e150_s15450.pth"})
        self.logger = FakeLogger()
        self.managers = []
        self.posts = []
        self.gate = None
        self.post_error = None
        self.post_status = 200
        self.post_body = None
        self.now = 1000.0
        self.durations = {}

        def factory(**kwargs):
            manager = FakeManager(**kwargs)
            self.managers.append(manager)
            return manager

        async def post(url, payload, timeout):
            self.posts.append((url, payload, timeout))
            if self.gate is not None:
                await self.gate.wait()
            if self.post_error is not None:
                raise self.post_error
            if self.post_status == 200:
                Path(payload["output_path"]).write_bytes(b"RIFFcover")
            body = self.post_body if self.post_body is not None else {
                "ok": True, "sep_ms": 11, "vc_ms": 22, "mix_ms": 33, "total_ms": 66, "duration": 9.5,
            }
            return self.post_status, body

        self.service = CoverService(
            data_dir=self.root / "data",
            get_settings=lambda: self.settings,
            logger=self.logger,
            worker_script=self.root / "worker.py",
            manager_factory=factory,
            post=post,
            probe=lambda rvc_root, path: self.durations.get(path.name, 200.0),
            clock=lambda: self.now,
        )

    async def add_song(self, n=1, duration=200.0):
        song = await self.service.add_song(f"song{n}.mp3", song_bytes(n))
        self.service.library.set_duration(song["id"], duration)
        return self.service.library.get(song["id"])

    def cover_logs(self):
        return [line for line in self.logger.infos if "Cover song=" in line]


class ClipWindowTests(unittest.TestCase):
    SETTINGS = {"clip_seconds": 60, "default_start_percent": 30}

    def test_default_start_is_a_percentage_into_the_song(self):
        self.assertEqual(clip_window({"duration": 200}, self.SETTINGS), (60.0, 60.0))

    def test_the_songs_own_start_wins(self):
        self.assertEqual(clip_window({"duration": 200, "clip_start": 10}, self.SETTINGS), (10.0, 60.0))

    def test_the_clip_stays_inside_the_song(self):
        self.assertEqual(clip_window({"duration": 100, "clip_start": 90}, self.SETTINGS), (40.0, 60.0))
        self.assertEqual(clip_window({"duration": 30}, self.SETTINGS), (0.0, 30.0))
        self.assertEqual(clip_window({"duration": 100, "clip_start": 10}, {"clip_seconds": 200, "default_start_percent": 30}), (0.0, 100.0))

    def test_zero_seconds_means_the_whole_song(self):
        self.assertEqual(clip_window({"duration": 200, "clip_start": 50}, {"clip_seconds": 0, "default_start_percent": 30}), (0.0, 0.0))

    def test_unknown_duration(self):
        self.assertEqual(clip_window({"duration": 0}, self.SETTINGS), (0.0, 60.0))

    def test_job_params_carry_the_new_fields_and_not_the_old_ones(self):
        params = job_params(validate_cover_settings({"vocal_reverb": 40, "dereverb_model": ""}), "i.index", 0.0, 60.0)
        self.assertEqual(
            (params["sep_model"], params["dereverb_model"], params["vocal_reverb"]),
            ("vocals-bs-roformer-368", "", 40),
        )
        for name in ("uvr_model", "filter_radius", "stems_dir", "keep_stems"):
            self.assertNotIn(name, params)

    def test_summary_names_the_separation_and_the_reverb(self):
        base = params_summary(job_params(validate_cover_settings({"vocal_reverb": 0}), "", 0.0, 60.0))
        self.assertIn("去伴奏 + 去混响", base)
        self.assertNotIn("混响 ", base.replace("去混响", ""))
        plain = params_summary(job_params(validate_cover_settings({"dereverb_model": "", "vocal_reverb": 30}), "", 0.0, 60.0))
        self.assertIn("去伴奏 · 混响 30", plain)
        self.assertNotIn("去混响", plain)

    def test_stems_dir_name(self):
        now = datetime(2026, 11, 2, 23, 5, 9)
        path = stems_dir_for(Path("/data"), '春日影: a/b?*"c"', now)
        self.assertEqual(path.parent, Path("/data") / "cover_stems")
        self.assertEqual(path.name, "20261102-230509_春日影_a_b_c")
        self.assertEqual(stems_dir_for(Path("/data"), "", now).name, "20261102-230509_song")
        self.assertLessEqual(len(stems_dir_for(Path("/data"), "x" * 200, now).name), 16 + 40)

    def test_summary(self):
        params = job_params(validate_cover_settings({"model": "kotoneV2_e150_s15450.pth"}), "i.index", 12.5, 60.0)
        text = params_summary(params)
        self.assertIn("kotoneV2", text)
        self.assertIn("12.5s 起 60s", text)
        self.assertIn("人声 +0", text)
        self.assertNotRegex(text, r"· 伴奏 [+-]")
        whole = params_summary(job_params(validate_cover_settings({"clip_seconds": 0}), "", 0.0, 0.0))
        self.assertIn("整首", whole)


class SingTests(ServiceCase):
    async def test_a_new_cover_is_made_and_cached(self):
        song = await self.add_song()
        result = await self.service.sing(song["id"])
        self.assertFalse(result["cached"])
        self.assertEqual(result["timings"], {"sep_ms": 11, "vc_ms": 22, "mix_ms": 33, "total_ms": 66})
        cover = result["cover"]
        self.assertEqual(cover["title"], "song1")
        self.assertEqual(cover["duration"], 9.5)
        self.assertTrue(self.service.cache.path_for(cover["key"]).is_file())
        self.assertFalse(self.service.cache.partial_path(cover["key"]).exists())
        url, payload, timeout = self.posts[0]
        self.assertEqual(url, f"http://127.0.0.1:{self.settings['port']}/cover")
        self.assertEqual(timeout, 600.0)
        self.assertEqual(payload["model"], "kotoneV2_e150_s15450.pth")
        self.assertEqual(payload["clip_start"], 60.0)
        self.assertEqual(payload["clip_seconds"], 60.0)
        self.assertTrue(payload["input_path"].endswith("song1.mp3"))
        self.assertTrue(payload["output_path"].endswith(".part.wav"))

    async def test_the_second_identical_request_is_served_from_the_cache(self):
        song = await self.add_song()
        first = await self.service.sing(song["id"])
        second = await self.service.sing(song["id"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["cover"]["key"], first["cover"]["key"])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.managers[0].started, 1)

    async def test_any_changed_parameter_misses_the_cache(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.settings = {**self.settings, "index_rate": 0.5}
        await self.service.sing(song["id"])
        self.settings = {**self.settings, "sep_model": "vocals-bs-roformer-317"}
        await self.service.sing(song["id"])
        await self.service.sing(song["id"], f0_up_key=2)
        self.service.library.update(song["id"], clip_start=20)
        await self.service.sing(song["id"], f0_up_key=2)
        self.assertEqual(len(self.posts), 5)
        self.assertEqual(len(self.service.cache.list()), 5)

    async def test_vocal_reverb_and_models_change_the_key_but_keep_stems_does_not(self):
        song = await self.add_song()
        first = await self.service.sing(song["id"])
        self.assertNotIn("stems_dir", self.posts[0][1])
        self.settings = {**self.settings, "keep_stems": True}
        again = await self.service.sing(song["id"])
        self.assertTrue(again["cached"])  # same cover, so no new job and no stems
        self.assertEqual(again["cover"]["key"], first["cover"]["key"])
        self.assertEqual(len(self.posts), 1)
        self.settings = {**self.settings, "vocal_reverb": 20}
        changed = await self.service.sing(song["id"])
        self.assertFalse(changed["cached"])
        self.assertNotEqual(changed["cover"]["key"], first["cover"]["key"])
        self.settings = {**self.settings, "dereverb_model": ""}
        await self.service.sing(song["id"])
        self.assertEqual(len(self.posts), 3)

    async def test_stems_dir_is_sent_only_when_keeping_stems(self):
        song = await self.add_song()
        self.settings = {**self.settings, "keep_stems": True}
        await self.service.sing(song["id"])
        payload = self.posts[0][1]
        stems = Path(payload["stems_dir"])
        self.assertEqual(stems.parent, self.root / "data" / "cover_stems")
        self.assertTrue(stems.is_absolute())
        self.assertRegex(stems.name, r"^\d{8}-\d{6}_song1$")
        for name in ("sep_model", "dereverb_model", "vocal_reverb"):
            self.assertIn(name, payload)
        for name in ("uvr_model", "filter_radius", "keep_stems"):
            self.assertNotIn(name, payload)

    async def test_dereverb_timing_is_kept_in_the_cache_entry(self):
        song = await self.add_song()
        self.post_body = {"ok": True, "sep_ms": 11, "dereverb_ms": 7, "vc_ms": 22, "mix_ms": 33, "total_ms": 80, "duration": 9.5}
        result = await self.service.sing(song["id"])
        self.assertEqual(result["cover"]["timings"]["dereverb_ms"], 7)
        self.assertIn("dereverb_ms=7", self.cover_logs()[-1])

    async def test_pitch_override_does_not_change_the_saved_settings(self):
        song = await self.add_song()
        await self.service.sing(song["id"], f0_up_key=5)
        self.assertEqual(self.posts[0][1]["f0_up_key"], 5)
        self.assertEqual(self.settings["f0_up_key"], 0)

    async def test_precondition_failures(self):
        song = await self.add_song()
        cases = [
            ({**self.settings, "enabled": False}, song["id"], "Disabled"),
            ({**self.settings, "model": ""}, song["id"], "NoModel"),
            ({**self.settings, "rvc_root": ""}, song["id"], "RvcRootNotSet"),
            (self.settings, "nothere", "SongNotFound"),
        ]
        for settings, song_id, kind in cases:
            self.settings = settings
            with self.assertRaises(CoverError) as caught:
                await self.service.sing(song_id)
            self.assertEqual(caught.exception.kind, kind)
        self.settings = self.service.library and self.settings
        self.assertEqual(self.posts, [])
        self.service.library.path(song).unlink()
        self.settings = validate_cover_settings({"rvc_root": str(self.root / "rvc"), "model": "kotoneV2_e150_s15450.pth"})
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "SongFileMissing")

    async def test_service_start_failure(self):
        song = await self.add_song()
        self.service.manager().start_error = ServiceStartError("boom C:\\secret")
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "ServiceStartFailed")
        self.assertEqual(self.posts, [])

    async def test_worker_errors_keep_a_short_safe_kind(self):
        song = await self.add_song()
        self.post_status, self.post_body = 500, {"error": "ConversionFailed"}
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "ConversionFailed")
        self.post_body = {"error": "C:\\Users\\secret\\path failed!"}
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertNotIn("\\", caught.exception.kind)
        self.assertNotIn(" ", caught.exception.kind)
        self.post_status, self.post_body = 502, {}
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "HTTP502")
        self.assertEqual(self.service.cache.list(), [])

    async def test_a_worker_that_is_down_is_reported(self):
        song = await self.add_song()
        self.post_error = aiohttp.ClientConnectionError("refused")
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "WorkerUnreachable")

    async def test_timeout_stops_the_worker_and_leaves_no_partial_file(self):
        song = await self.add_song()
        self.settings = {**self.settings, "job_timeout_seconds": 45}
        self.post_error = asyncio.TimeoutError()
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "Timeout")
        self.assertEqual(self.posts[0][2], 45.0)
        self.assertEqual(self.managers[0].stops, 1)
        self.assertEqual(list(self.service.cache.directory.glob("*.part.wav")), [])
        self.assertTrue(any("result=failed:Timeout" in line for line in self.cover_logs()))

    async def test_a_missing_output_file_is_a_failure(self):
        song = await self.add_song()
        self.post_status = 200

        async def post(url, payload, timeout):
            return 200, {"ok": True}

        self.service._post = post
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"])
        self.assertEqual(caught.exception.kind, "OutputMissing")


class QueueTests(ServiceCase):
    async def test_one_runs_three_wait_the_next_is_refused(self):
        songs = [await self.add_song(n) for n in range(1, 6)]
        self.gate = asyncio.Event()
        tasks = [asyncio.create_task(self.service.sing(song["id"])) for song in songs[:1 + MAX_WAITING]]
        for _ in range(50):
            await asyncio.sleep(0)
        self.assertEqual(len(self.posts), 1)  # one at a time
        status = await self.service.status()
        self.assertTrue(status["busy"])
        self.assertEqual(status["waiting"], MAX_WAITING)
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(songs[4]["id"])
        self.assertEqual(caught.exception.kind, "QueueFull")
        self.gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(results), 4)
        self.assertEqual(len(self.posts), 4)
        self.assertEqual((await self.service.status())["waiting"], 0)
        self.assertFalse((await self.service.status())["busy"])

    async def test_a_cache_hit_never_waits_for_the_queue(self):
        songs = [await self.add_song(n) for n in range(1, 6)]
        await self.service.sing(songs[0]["id"])
        self.gate = asyncio.Event()
        tasks = [asyncio.create_task(self.service.sing(song["id"])) for song in songs[1:5]]
        for _ in range(50):
            await asyncio.sleep(0)
        again = await self.service.sing(songs[0]["id"])
        self.assertTrue(again["cached"])
        self.gate.set()
        await asyncio.gather(*tasks)

    async def test_identical_waiting_jobs_run_once(self):
        song = await self.add_song()
        self.gate = asyncio.Event()
        first = asyncio.create_task(self.service.sing(song["id"]))
        second = asyncio.create_task(self.service.sing(song["id"]))
        for _ in range(50):
            await asyncio.sleep(0)
        self.gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(sorted(result["cached"] for result in results), [False, True])
        self.assertEqual(len(self.posts), 1)

    async def test_a_failed_job_frees_its_slot(self):
        song = await self.add_song()
        self.post_error = aiohttp.ClientConnectionError("x")
        for _ in range(MAX_WAITING + 3):
            with self.assertRaises(CoverError) as caught:
                await self.service.sing(song["id"])
            self.assertEqual(caught.exception.kind, "WorkerUnreachable")
        self.assertFalse((await self.service.status())["busy"])


class WorkerProcessTests(ServiceCase):
    async def test_manager_is_built_for_the_rvc_python_and_the_bundled_script(self):
        manager = self.service.manager()
        kwargs = manager.kwargs
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 8099)
        self.assertEqual(kwargs["launch_dir"], self.settings["rvc_root"])
        self.assertEqual(Path(kwargs["python_path"]), Path(self.settings["rvc_root"]) / "runtime" / "python.exe")
        self.assertEqual(kwargs["args"], [str(self.root / "worker.py"), "--host", "127.0.0.1", "--port", "8099"])
        self.assertTrue(kwargs["stop_on_exit"])
        self.assertEqual(kwargs["log_name"], "cover_service.log")
        self.assertEqual(kwargs["service_name"], "RVC")
        self.assertIs(self.service.manager(), manager)

    async def test_changed_launch_settings_stop_the_old_worker(self):
        manager = self.service.manager()
        await self.service.start_service()
        old = self.settings
        self.settings = {**old, "port": 8100}
        self.assertTrue(await self.service.apply_settings(old, self.settings))
        self.assertEqual(manager.stops, 1)
        self.assertIsNot(self.service.manager(), manager)
        self.assertEqual(self.service.manager().kwargs["port"], 8100)

    async def test_other_setting_changes_keep_the_worker(self):
        manager = self.service.manager()
        await self.service.start_service()
        old = self.settings
        self.settings = {**old, "f0_up_key": 3, "model": "x.pth", "idle_stop_minutes": 5}
        self.assertFalse(await self.service.apply_settings(old, self.settings))
        self.assertEqual(manager.stops, 0)
        self.assertIs(self.service.manager(), manager)

    async def test_changed_root_also_stops_it(self):
        await self.service.start_service()
        old = self.settings
        self.settings = {**old, "rvc_root": str(self.root / "other")}
        self.assertTrue(await self.service.apply_settings(old, self.settings))

    async def test_start_stop_status(self):
        self.assertEqual((await self.service.status())["state"], "stopped")
        self.assertEqual(await self.service.start_service(), "running")
        self.assertEqual((await self.service.status())["state"], "running")
        self.assertTrue((await self.service.stop_service())["ok"])
        self.assertFalse((await self.service.stop_service())["ok"])

    async def test_terminate_stops_the_idle_watch_and_the_worker(self):
        await self.service.start_service()
        self.service.start_idle_watch(interval=0.01)
        task = self.service._idle_task
        await self.service.terminate()
        self.assertTrue(task.done())
        self.assertEqual(self.managers[0].terminated, 1)


class IdleStopTests(ServiceCase):
    async def test_idle_worker_is_stopped_after_the_configured_minutes(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.assertEqual(self.managers[0].state, "running")
        self.now += 9 * 60
        self.assertFalse(await self.service.idle_check())
        self.now += 2 * 60
        self.assertTrue(await self.service.idle_check())
        self.assertEqual(self.managers[0].state, "stopped")
        self.assertTrue(any("stopped after 10 minutes idle" in line for line in self.logger.infos))
        self.assertFalse(await self.service.idle_check())  # nothing left to stop

    async def test_a_new_job_restarts_the_worker(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.now += 11 * 60
        await self.service.idle_check()
        self.settings = {**self.settings, "f0_up_key": 1}
        await self.service.sing(song["id"])
        self.assertEqual(self.managers[0].started, 2)

    async def test_zero_disables_the_idle_stop(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.settings = {**self.settings, "idle_stop_minutes": 0}
        self.now += 10_000
        self.assertFalse(await self.service.idle_check())
        self.assertEqual(self.managers[0].state, "running")

    async def test_busy_worker_is_not_stopped(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.gate = asyncio.Event()
        self.settings = {**self.settings, "f0_up_key": 1}
        task = asyncio.create_task(self.service.sing(song["id"]))
        for _ in range(30):
            await asyncio.sleep(0)
        self.now += 10_000
        self.assertFalse(await self.service.idle_check())
        self.gate.set()
        await task

    async def test_the_loop_runs_checks(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.now += 11 * 60
        self.service.start_idle_watch(interval=0.01)
        for _ in range(100):
            if self.managers[0].state == "stopped":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.managers[0].state, "stopped")
        await self.service.terminate()


class SongUploadTests(ServiceCase):
    async def test_the_duration_comes_from_the_probe(self):
        self.durations["song1.mp3"] = 187.5
        song = await self.service.add_song("song1.mp3", song_bytes(1))
        self.assertEqual(song["duration"], 187.5)

    async def test_an_unreadable_file_is_dropped_again(self):
        self.durations["song1.mp3"] = 0.0
        with self.assertRaises(EmotionConfigError):
            await self.service.add_song("song1.mp3", song_bytes(1))
        self.assertEqual(self.service.library.list(), [])
        self.assertEqual(list((self.root / "data" / "songs").glob("*.mp3")), [])

    async def test_format_and_duplicates_are_refused_before_probing(self):
        with self.assertRaises(EmotionConfigError):
            await self.service.add_song("x.mp3", b"not audio")
        await self.service.add_song("a.mp3", song_bytes(1))
        with self.assertRaises(EmotionConfigError):
            await self.service.add_song("b.mp3", song_bytes(1))


class LogTests(ServiceCase):
    async def test_log_line_fields_and_no_paths(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        await self.service.sing(song["id"])
        first, second = self.cover_logs()
        self.assertRegex(
            first,
            r"^\[STBridge\] Cover song=song1 model=kotoneV2 key=[0-9a-f]{8} result=ok sep_ms=11 dereverb_ms=0 vc_ms=22 mix_ms=33 total_ms=66$",
        )
        self.assertRegex(second, r"result=cached sep_ms=0 dereverb_ms=0 vc_ms=0 mix_ms=0 total_ms=\d+$")
        for line in self.logger.infos + self.logger.warnings + self.logger.errors:
            self.assertNotIn(str(self.root), line)
            self.assertNotIn("\\", line)
            self.assertNotIn(".mp3", line)

    async def test_failures_are_logged_with_their_kind(self):
        song = await self.add_song()
        self.post_status, self.post_body = 500, {"error": "SeparationFailed"}
        with self.assertRaises(CoverError):
            await self.service.sing(song["id"])
        self.assertRegex(self.cover_logs()[-1], r"result=failed:SeparationFailed ")

    async def test_long_titles_are_truncated(self):
        song = await self.service.add_song("x" * 100 + ".mp3", song_bytes(9))
        self.service.library.set_duration(song["id"], 100)
        await self.service.sing(song["id"])
        self.assertIn("song=" + "x" * 20 + " ", self.cover_logs()[0])


if __name__ == "__main__":
    unittest.main()


class EffectivePitchTests(unittest.TestCase):
    SETTINGS = {"f0_up_key": 3, "inst_pitch": 2}

    def test_voice_gets_vocal_plus_overall_and_accompaniment_the_overall(self):
        self.assertEqual(effective_pitch(self.SETTINGS), (5, 2))
        self.assertEqual(effective_pitch(self.SETTINGS, 12, 0), (12, 0))  # a preset replaces both values
        self.assertEqual(effective_pitch(self.SETTINGS, None, 4), (7, 4))
        self.assertEqual(effective_pitch(self.SETTINGS, -1, None), (1, 2))
        self.assertEqual(effective_pitch({"f0_up_key": 12, "inst_pitch": 12}), (24, 12))
        self.assertEqual(effective_pitch({"f0_up_key": -12, "inst_pitch": -12}), (-24, -12))
        self.assertEqual(effective_pitch({"f0_up_key": 0, "inst_pitch": 0}, 0, 0), (0, 0))


class PitchSingTests(ServiceCase):
    async def test_the_worker_gets_the_combined_values(self):
        song = await self.add_song()
        self.settings = {**self.settings, "f0_up_key": 3, "inst_pitch": 2}
        await self.service.sing(song["id"])
        self.assertEqual((self.posts[0][1]["f0_up_key"], self.posts[0][1]["inst_pitch"]), (5, 2))
        await self.service.sing(song["id"], f0_up_key=12, inst_pitch=0)
        self.assertEqual((self.posts[1][1]["f0_up_key"], self.posts[1][1]["inst_pitch"]), (12, 0))
        # the saved settings are untouched
        self.assertEqual((self.settings["f0_up_key"], self.settings["inst_pitch"]), (3, 2))

    async def test_inst_pitch_is_part_of_the_cache_key_and_the_summary(self):
        song = await self.add_song()
        first = await self.service.sing(song["id"])
        again = await self.service.sing(song["id"], inst_pitch=0)
        self.assertTrue(again["cached"])
        shifted = await self.service.sing(song["id"], inst_pitch=2)
        self.assertFalse(shifted["cached"])
        self.assertNotEqual(first["cover"]["key"], shifted["cover"]["key"])
        self.assertIn("人声 +2 · 伴奏 +2", shifted["cover"]["summary"])
        self.assertNotRegex(first["cover"]["summary"], r"· 伴奏 [+-]")
        # same voice shift reached two ways, but a different accompaniment: different covers
        a = await self.service.sing(song["id"], f0_up_key=0, inst_pitch=2)
        b = await self.service.sing(song["id"], f0_up_key=2, inst_pitch=0)
        self.assertNotEqual(a["cover"]["key"], b["cover"]["key"])

    async def test_the_pitch_error_passes_through(self):
        song = await self.add_song()
        self.post_status = 500
        self.post_body = {"error": "PitchShiftUnavailable"}
        with self.assertRaises(CoverError) as caught:
            await self.service.sing(song["id"], inst_pitch=2)
        self.assertEqual(caught.exception.kind, "PitchShiftUnavailable")


class JobParamsPitchTests(unittest.TestCase):
    def test_inst_pitch_is_a_job_param(self):
        params = job_params(validate_cover_settings({"inst_pitch": 4}), "", 0.0, 60.0)
        self.assertEqual(params["inst_pitch"], 4)
        self.assertNotIn("pitch_presets", params)
