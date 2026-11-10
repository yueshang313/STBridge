"""P7b: the online song source client (parsing, matching rules, redaction, download limits)."""

import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.music_source import (
    MusicClient,
    MusicError,
    is_variant,
    normalize_text,
    parse_search,
    pick_result,
    redact,
)

KEY = "TESTKEY-not-a-real-key-0000"


def item(name, artist="歌手", source="wy", song_id="1"):
    return {"source": source, "id": song_id, "name": name, "artist": artist, "album": ""}


class NormalizeTests(unittest.TestCase):
    def test_case_space_punctuation_and_brackets_are_ignored(self):
        self.assertEqual(normalize_text(" Haruhikage！ (Live) "), "haruhikage")
        self.assertEqual(normalize_text("春日影　【TV size】"), "春日影")
        self.assertEqual(normalize_text("Hello, World."), normalize_text("hello world"))

    def test_a_title_that_is_only_a_bracket_keeps_its_text(self):
        self.assertEqual(normalize_text("(live)"), "live")
        self.assertEqual(normalize_text(None), "")

    def test_variant_words(self):
        self.assertTrue(is_variant("晴天 (原唱 周杰伦) / RyaVocal"))
        self.assertTrue(is_variant("春日影 DJ版"))
        self.assertFalse(is_variant("春日影"))
        self.assertFalse(is_variant("Live Forever", "live forever"))  # the query itself asks for it


class PickTests(unittest.TestCase):
    def test_exact_normalized_title_wins_over_the_first_result(self):
        results = [item("春日影 (cover)"), item("春日影 Live", song_id="2"), item("春日影", song_id="3")]
        self.assertEqual(pick_result(results, "春日影", None)["id"], "3")

    def test_variants_sort_behind_plain_matches(self):
        results = [item("晴天 (原唱 周杰伦) / RyaVocal", song_id="1"), item("晴天", song_id="2")]
        self.assertEqual(pick_result(results, "晴天", None)["id"], "2")
        # only variants: a variant with the right title still beats nothing
        self.assertEqual(pick_result(results[:1], "晴天", None)["id"], "1")

    def test_artist_must_be_contained_when_given(self):
        results = [item("晴天", "别人", song_id="1"), item("晴天", "周杰伦/别人", song_id="2")]
        self.assertEqual(pick_result(results, "晴天", "周杰伦")["id"], "2")
        self.assertIsNone(pick_result(results[:1], "晴天", "周杰伦"))

    def test_no_exact_match_falls_back_to_the_first_only_without_an_artist(self):
        results = [item("别的歌", song_id="9"), item("又一首", song_id="8")]
        self.assertEqual(pick_result(results, "春日影", None)["id"], "9")
        self.assertIsNone(pick_result(results, "春日影", "某人"))
        self.assertIsNone(pick_result([], "春日影", None))


class ParseTests(unittest.TestCase):
    def test_numeric_and_string_ids_and_artist_forms(self):
        body = {"code": 1, "data": [
            {"type": "wy", "id": 1901371647, "name": "孤勇者", "album": "A", "artist": "陈奕迅"},
            {"type": "qq", "id": "000abc", "name": " 春日影  ", "artist": ["MyGO", "CRYCHIC"]},
            {"id": None, "name": "no id"}, {"id": 3, "name": ""}, "junk", {"id": True, "name": "bool"},
        ]}
        results = parse_search(body, "wy")
        self.assertEqual([r["id"] for r in results], ["1901371647", "000abc"])
        self.assertEqual(results[0], {"source": "wy", "id": "1901371647", "name": "孤勇者", "artist": "陈奕迅", "album": "A"})
        self.assertEqual((results[1]["name"], results[1]["artist"], results[1]["album"]), ("春日影", "MyGO/CRYCHIC", ""))

    def test_failure_shapes_raise(self):
        for body in ({"code": 0, "msg": "获取失败", "data": ""}, {"code": 1, "data": ""}, {"code": 1}, [], "x"):
            with self.assertRaises(MusicError) as caught:
                parse_search(body, "wy")
            self.assertEqual(caught.exception.kind, "api_failed")


class RedactTests(unittest.TestCase):
    def test_key_parameter_and_bare_key_are_masked(self):
        text = f"GET https://myhkw.cn/open/music/url?id=1&key={KEY}&type=wy failed for {KEY}"
        masked = redact(text, KEY)
        self.assertNotIn(KEY, masked)
        self.assertIn("key=***", masked)
        self.assertNotIn("abc", redact("https://x/y?key=abc&z=1").replace("key=***", ""))
        self.assertIn("&z=1", redact("https://x/y?key=abc&z=1"))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.search_body = {"code": 1, "data": [{"type": "wy", "id": 7, "name": "歌", "artist": "人", "album": "专"}]}
        self.url_body = {"code": 1, "data": "http://placeholder/file.mp3"}
        self.file_body = b"ID3" + b"x" * 2000
        self.file_status = 200

        async def search(request):
            self.requests.append(("search", dict(request.query), request.headers.get("User-Agent")))
            return web.json_response(self.search_body)

        async def url(request):
            self.requests.append(("url", dict(request.query), None))
            return web.json_response(self.url_body)

        async def file(request):
            return web.Response(body=self.file_body, status=self.file_status)

        async def slow(request):
            await asyncio.sleep(1)
            return web.Response(body=b"late")

        async def chunks(request):
            response = web.StreamResponse()
            await response.prepare(request)
            self.sent = 0
            try:
                for _ in range(400):
                    await response.write(b"y" * 1024)
                    self.sent += 1024
                    await asyncio.sleep(0)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return response

        app = web.Application()
        app.router.add_get("/open/music/search", search)
        app.router.add_get("/open/music/url", url)
        app.router.add_get("/file.mp3", file)
        app.router.add_get("/slow", slow)
        app.router.add_get("/chunks", chunks)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.key = KEY
        self.client = MusicClient(lambda: self.key, base_url=f"{self.base}/open/music")

    async def asyncTearDown(self):
        await self.server.close()

    async def test_search_sends_the_documented_query(self):
        results = await self.client.search("春日影", "wy", 10)
        self.assertEqual(results[0]["id"], "7")
        kind, query, agent = self.requests[0]
        self.assertEqual(query, {"key": KEY, "name": "春日影", "type": "wy", "page": "1", "limit": "10", "format": "1"})
        self.assertEqual(agent, "Mozilla/5.0")

    async def test_empty_key_fails_before_any_request(self):
        for empty in ("", "   ", None):
            self.key = empty
            with self.assertRaises(MusicError) as caught:
                await self.client.search("x", "wy")
            self.assertEqual(caught.exception.kind, "music api key not set")
        self.assertEqual(self.requests, [])

    async def test_unknown_source_is_rejected_locally(self):
        with self.assertRaises(MusicError):
            await self.client.search("x", "zz")
        with self.assertRaises(MusicError):
            await self.client.get_url("1", "zz")
        self.assertEqual(self.requests, [])

    async def test_get_url_needs_code_1_and_an_http_link(self):
        self.url_body = {"code": 1, "data": "https://cdn.example/a.mp3"}
        self.assertEqual(await self.client.get_url(5, "kg"), "https://cdn.example/a.mp3")
        self.assertEqual(self.requests[0][1], {"key": KEY, "id": "5", "type": "kg"})
        for body in ({"code": 0, "data": ""}, {"code": 1, "data": ""}, {"code": 1, "data": "ftp://x/y"}, {"code": 1, "data": "file:///c:/a"}):
            self.url_body = body
            with self.assertRaises(MusicError) as caught:
                await self.client.get_url(5, "kg")
            self.assertEqual(caught.exception.kind, "no_link")

    async def test_network_errors_never_carry_the_key(self):
        dead = MusicClient(lambda: KEY, base_url="http://127.0.0.1:1/open/music")
        with self.assertRaises(MusicError) as caught:
            await dead.search("x", "wy")
        self.assertTrue(caught.exception.kind.startswith("network:"))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(caught.exception.__cause__))
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_http_error_status(self):
        self.file_status = 404
        with self.assertRaises(MusicError) as caught:
            await self.client.download(f"{self.base}/file.mp3", max_bytes=10_000, timeout=5)
        self.assertEqual(caught.exception.kind, "HTTP404")

    async def test_download_returns_the_bytes(self):
        data = await self.client.download(f"{self.base}/file.mp3", max_bytes=10_000, timeout=5)
        self.assertEqual(data, self.file_body)

    async def test_download_over_the_limit_by_content_length(self):
        with self.assertRaises(MusicError) as caught:
            await self.client.download(f"{self.base}/file.mp3", max_bytes=1000, timeout=5)
        self.assertEqual(caught.exception.kind, "too_large")

    async def test_streamed_download_aborts_once_over_the_limit(self):
        with self.assertRaises(MusicError) as caught:
            await self.client.download(f"{self.base}/chunks", max_bytes=50_000, timeout=5)
        self.assertEqual(caught.exception.kind, "too_large")

    async def test_download_timeout_and_empty_body(self):
        with self.assertRaises(MusicError) as caught:
            await self.client.download(f"{self.base}/slow", max_bytes=1000, timeout=0.2)
        self.assertEqual(caught.exception.kind, "timeout")
        self.file_body = b""
        with self.assertRaises(MusicError) as caught:
            await self.client.download(f"{self.base}/file.mp3", max_bytes=1000, timeout=5)
        self.assertEqual(caught.exception.kind, "empty")

    async def test_only_http_and_https_links_download(self):
        for url in ("file:///c:/windows/win.ini", "ftp://example.com/a.mp3", "//example.com/a", "javascript:1", ""):
            with self.assertRaises(MusicError) as caught:
                await self.client.download(url, max_bytes=1000, timeout=1)
            self.assertEqual(caught.exception.kind, "invalid_url")


if __name__ == "__main__":
    unittest.main()
