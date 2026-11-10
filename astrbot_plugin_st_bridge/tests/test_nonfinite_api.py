"""F1: every console save endpoint answers 400 (never 500) to Infinity / -Infinity / NaN in any number.

Python's json module accepts these three words, so a hand-written request can carry them past the JSON parser.
For each endpoint the test takes the settings the console shows, puts a bad value into every number of them,
one at a time, and expects 400 and an unchanged config.
"""

import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_hub import RemotePlatform
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore

BAD_NUMBERS = (float("inf"), float("-inf"), float("nan"))
# (GET path, key of the settings in the answer or None for the whole answer, POST path)
SETTINGS_ENDPOINTS = (
    ("/api/queue", "settings", "/api/queue/settings"),
    ("/api/network", "settings", "/api/network"),
    ("/api/affinity", "settings", "/api/affinity/settings"),
    ("/api/stickers", "settings", "/api/stickers/settings"),
    ("/api/cleanup", "settings", "/api/cleanup/settings"),
    ("/api/cover", "settings", "/api/cover/settings"),
    ("/api/cover", "song_request", "/api/cover/song-request/settings"),
    ("/api/schedule", "settings", "/api/schedule/settings"),
)


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def number_paths(value, prefix=()):
    """The paths of every number (not a boolean) inside nested dicts and lists."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield prefix
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from number_paths(item, prefix + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from number_paths(item, prefix + (index,))


def with_value(settings, path, value):
    result = copy.deepcopy(settings)
    target = result
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return result


class NonFiniteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        ConfigStore(self.root)
        ports = dict(zip(("st_port", "bridge_port", "console_port", "connector_port"), free_ports(4)))
        path = self.root / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["network"] = ports
        path.write_text(json.dumps(config), encoding="utf-8")
        self.core = BridgeCore(RemotePlatform(), lambda: self.root)
        await self.core.initialize()
        self.addAsyncCleanup(self.core.terminate)
        self.base = f"http://127.0.0.1:{ports['console_port']}"
        self.session = ClientSession()
        self.addAsyncCleanup(self.session.close)

    async def call(self, path, method="GET", payload=None):
        async with self.session.request(method, self.base + path, json=payload) as response:
            try:
                return response.status, await response.json()
            except Exception:
                return response.status, None

    def stored(self):
        return json.loads((self.root / "config.json").read_text(encoding="utf-8"))

    async def test_every_number_of_every_settings_endpoint_refuses_infinity_and_nan(self):
        checked = 0
        for get_path, key, post_path in SETTINGS_ENDPOINTS:
            status, body = await self.call(get_path)
            self.assertEqual(status, 200, get_path)
            settings = body[key] if key else body
            paths = list(number_paths(settings))
            self.assertTrue(paths, f"{get_path} shows no number to try")
            before = self.stored()
            for path in paths:
                for bad in BAD_NUMBERS:
                    with self.subTest(endpoint=post_path, field=".".join(map(str, path)), value=str(bad)):
                        status, _ = await self.call(post_path, "POST", with_value(settings, path, bad))
                        self.assertEqual(status, 400)
                        checked += 1
            self.assertEqual(self.stored(), before, f"{post_path} changed the config")
        self.assertGreater(checked, 150)

    async def test_the_tts_settings_refuse_infinity_and_nan(self):
        status, body = await self.call("/api/tts")
        self.assertEqual(status, 200)
        settings = body["settings"]
        before = self.stored()
        checked = 0
        for path in number_paths(settings):
            for bad in BAD_NUMBERS:
                with self.subTest(field=".".join(map(str, path)), value=str(bad)):
                    status, _ = await self.call("/api/tts/settings", "POST", with_value(settings, path, bad))
                    self.assertEqual(status, 400)
                    checked += 1
        self.assertGreater(checked, 30)
        self.assertEqual(self.stored(), before)

    async def test_schedule_plans_refuse_infinity_and_nan(self):
        status, body = await self.call("/api/schedule")
        plan = body["plans"][0]
        plan = {key: value for key, value in plan.items() if key != "next_fire"}
        self.assertEqual((await self.call("/api/schedule/plans", "POST", plan))[0], 200)  # the plan itself is fine
        before = self.stored()
        paths = list(number_paths(plan))
        self.assertTrue(paths)
        for path in paths:
            for bad in BAD_NUMBERS:
                with self.subTest(field=".".join(map(str, path)), value=str(bad)):
                    status, _ = await self.call("/api/schedule/plans", "POST", with_value(plan, path, bad))
                    self.assertEqual(status, 400)
        self.assertEqual(self.stored(), before)

    async def test_emotion_profiles_refuse_infinity_and_nan(self):
        self.assertEqual((await self.call("/api/models", "POST", {"name": "m", "gpt_model_path": "", "sovits_model_path": "", "reference_audio_root": ""}))[0], 200)
        self.assertEqual((await self.call("/api/characters", "POST", {"name": "角色", "model_name": "m", "default_emotion": "neutral", "emotions": {}}))[0], 200)
        profile = {"ref_audio_path": "", "prompt_text": "", "prompt_text_lang": "日文", "text_lang": "日文", "top_k": 10, "top_p": 0.9,
                   "temperature": 0.8, "speed_factor": 1.0, "split_sentence": False, "caption": "", "emoji": ""}
        path = "/api/characters/%E8%A7%92%E8%89%B2/emotions/neutral"
        self.assertEqual((await self.call(path, "PUT", profile))[0], 200)
        before = self.stored()
        for field in ("top_k", "top_p", "temperature", "speed_factor"):
            for bad in BAD_NUMBERS:
                with self.subTest(field=field, value=str(bad)):
                    status, _ = await self.call(path, "PUT", {**profile, field: bad})
                    self.assertEqual(status, 400)
        self.assertEqual(self.stored(), before)

    async def test_an_affinity_value_refuses_infinity_and_nan(self):
        for bad in BAD_NUMBERS:
            with self.subTest(value=str(bad)):
                status, _ = await self.call("/api/affinity/users/0123456789ab", "PUT", {"value": bad})
                self.assertEqual(status, 400)

    async def test_the_words_also_fail_when_written_as_raw_json(self):
        # Not through the client's own encoder: the body is the text a hand-written request would send.
        for body in ('{"max_waiting": Infinity, "max_wait_seconds": 60}', '{"max_waiting": 5, "max_wait_seconds": NaN}', '{"max_waiting": -Infinity, "max_wait_seconds": 60}'):
            with self.subTest(body=body):
                async with self.session.post(self.base + "/api/queue/settings", data=body, headers={"Content-Type": "application/json"}) as response:
                    self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
