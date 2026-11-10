"""C1: storage cleanup (temp-file ageing, cover temp dirs, daily loop, cover-cache clearing, routes)."""

import asyncio
import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import storage_cleanup
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import cover_settings, validate_cover_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import CoverService
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.storage_cleanup import (
    AUTO_INTERVAL_SECONDS,
    CleanupService,
    clean_cover_temp,
    clean_dir,
    cleanup_settings,
    cutoff_time,
    default_cleanup_settings,
    measure_dir,
    validate_cleanup_settings,
)
from test_cover_service import FakeLogger, FakeManager

NOW = 2_000_000_000.0
HOUR = 3600.0
DAY = 86400.0


def make_file(path: Path, size: int = 10, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - age, NOW - age))
    return path


def make_dir(path: Path, age: float) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.utime(path, (NOW - age, NOW - age))
    return path


class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(default_cleanup_settings(), {"enabled": True, "keep_days": 3})
        self.assertEqual(validate_cleanup_settings({}), {"enabled": True, "keep_days": 3})
        self.assertEqual(validate_cleanup_settings({"enabled": False, "keep_days": 365.0}), {"enabled": False, "keep_days": 365})
        for bad in ({"keep_days": 0}, {"keep_days": 366}, {"keep_days": 1.5}, {"keep_days": "3"}, {"keep_days": True},
                    {"keep_days": float("inf")}, {"keep_days": float("-inf")}, {"keep_days": float("nan")},
                    {"enabled": 1}, {"enabled": "yes"}):
            with self.assertRaises(EmotionConfigError, msg=str(bad)):
                validate_cleanup_settings(bad)
        with self.assertRaises(EmotionConfigError):
            validate_cleanup_settings([])

    def test_cleanup_settings_falls_back_to_defaults(self):
        self.assertEqual(cleanup_settings({}), default_cleanup_settings())
        self.assertEqual(cleanup_settings({"cleanup": {"keep_days": 0}}), default_cleanup_settings())
        self.assertEqual(cleanup_settings({"cleanup": {"keep_days": 7}})["keep_days"], 7)

    def test_old_config_without_the_section_gets_defaults_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp))
            config = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["cleanup"], {"enabled": True, "keep_days": 3})
            del config["cleanup"]
            (Path(tmp) / "config.json").write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(ConfigStore(Path(tmp)).config["cleanup"], {"enabled": True, "keep_days": 3})

    def test_cutoff_never_goes_inside_the_last_hour(self):
        self.assertEqual(cutoff_time(NOW, 3), NOW - 3 * DAY)
        self.assertEqual(cutoff_time(NOW, 0), NOW - HOUR)


class CleanDirTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_age_threshold(self):
        old = make_file(self.root / "media_audio_old.silk", 100, age=4 * DAY)
        edge_new = make_file(self.root / "media_image_new.png", 50, age=2 * DAY)
        fresh = make_file(self.root / "media_audio_fresh.silk", 10, age=60)
        self.assertEqual(clean_dir(self.root, cutoff_time(NOW, 3)), (1, 100, 0))
        self.assertFalse(old.exists())
        self.assertTrue(edge_new.exists() and fresh.exists())

    def test_one_hour_protection_even_with_a_tiny_keep_time(self):
        young = make_file(self.root / "a.silk", age=30 * 60)
        older = make_file(self.root / "b.silk", age=2 * HOUR)
        self.assertEqual(clean_dir(self.root, cutoff_time(NOW, 0))[0], 1)
        self.assertTrue(young.exists())
        self.assertFalse(older.exists())

    def test_files_in_subdirectories_go_but_no_directory_is_removed(self):
        nested = make_file(self.root / "tool_images" / "deep" / "x.png", 7, age=10 * DAY)
        keep = make_file(self.root / "tool_images" / "y.png", 7, age=1 * DAY)
        self.assertEqual(clean_dir(self.root, cutoff_time(NOW, 3)), (1, 7, 0))
        self.assertFalse(nested.exists())
        self.assertTrue(keep.exists())
        self.assertTrue((self.root / "tool_images" / "deep").is_dir())  # now empty, still there
        make_dir(self.root / "empty_old", age=30 * DAY)
        clean_dir(self.root, cutoff_time(NOW, 3))
        self.assertTrue((self.root / "empty_old").is_dir())

    def test_a_failing_delete_is_counted_and_skipped(self):
        locked = make_file(self.root / "locked.silk", 5, age=9 * DAY)
        free = make_file(self.root / "free.silk", 6, age=9 * DAY)
        real_remove = os.remove

        def remove(path):
            if str(path).endswith("locked.silk"):
                raise PermissionError("in use")
            real_remove(path)

        with mock.patch.object(storage_cleanup.os, "remove", remove):
            self.assertEqual(clean_dir(self.root, cutoff_time(NOW, 3)), (1, 6, 1))
        self.assertTrue(locked.exists())
        self.assertFalse(free.exists())

    def test_missing_path_counts_as_zero(self):
        missing = self.root / "nope"
        self.assertEqual(measure_dir(missing), (0, 0))
        self.assertEqual(clean_dir(missing, NOW), (0, 0, 0))
        a_file = make_file(self.root / "plain.txt", age=9 * DAY)
        self.assertEqual(clean_dir(a_file, NOW), (0, 0, 0))  # a file is not a directory to sweep
        self.assertTrue(a_file.exists())

    def test_measure(self):
        make_file(self.root / "a", 10)
        make_file(self.root / "sub" / "b", 32)
        self.assertEqual(measure_dir(self.root), (2, 42))


class CoverTempTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)

    def test_only_old_prefixed_directories_are_removed(self):
        old = make_dir(self.tmp / "stbridge_cover_abc123", age=3 * HOUR)
        make_file(old / "vocal.wav", 100, age=3 * HOUR)
        make_file(old / "sub" / "inst.wav", 50, age=3 * HOUR)
        os.utime(old, (NOW - 3 * HOUR, NOW - 3 * HOUR))
        young = make_dir(self.tmp / "stbridge_cover_young", age=10 * 60)
        make_file(young / "a.wav", 5, age=10 * 60)
        os.utime(young, (NOW - 600, NOW - 600))
        other = make_dir(self.tmp / "gradio", age=90 * DAY)
        make_file(other / "keep.png", 5, age=90 * DAY)
        similar = make_dir(self.tmp / "my_stbridge_cover_x", age=90 * DAY)
        plain_file = make_file(self.tmp / "stbridge_cover_file.txt", 5, age=90 * DAY)
        nested = make_dir(self.tmp / "gradio" / "stbridge_cover_nested", age=90 * DAY)
        self.assertEqual(clean_cover_temp(self.tmp, NOW), (2, 150, 0))
        self.assertFalse(old.exists())
        for survivor in (young, other, similar, plain_file, nested, other / "keep.png"):
            self.assertTrue(survivor.exists(), survivor)

    def test_rmtree_failure_is_counted(self):
        make_dir(self.tmp / "stbridge_cover_locked", age=3 * HOUR)
        with mock.patch.object(storage_cleanup.shutil, "rmtree", side_effect=PermissionError("busy")):
            self.assertEqual(clean_cover_temp(self.tmp, NOW), (0, 0, 1))
        self.assertTrue((self.tmp / "stbridge_cover_locked").exists())

    def test_missing_temp_root(self):
        self.assertEqual(clean_cover_temp(self.tmp / "nope", NOW), (0, 0, 0))


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.astrbot = self.root / "astrbot_temp"
        self.bridge = self.data / "temp_audio"
        self.system = self.root / "system_temp"
        self.stems = self.root / "cover_stems"
        for path in (self.astrbot, self.bridge, self.system, self.stems):
            path.mkdir()
        self.now = NOW
        self.settings = {"enabled": True, "keep_days": 3}
        self.logger = FakeLogger()
        self.service = CleanupService(
            data_dir=self.data,
            get_settings=lambda: self.settings,
            logger=self.logger,
            astrbot_temp=lambda: self.astrbot,
            bridge_temp=lambda: self.bridge,
            cover_stems=lambda: self.stems,
            system_temp=lambda: self.system,
            clock=lambda: self.now,
        )

    def fill(self):
        make_file(self.astrbot / "media_audio_1.silk", 100, age=5 * DAY)
        make_file(self.astrbot / "tool_images" / "t.png", 20, age=5 * DAY)
        make_file(self.astrbot / "media_image_new.png", 30, age=DAY)
        make_file(self.bridge / "a.wav", 40, age=4 * DAY)
        cover = make_dir(self.system / "stbridge_cover_x", age=2 * HOUR)
        make_file(cover / "v.wav", 60, age=2 * HOUR)
        os.utime(cover, (NOW - 2 * HOUR, NOW - 2 * HOUR))


class RunTests(ServiceCase):
    async def test_stats_per_target(self):
        self.fill()
        stats = await asyncio.to_thread(self.service.stats)
        self.assertEqual(stats, {
            "astrbot_temp": {"files": 3, "bytes": 150},
            "bridge_temp": {"files": 1, "bytes": 40},
            "cover_temp": {"files": 1, "bytes": 60},
            "cover_stems": {"files": 0, "bytes": 0},
        })

    async def test_missing_directories_are_zero(self):
        service = CleanupService(
            data_dir=self.data, get_settings=lambda: self.settings, logger=self.logger,
            astrbot_temp=lambda: None, bridge_temp=lambda: self.root / "nope",
            system_temp=lambda: self.root / "also_nope", clock=lambda: NOW,
        )
        self.assertEqual((await service.overview())["stats"]["astrbot_temp"], {"files": 0, "bytes": 0})
        result = await service.run("manual")
        self.assertEqual((result["removed"], result["bytes"], result["skipped"]), (0, 0, 0))

    async def test_manual_run_cleans_everything_due_and_logs(self):
        self.fill()
        result = await self.service.run("manual")
        self.assertEqual((result["removed"], result["bytes"], result["skipped"]), (4, 220, 0))
        self.assertEqual(result["targets"]["astrbot_temp"], {"removed": 2, "bytes": 120, "skipped": 0})
        self.assertTrue((self.astrbot / "media_image_new.png").exists())  # a day old, keep time is 3 days
        self.assertFalse((self.system / "stbridge_cover_x").exists())
        self.assertEqual(self.logger.infos[-1], "[STBridge] Cleanup manual removed=4 bytes=220 skipped=0")
        self.assertEqual(self.service.last_auto(), {})  # manual runs are not remembered

    async def test_keep_days_setting_is_used(self):
        self.fill()
        self.settings = {"enabled": True, "keep_days": 1}
        await self.service.run("manual")
        self.assertEqual(sorted(p.name for p in self.astrbot.iterdir()), ["media_image_new.png", "tool_images"])

    async def test_one_hour_rule_in_a_run(self):
        make_file(self.astrbot / "fresh.silk", age=10 * 60)
        self.settings = {"enabled": True, "keep_days": 0}  # only reachable by hand-edited settings
        await self.service.run("manual")
        self.assertTrue((self.astrbot / "fresh.silk").exists())


class CoverStemsTests(ServiceCase):
    def fill_stems(self):
        old = make_dir(self.stems / "20261101-100000_old", age=5 * DAY)
        make_file(old / "vocals.wav", 70, age=5 * DAY)
        make_file(old / "dry.wav", 30, age=5 * DAY)
        mixed = make_dir(self.stems / "20261102-100000_mixed", age=5 * DAY)
        make_file(mixed / "clip.wav", 10, age=5 * DAY)
        make_file(mixed / "converted.wav", 20, age=DAY)  # still inside the keep time
        fresh = make_dir(self.stems / "20261102-110000_fresh", age=DAY)
        make_file(fresh / "clip.wav", 5, age=DAY)

    async def test_stats_count_the_stems(self):
        self.fill_stems()
        stats = await asyncio.to_thread(self.service.stats)
        self.assertEqual(stats["cover_stems"], {"files": 5, "bytes": 135})

    async def test_old_stem_files_and_emptied_folders_are_removed(self):
        self.fill_stems()
        result = await self.service.run("manual")
        self.assertEqual(result["targets"]["cover_stems"], {"removed": 3, "bytes": 110, "skipped": 0})
        self.assertEqual(sorted(p.name for p in self.stems.iterdir()), ["20261102-100000_mixed", "20261102-110000_fresh"])
        self.assertEqual([p.name for p in (self.stems / "20261102-100000_mixed").iterdir()], ["converted.wav"])
        self.assertTrue(self.stems.is_dir())  # the root stays

    async def test_the_daily_auto_clean_includes_the_stems(self):
        self.fill_stems()
        self.assertTrue(await self.service.check_once())
        self.assertFalse((self.stems / "20261101-100000_old").exists())
        self.assertEqual(self.service.last_auto()["removed"], 3)

    async def test_a_missing_stems_directory_is_zero(self):
        shutil.rmtree(self.stems)
        stats = await asyncio.to_thread(self.service.stats)
        self.assertEqual(stats["cover_stems"], {"files": 0, "bytes": 0})
        result = await self.service.run("manual")
        self.assertEqual(result["targets"]["cover_stems"], {"removed": 0, "bytes": 0, "skipped": 0})


class AutoTests(ServiceCase):
    async def test_due_without_state_then_not_for_24_hours(self):
        self.assertTrue(self.service.auto_due())
        self.fill()
        self.assertTrue(await self.service.check_once())
        last = self.service.last_auto()
        self.assertEqual((last["time"], last["removed"], last["bytes"], last["skipped"]), (NOW, 4, 220, 0))
        self.assertEqual(self.logger.infos[-1], "[STBridge] Cleanup auto removed=4 bytes=220 skipped=0")
        self.assertFalse(await self.service.check_once())
        self.now = NOW + AUTO_INTERVAL_SECONDS - 1
        self.assertFalse(self.service.auto_due())
        self.now = NOW + AUTO_INTERVAL_SECONDS
        self.assertTrue(self.service.auto_due())

    async def test_state_survives_a_restart(self):
        await self.service.check_once()
        again = CleanupService(
            data_dir=self.data, get_settings=lambda: self.settings, logger=self.logger,
            astrbot_temp=lambda: self.astrbot, bridge_temp=lambda: self.bridge, cover_stems=lambda: self.stems,
            system_temp=lambda: self.system, clock=lambda: NOW + 3600,
        )
        self.assertFalse(again.auto_due())
        self.assertEqual(again.last_auto()["time"], NOW)
        self.assertTrue((self.data / "cleanup_state.json").is_file())

    async def test_disabled_never_runs_and_a_future_timestamp_does_not_block(self):
        self.settings = {"enabled": False, "keep_days": 3}
        self.assertFalse(await self.service.check_once())
        self.assertEqual(self.service.last_auto(), {})
        self.settings = {"enabled": True, "keep_days": 3}
        await self.service.check_once()
        self.now = NOW - 10 * DAY  # clock moved back
        self.assertTrue(self.service.auto_due())

    async def test_corrupt_state_means_due(self):
        (self.data / "cleanup_state.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.service.last_auto(), {})
        self.assertTrue(self.service.auto_due())
        (self.data / "cleanup_state.json").write_text(json.dumps({"last_auto": {"time": "x"}}), encoding="utf-8")
        self.assertTrue(self.service.auto_due())

    async def test_background_loop_runs_and_stop_cancels_it(self):
        self.service.start(interval=0.01)
        for _ in range(100):
            if self.service.last_auto():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.service.last_auto())
        task = self.service._task
        await self.service.stop()
        self.assertTrue(task.cancelled() or task.done())
        self.assertIsNone(self.service._task)
        await self.service.stop()  # idempotent

    async def test_loop_survives_an_error(self):
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("boom")

        self.service.clean = broken
        self.service.start(interval=0.01)
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        await self.service.stop()
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("[STBridge] Cleanup auto failed (RuntimeError)", self.logger.errors)
        self.assertEqual(self.service.last_auto(), {})  # a failed run is not "successful"


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConsoleCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = ConfigStore(self.root / "data")
        self.store.config["cover"]["rvc_root"] = str(self.root / "rvc")  # no default folder any more
        self.astrbot = self.root / "astrbot_temp"
        self.astrbot.mkdir()
        (self.root / "system").mkdir()
        self.now = NOW
        self.cleanup = CleanupService(
            data_dir=self.store.data_dir,
            get_settings=lambda: cleanup_settings(self.store.config),
            logger=FakeLogger(),
            astrbot_temp=lambda: self.astrbot,
            bridge_temp=lambda: self.store.data_dir / "temp_audio",
            cover_stems=lambda: self.store.data_dir / "cover_stems",
            system_temp=lambda: self.root / "system",
            clock=lambda: self.now,
        )
        self.cover = CoverService(
            data_dir=self.store.data_dir,
            get_settings=lambda: cover_settings(self.store.config),
            logger=FakeLogger(),
            worker_script=self.root / "w.py",
            manager_factory=lambda **kwargs: FakeManager(**kwargs),
            probe=lambda rvc_root, path: 100.0,
        )
        self.port = _unused_port()
        self.service = ConsoleService(create_console_app(self.store, cover=self.cover, cleanup=self.cleanup), port=self.port)
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

    def add_cover(self, key, size):
        path = self.cover.cache.path_for(key)
        self.cover.cache.directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"w" * size)
        self.cover.cache.add(key, song_id="s", title="t", summary="", duration=1.0, timings={})

    async def test_overview(self):
        make_file(self.astrbot / "a.silk", 11, age=5 * DAY)
        self.add_cover("abc", 300)
        status, body = await self.call("/api/cleanup")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], {"enabled": True, "keep_days": 3})
        self.assertEqual(body["stats"]["astrbot_temp"], {"files": 1, "bytes": 11})
        self.assertEqual(body["cover_cache"], {"entries": 1, "bytes": 300})
        self.assertEqual(body["last_auto"], {})

    async def test_run_deletes_and_reports(self):
        make_file(self.astrbot / "a.silk", 11, age=5 * DAY)
        make_file(self.astrbot / "b.silk", 11, age=DAY)
        status, body = await self.call("/api/cleanup/run", "POST", {})
        self.assertEqual((status, body["removed"], body["bytes"], body["skipped"]), (200, 1, 11, 0))
        self.assertEqual([p.name for p in self.astrbot.iterdir()], ["b.silk"])

    async def test_settings_are_validated_and_saved(self):
        status, body = await self.call("/api/cleanup/settings", "POST", {"enabled": False, "keep_days": 7})
        self.assertEqual((status, body["settings"]), (200, {"enabled": False, "keep_days": 7}))
        self.assertEqual(self.store.config["cleanup"], {"enabled": False, "keep_days": 7})
        for bad in ({"enabled": True, "keep_days": 0}, {"enabled": True, "keep_days": 400}, {"enabled": "x", "keep_days": 3}):
            status, _ = await self.call("/api/cleanup/settings", "POST", bad)
            self.assertEqual(status, 400, str(bad))
        self.assertEqual(self.store.config["cleanup"]["keep_days"], 7)

    async def test_non_finite_numbers_in_the_body_are_a_clean_400(self):
        # json.loads accepts the non-standard Infinity / NaN literals
        for literal in ("Infinity", "-Infinity", "NaN"):
            async with self.client.post(
                f"http://127.0.0.1:{self.port}/api/cleanup/settings",
                data=f'{{"enabled": true, "keep_days": {literal}}}',
                headers={"Content-Type": "application/json"},
            ) as response:
                self.assertEqual(response.status, 400, literal)
        self.assertEqual(self.store.config["cleanup"]["keep_days"], 3)
        with self.assertRaises(EmotionConfigError):
            validate_cover_settings({"port": float("inf")})
        with self.assertRaises(EmotionConfigError):
            validate_cover_settings({"clip_seconds": float("nan")})

    async def test_clear_covers_removes_finished_covers_only(self):
        self.add_cover("k1", 10)
        self.add_cover("k2", 20)
        partial = self.cover.cache.partial_path("k3")
        partial.write_bytes(b"generating")
        songs = self.store.data_dir / "songs"
        song = await self.cover.add_song("s.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64)
        status, body = await self.call("/api/cleanup/covers/clear", "POST", {})
        self.assertEqual((status, body), (200, {"removed": 2, "bytes": 30}))
        self.assertEqual(self.cover.cache.list(), [])
        self.assertEqual(json.loads((self.cover.cache.directory / "covers.json").read_text(encoding="utf-8")), {"covers": {}})
        self.assertTrue(partial.exists())
        self.assertIsNotNone(self.cover.library.get(song["id"]))
        self.assertTrue(self.cover.library.path(song).is_file())
        self.assertTrue((songs / "songs.json").is_file())

    async def test_clear_covers_is_refused_while_a_job_runs(self):
        self.add_cover("k1", 10)
        self.cover._queued = 1
        status, body = await self.call("/api/cleanup/covers/clear", "POST", {})
        self.assertEqual((status, body), (409, {"error": "翻唱进行中"}))
        self.assertEqual(len(self.cover.cache.list()), 1)
        self.cover._queued = 0
        status, _ = await self.call("/api/cleanup/covers/clear", "POST", {})
        self.assertEqual(status, 200)

    async def test_foreign_origin_is_rejected(self):
        evil = {"Origin": "http://evil.example"}
        for path in ("/api/cleanup/run", "/api/cleanup/settings", "/api/cleanup/covers/clear"):
            status, _ = await self.call(path, "POST", {"enabled": True, "keep_days": 3}, headers=evil)
            self.assertEqual(status, 403, path)


class UnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_answer_503_without_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "data")
            port = _unused_port()
            service = ConsoleService(create_console_app(store), port=port)
            await service.start()
            try:
                async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                    async with client.get(f"http://127.0.0.1:{port}/api/cleanup") as response:
                        self.assertEqual(response.status, 503)
                    async with client.post(f"http://127.0.0.1:{port}/api/cleanup/covers/clear", json={}) as response:
                        self.assertEqual(response.status, 503)
            finally:
                await service.stop()


if __name__ == "__main__":
    unittest.main()
