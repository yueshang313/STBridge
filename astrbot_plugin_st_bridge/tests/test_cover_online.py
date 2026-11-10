"""P7b: online songs in the cover service (find_or_fetch, download, search) and the console routes/config."""

import json
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import (
    cover_settings,
    default_cover_settings,
    public_cover_settings,
    validate_cover_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_library import SONG_MAX_BYTES
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import CoverError, CoverService
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.music_source import MusicError
from test_cover_service import FakeLogger, FakeManager

KEY = "SECRETKEY-0123456789-abcdef"
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def audio(n):
    return MP3 + bytes([n]) * 16


def hit(source, song_id, name, artist="歌手", album=""):
    return {"source": source, "id": song_id, "name": name, "artist": artist, "album": album}


class FakeMusic:
    """Programmable stand-in for MusicClient: results/links/files per source, every call recorded."""

    def __init__(self):
        self.results = {}   # source -> list | Exception
        self.links = {}     # (source, id) -> url | Exception
        self.files = {}     # url -> bytes | Exception
        self.calls = []

    async def search(self, name, source, limit=10):
        self.calls.append(("search", source, name))
        outcome = self.results.get(source, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_url(self, song_id, source):
        self.calls.append(("url", source, song_id))
        outcome = self.links.get((source, song_id), MusicError("no_link"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def download(self, url, *, max_bytes, timeout):
        self.calls.append(("download", url, timeout))
        outcome = self.files.get(url, MusicError("HTTP404"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def downloads(self):
        return [call for call in self.calls if call[0] == "download"]

    def searched_sources(self):
        return [call[1] for call in self.calls if call[0] == "search"]

    def offer(self, source, song_id, name, data, artist="歌手"):
        self.results.setdefault(source, []).append(hit(source, song_id, name, artist))
        self.links[(source, song_id)] = f"http://cdn.test/{source}/{song_id}"
        self.files[f"http://cdn.test/{source}/{song_id}"] = data


class OnlineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = validate_cover_settings({"rvc_root": str(self.root / "rvc"), "music_api_key": KEY})
        self.logger = FakeLogger()
        self.music = FakeMusic()
        self.durations = {}  # last byte of the file -> seconds

        def probe(rvc_root, path):
            return self.durations.get(path.read_bytes()[-1], 200.0)

        self.service = CoverService(
            data_dir=self.root / "data",
            get_settings=lambda: self.settings,
            logger=self.logger,
            worker_script=self.root / "worker.py",
            manager_factory=lambda **kwargs: FakeManager(**kwargs),
            probe=probe,
            music=self.music,
        )

    def song_files(self):
        directory = self.service.library.directory
        return sorted(p.name for p in directory.iterdir() if p.name != "songs.json") if directory.exists() else []

    def all_logs(self):
        return "\n".join(self.logger.infos + self.logger.warnings + self.logger.errors)


class FindOrFetchTests(OnlineCase):
    async def test_library_hit_by_normalized_title_makes_no_request(self):
        song = await self.service.add_song("春日影 (Live).mp3", audio(1))
        found = await self.service.find_or_fetch("  春日影！ ")
        self.assertEqual((found["source"], found["downloaded"], found["song"]["id"]), ("library", False, song["id"]))
        self.assertEqual(self.music.calls, [])

    async def test_library_hit_works_without_a_key_and_respects_artist(self):
        self.settings = validate_cover_settings({"rvc_root": "x", "music_api_key": ""})
        await self.service.add_song("晴天.mp3", audio(1))  # manual upload: no artist known
        self.assertEqual((await self.service.find_or_fetch("晴天", "周杰伦"))["source"], "library")
        self.service.library.add("别的.mp3", audio(2), {"title": "晴天", "artist": "某人"})
        song = await self.service.find_or_fetch("晴天", "某人")
        self.assertEqual(song["song"]["artist"], "某人")

    async def test_first_successful_source_wins_and_the_rest_are_not_tried(self):
        self.music.offer("wy", "11", "春日影", audio(3), "MyGO!!!!!")
        self.music.offer("kg", "22", "春日影", audio(4))
        found = await self.service.find_or_fetch("春日影")
        self.assertEqual((found["source"], found["downloaded"]), ("wy", True))
        self.assertEqual(self.music.searched_sources(), ["wy"])
        song = self.service.library.get(found["song"]["id"])
        self.assertEqual((song["title"], song["artist"], song["source"], song["source_id"]), ("春日影", "MyGO!!!!!", "wy", "11"))
        self.assertEqual(len(self.song_files()), 1)
        # the next request is a library hit
        again = await self.service.find_or_fetch("春日影")
        self.assertEqual((again["source"], len(self.music.downloads())), ("library", 1))

    async def test_a_failing_source_only_logs_and_the_next_one_is_tried(self):
        self.music.results["wy"] = MusicError(f"network:ClientError key={KEY}")
        self.music.results["kg"] = []  # no hits
        self.music.offer("qq", "33", "孤勇者", audio(5))
        found = await self.service.find_or_fetch("孤勇者")
        self.assertEqual(found["source"], "qq")
        self.assertEqual(self.music.searched_sources(), ["wy", "kg", "qq"])
        self.assertNotIn(KEY, self.all_logs())
        self.assertIn("source=wy", self.all_logs())

    async def test_source_order_follows_the_setting(self):
        self.settings = validate_cover_settings({"rvc_root": "x", "music_api_key": KEY, "sources": ["qq", "wy"]})
        self.music.offer("wy", "1", "歌", audio(6))
        self.music.offer("qq", "2", "歌", audio(7))
        self.assertEqual((await self.service.find_or_fetch("歌"))["source"], "qq")

    async def test_preview_clip_is_deleted_and_the_next_source_is_used(self):
        self.music.offer("wy", "1", "海阔天空", audio(8))
        self.music.offer("kg", "2", "海阔天空", audio(9))
        self.durations = {8: 30.0, 9: 240.0}
        found = await self.service.find_or_fetch("海阔天空")
        self.assertEqual(found["source"], "kg")
        self.assertEqual(len(self.service.library.list()), 1)
        self.assertEqual(len(self.song_files()), 1)
        self.assertEqual(self.music.searched_sources(), ["wy", "kg"])

    async def test_all_sources_failing_raises_not_found_and_leaves_no_files(self):
        self.music.offer("wy", "1", "歌", audio(10))
        self.durations = {10: 31.0}  # preview only
        self.music.results["kg"] = MusicError("api_failed")
        self.music.offer("qq", "3", "歌", b"not audio at all")
        with self.assertRaises(CoverError) as caught:
            await self.service.find_or_fetch("歌")
        self.assertEqual(caught.exception.kind, "not_found")
        self.assertEqual((self.song_files(), self.service.library.list()), ([], []))

    async def test_download_failures_and_oversize_leave_nothing_behind(self):
        self.music.offer("wy", "1", "歌", audio(11))
        self.music.files["http://cdn.test/wy/1"] = MusicError("too_large")
        self.music.results["kg"] = [hit("kg", "2", "歌")]
        self.music.links[("kg", "2")] = MusicError("no_link")
        with self.assertRaises(CoverError):
            await self.service.find_or_fetch("歌")
        self.assertEqual(self.song_files(), [])

    async def test_unreadable_audio_is_skipped(self):
        self.music.offer("wy", "1", "歌", audio(12))
        self.durations = {12: 0.0}  # probe cannot read it
        self.music.offer("kg", "2", "歌", audio(13))
        self.assertEqual((await self.service.find_or_fetch("歌"))["source"], "kg")
        self.assertEqual(len(self.song_files()), 1)

    async def test_artist_without_exact_match_fails_that_source(self):
        self.music.results["wy"] = [hit("wy", "1", "晴天", "别人")]
        self.music.links[("wy", "1")] = "http://cdn.test/wy/1"
        self.music.files["http://cdn.test/wy/1"] = audio(14)
        self.music.offer("kg", "2", "晴天", audio(15), "周杰伦")
        found = await self.service.find_or_fetch("晴天", "周杰伦")
        self.assertEqual(found["source"], "kg")
        self.assertEqual(self.music.downloads(), [("download", "http://cdn.test/kg/2", 120.0)])

    async def test_without_artist_the_first_result_is_the_fallback(self):
        self.music.results["wy"] = [hit("wy", "1", "完全不同的名字")]
        self.music.links[("wy", "1")] = "http://cdn.test/wy/1"
        self.music.files["http://cdn.test/wy/1"] = audio(16)
        self.assertEqual((await self.service.find_or_fetch("春日影"))["source"], "wy")

    async def test_duplicate_file_returns_the_existing_song(self):
        existing = await self.service.add_song("别名.mp3", audio(17))
        self.music.offer("wy", "1", "新名字", audio(17))  # byte-identical
        found = await self.service.find_or_fetch("新名字")
        self.assertEqual(found["song"]["id"], existing["id"])
        self.assertEqual(len(self.service.library.list()), 1)

    async def test_missing_key_after_a_library_miss(self):
        self.settings = validate_cover_settings({"rvc_root": "x", "music_api_key": ""})
        with self.assertRaises(CoverError) as caught:
            await self.service.find_or_fetch("没有的歌")
        self.assertEqual(caught.exception.kind, "MusicKeyNotSet")
        self.assertEqual(self.music.calls, [])

    async def test_blank_title_is_not_found(self):
        with self.assertRaises(CoverError):
            await self.service.find_or_fetch("  ！！ ")

    async def test_concurrent_requests_for_one_song_download_once(self):
        import asyncio

        self.music.offer("wy", "1", "同一首", audio(18))
        first, second = await asyncio.gather(self.service.find_or_fetch("同一首"), self.service.find_or_fetch("同一首"))
        self.assertEqual(first["song"]["id"], second["song"]["id"])
        self.assertEqual(len(self.music.downloads()), 1)

    async def test_download_timeout_comes_from_settings(self):
        self.settings = validate_cover_settings({"rvc_root": "x", "music_api_key": KEY, "download_timeout_seconds": 45})
        self.music.offer("wy", "1", "歌", audio(19))
        await self.service.find_or_fetch("歌")
        self.assertEqual(self.music.downloads()[0][2], 45.0)


class DownloadAndSearchTests(OnlineCase):
    async def test_download_online_stores_with_metadata(self):
        self.music.offer("kg", "9", "孤勇者", audio(20), "陈奕迅")
        song = await self.service.download_online("kg", "9", "孤勇者", "陈奕迅")
        self.assertEqual((song["title"], song["artist"], song["source"]), ("孤勇者", "陈奕迅", "kg"))
        self.assertGreater(song["duration"], 0)

    async def test_download_online_rejects_a_preview_and_keeps_nothing(self):
        self.music.offer("wy", "9", "孤勇者", audio(21))
        self.durations = {21: 29.0}
        with self.assertRaises(CoverError) as caught:
            await self.service.download_online("wy", "9", "孤勇者")
        self.assertEqual(caught.exception.kind, "preview_only")
        self.assertEqual(self.song_files(), [])

    async def test_download_online_duplicate_returns_the_existing_song(self):
        existing = await self.service.add_song("x.mp3", audio(22))
        self.music.offer("wy", "9", "别的名", audio(22))
        song = await self.service.download_online("wy", "9", "别的名")
        self.assertEqual(song["id"], existing["id"])

    async def test_download_online_needs_a_key_and_a_known_source(self):
        with self.assertRaises(CoverError) as caught:
            await self.service.download_online("zz", "1", "x")
        self.assertEqual(caught.exception.kind, "invalid source")
        self.settings = validate_cover_settings({"rvc_root": "x"})
        with self.assertRaises(CoverError) as caught:
            await self.service.download_online("wy", "1", "x")
        self.assertEqual(caught.exception.kind, "MusicKeyNotSet")

    async def test_auto_search_merges_in_source_order_and_reports_errors(self):
        self.music.results["wy"] = [hit("wy", "1", "A")]
        self.music.results["kg"] = MusicError("api_failed")
        self.music.results["qq"] = [hit("qq", "2", "B"), hit("qq", "3", "C")]
        outcome = await self.service.search_online("歌", "auto")
        self.assertEqual([r["id"] for r in outcome["results"]], ["1", "2", "3"])
        self.assertEqual(outcome["errors"], [{"source": "kg", "error": "api_failed"}])

    async def test_single_source_search_and_missing_key(self):
        self.music.results["kw"] = [hit("kw", "5", "A")]
        outcome = await self.service.search_online("歌", "kw")
        self.assertEqual(self.music.searched_sources(), ["kw"])
        self.assertEqual(len(outcome["results"]), 1)
        self.settings = validate_cover_settings({"rvc_root": "x"})
        with self.assertRaises(CoverError) as caught:
            await self.service.search_online("歌", "auto")
        self.assertEqual(caught.exception.kind, "MusicKeyNotSet")


class LibraryMetaTests(OnlineCase):
    async def test_add_keeps_optional_fields_only_when_given(self):
        plain = await self.service.add_song("a.mp3", audio(30))
        self.assertNotIn("artist", plain)
        self.assertNotIn("source", plain)
        online = self.service.library.add("b.mp3", audio(31), {"title": "T", "artist": " A  B ", "source": "wy", "source_id": "5"})
        self.assertEqual((online["title"], online["artist"], online["source"], online["source_id"]), ("T", "A B", "wy", "5"))
        reloaded = type(self.service.library)(self.root / "data")
        self.assertEqual(reloaded.get(online["id"])["source_id"], "5")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        defaults = default_cover_settings()
        self.assertEqual(defaults["music_api_key"], "")
        self.assertEqual(defaults["sources"], ["wy", "kg", "qq"])
        self.assertEqual((defaults["min_full_seconds"], defaults["download_timeout_seconds"]), (90, 120))
        self.assertEqual(defaults["inst_gain_db"], -5)

    def test_old_config_without_the_new_fields_gets_defaults(self):
        old = {key: value for key, value in default_cover_settings().items()
               if key not in ("music_api_key", "sources", "min_full_seconds", "download_timeout_seconds")}
        settings = validate_cover_settings(old)
        self.assertEqual((settings["sources"], settings["min_full_seconds"], settings["music_api_key"]), (["wy", "kg", "qq"], 90, ""))

    def test_validation(self):
        bad = [
            {"sources": []}, {"sources": ["wy", "wy"]}, {"sources": ["xx"]}, {"sources": "wy"}, {"sources": [1]},
            {"sources": ["wy", "qq", "kw", "kg", "mg", "qi", "wy"]},
            {"min_full_seconds": -1}, {"min_full_seconds": 601}, {"min_full_seconds": 1.5}, {"min_full_seconds": True},
            {"download_timeout_seconds": 9}, {"download_timeout_seconds": 601},
            {"music_api_key": 5}, {"music_api_key": "a b"}, {"music_api_key": "x" * 201}, {"music_api_key": "a\nb"},
        ]
        for patch in bad:
            with self.assertRaises(EmotionConfigError, msg=str(patch)):
                validate_cover_settings(patch)
        ok = validate_cover_settings({"sources": ["qi", "mg", "kw", "kg", "qq", "wy"], "min_full_seconds": 0, "music_api_key": " k1 "})
        self.assertEqual((ok["sources"][0], ok["min_full_seconds"], ok["music_api_key"]), ("qi", 0, "k1"))

    def test_public_settings_hide_the_key(self):
        public = public_cover_settings(validate_cover_settings({"music_api_key": KEY}))
        self.assertNotIn("music_api_key", public)
        self.assertNotIn(KEY, json.dumps(public))
        self.assertEqual((public["key_set"], public["key_tail"]), (True, KEY[-4:]))
        empty = public_cover_settings(validate_cover_settings({}))
        self.assertEqual((empty["key_set"], empty["key_tail"]), (False, ""))
        short = public_cover_settings(validate_cover_settings({"music_api_key": "abc"}))
        self.assertEqual((short["key_set"], short["key_tail"]), (True, ""))  # never reveal most of a short key


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConsoleOnlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = ConfigStore(self.root / "data")
        await self.store.update(lambda config: config["cover"].update(rvc_root=str(self.root / "rvc"), music_api_key=KEY))
        self.music = FakeMusic()
        self.durations = {}
        self.cover = CoverService(
            data_dir=self.root / "data",
            get_settings=lambda: cover_settings(self.store.config),
            logger=FakeLogger(),
            worker_script=self.root / "worker.py",
            manager_factory=lambda **kwargs: FakeManager(**kwargs),
            probe=lambda rvc_root, path: self.durations.get(path.read_bytes()[-1], 200.0),
            music=self.music,
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
            return response.status, await response.text()

    async def jcall(self, *args, **kwargs):
        status, text = await self.call(*args, **kwargs)
        return status, json.loads(text)

    async def test_get_never_returns_the_key(self):
        for path in ("/api/cover", "/api/config"):
            status, text = await self.call(path)
            self.assertEqual(status, 200)
            self.assertNotIn(KEY, text)
        status, body = await self.jcall("/api/cover")
        self.assertEqual((body["settings"]["key_set"], body["settings"]["key_tail"]), (True, KEY[-4:]))
        self.assertNotIn("music_api_key", body["settings"])
        self.assertEqual(body["settings"]["sources"], ["wy", "kg", "qq"])

    async def test_saving_without_a_key_keeps_it_and_a_new_key_replaces_it(self):
        _, body = await self.jcall("/api/cover")
        status, saved = await self.jcall("/api/cover/settings", "POST", {**body["settings"], "min_full_seconds": 60})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.config["cover"]["music_api_key"], KEY)
        self.assertEqual(self.store.config["cover"]["min_full_seconds"], 60)
        self.assertNotIn("music_api_key", saved["settings"])
        status, _ = await self.jcall("/api/cover/settings", "POST", {**body["settings"], "music_api_key": ""})
        self.assertEqual(self.store.config["cover"]["music_api_key"], KEY)
        status, text = await self.call("/api/cover/settings", "POST", {**body["settings"], "music_api_key": "NEWKEY-abcdefghij"})
        self.assertEqual(status, 200)
        self.assertNotIn("NEWKEY", text)
        self.assertEqual(self.store.config["cover"]["music_api_key"], "NEWKEY-abcdefghij")

    async def test_explicit_clear_and_validation_errors(self):
        _, body = await self.jcall("/api/cover")
        status, saved = await self.jcall("/api/cover/settings", "POST", {**body["settings"], "clear_music_api_key": True})
        self.assertEqual((status, saved["settings"]["key_set"]), (200, False))
        self.assertEqual(self.store.config["cover"]["music_api_key"], "")
        for patch in ({"sources": ["wy", "wy"]}, {"sources": []}, {"min_full_seconds": 700}, {"music_api_key": "a b"}):
            status, _ = await self.jcall("/api/cover/settings", "POST", {**body["settings"], **patch})
            self.assertEqual(status, 400, str(patch))

    async def test_search_route(self):
        self.music.results["wy"] = [hit("wy", "1", "春日影", "MyGO", "专辑")]
        status, body = await self.jcall("/api/cover/online/search", "POST", {"name": "春日影", "source": "wy"})
        self.assertEqual(status, 200)
        self.assertEqual(body["results"][0]["album"], "专辑")
        status, body = await self.jcall("/api/cover/online/search", "POST", {"name": "春日影", "source": "auto"})
        self.assertEqual(self.music.searched_sources(), ["wy", "wy", "kg", "qq"])
        for payload in ({"name": "", "source": "wy"}, {"name": "x", "source": "zz"}, {"source": "wy"}, {"name": 5.5, "source": "wy"}, {"name": "x" * 201, "source": "wy"}):
            status, _ = await self.jcall("/api/cover/online/search", "POST", payload)
            self.assertEqual(status, 400, str(payload))

    async def test_search_without_a_key_is_a_clean_400(self):
        await self.store.update(lambda config: config["cover"].update(music_api_key=""))
        status, body = await self.jcall("/api/cover/online/search", "POST", {"name": "x", "source": "auto"})
        self.assertEqual((status, body), (400, {"error": "MusicKeyNotSet"}))

    async def test_download_route_and_preview_error(self):
        self.music.offer("kg", "5", "孤勇者", audio(40), "陈奕迅")
        status, body = await self.jcall("/api/cover/online/download", "POST", {"source": "kg", "id": "5", "name": "孤勇者", "artist": "陈奕迅"})
        self.assertEqual(status, 200)
        self.assertEqual((body["song"]["artist"], body["song"]["source"], body["song"]["source_id"]), ("陈奕迅", "kg", "5"))
        self.music.offer("wy", "6", "另一首", audio(41))
        self.durations = {41: 20.0}
        status, body = await self.jcall("/api/cover/online/download", "POST", {"source": "wy", "id": 6, "name": "另一首"})
        self.assertEqual((status, body), (422, {"error": "preview_only"}))
        _, overview = await self.jcall("/api/cover")
        self.assertEqual([song["title"] for song in overview["songs"]], ["孤勇者"])
        status, _ = await self.jcall("/api/cover/online/download", "POST", {"source": "zz", "id": "1", "name": "x"})
        self.assertEqual(status, 400)

    async def test_fetch_route(self):
        self.music.offer("wy", "1", "春日影", audio(42), "MyGO")
        status, body = await self.jcall("/api/cover/online/fetch", "POST", {"name": "春日影"})
        self.assertEqual((status, body["source"], body["downloaded"], body["song"]["title"]), (200, "wy", True, "春日影"))
        status, body = await self.jcall("/api/cover/online/fetch", "POST", {"name": "春日影", "artist": "MyGO"})
        self.assertEqual((body["source"], body["downloaded"]), ("library", False))
        self.music.results.clear()
        status, body = await self.jcall("/api/cover/online/fetch", "POST", {"name": "不存在的歌"})
        self.assertEqual((status, body), (404, {"error": "not_found"}))
        self.assertNotIn(KEY, json.dumps(body))

    async def test_online_routes_reject_a_foreign_origin(self):
        evil = {"Origin": "http://evil.example"}
        for path, payload in (
            ("/api/cover/online/search", {"name": "x", "source": "wy"}),
            ("/api/cover/online/download", {"source": "wy", "id": "1", "name": "x"}),
            ("/api/cover/online/fetch", {"name": "x"}),
        ):
            status, _ = await self.call(path, "POST", payload, headers=evil)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.music.calls, [])


if __name__ == "__main__":
    unittest.main()
