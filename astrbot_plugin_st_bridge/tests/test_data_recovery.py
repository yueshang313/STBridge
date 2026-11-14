"""101e: an unusable config.json / affinity.json no longer stops the core; it is set aside and replaced."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, default_config


class ConfigRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def good_config(self, marker):
        config = default_config()
        config["default_character"] = marker
        return config

    def test_bom_in_a_good_file_is_fine(self):
        ConfigStore(self.dir)  # writes a normal file
        path = self.dir / "config.json"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        store = ConfigStore(self.dir)
        self.assertIsNone(store.recovery)
        self.assertEqual(list(self.dir.glob("config.json.broken-*")), [])

    def test_broken_main_file_with_a_good_backup_uses_the_backup(self):
        first = ConfigStore(self.dir)
        asyncio.run(first.update(lambda c: c.__setitem__("default_character", "alice")))
        asyncio.run(first.update(lambda c: c.__setitem__("default_character", "bob")))  # .bak now holds "alice"
        backup_bytes = (self.dir / "config.json.bak").read_bytes()
        (self.dir / "config.json").write_bytes(b"{ not json")
        store = ConfigStore(self.dir)
        self.assertEqual(store.recovery["used"], "backup")
        self.assertEqual(store.config["default_character"], "alice")
        broken = self.dir / store.recovery["broken"][0]
        self.assertTrue(broken.name.startswith("config.json.broken-"))
        self.assertEqual(broken.read_bytes(), b"{ not json")
        # config.json is back, and the backup was not touched by the recovery.
        self.assertEqual(json.loads((self.dir / "config.json").read_text(encoding="utf-8"))["default_character"], "alice")
        self.assertEqual((self.dir / "config.json.bak").read_bytes(), backup_bytes)

    def test_both_broken_uses_defaults_and_keeps_both_broken_files(self):
        first = ConfigStore(self.dir)
        asyncio.run(first.update(lambda c: c.__setitem__("default_character", "alice")))
        asyncio.run(first.update(lambda c: c.__setitem__("default_character", "bob")))
        (self.dir / "config.json").write_bytes(b"\x00\x01 garbage")
        (self.dir / "config.json.bak").write_bytes(b"also garbage")
        store = ConfigStore(self.dir)
        self.assertEqual(store.recovery["used"], "default")
        self.assertEqual(len(store.recovery["broken"]), 2)
        contents = sorted((self.dir / name).read_bytes() for name in store.recovery["broken"])
        self.assertEqual(contents, sorted([b"\x00\x01 garbage", b"also garbage"]))
        self.assertEqual(store.config["default_character"], default_config()["default_character"])
        self.assertFalse((self.dir / "config.json.bak").exists())

    def test_fallback_never_replaces_the_good_backup_until_the_user_saves(self):
        first = ConfigStore(self.dir)
        asyncio.run(first.update(lambda c: c.__setitem__("default_character", "alice")))
        asyncio.run(first.update(lambda c: c.__setitem__("default_character", "bob")))
        good_backup = (self.dir / "config.json.bak").read_bytes()
        (self.dir / "config.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
        store = ConfigStore(self.dir)
        self.assertEqual((self.dir / "config.json.bak").read_bytes(), good_backup)
        self.assertIsNotNone(store.recovery)
        asyncio.run(store.update(lambda c: c.__setitem__("default_character", "carol")))
        self.assertIsNone(store.recovery)  # saved once: the notice goes away
        self.assertEqual(ConfigStore(self.dir).config["default_character"], "carol")

    def test_two_recoveries_in_the_same_second_do_not_collide(self):
        for attempt in range(2):
            ConfigStore(self.dir)
            (self.dir / "config.json").write_bytes(b"{ broken %d" % attempt)
            store = ConfigStore(self.dir)
            self.assertEqual(store.recovery["used"], "default")
        self.assertEqual(len(list(self.dir.glob("config.json.broken-*"))), 2)


class AffinityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bom_is_fine(self):
        store = AffinityStore(self.dir)
        store.set_value("k", 7)
        path = self.dir / "affinity.json"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        again = AffinityStore(self.dir)
        self.assertIsNone(again.recovery)
        self.assertEqual(again.data["users"]["k"]["value"], 7)

    def test_broken_file_with_a_good_backup_uses_the_backup(self):
        store = AffinityStore(self.dir)
        store.set_value("k", 7)
        store.set_value("k", 9)  # the .bak holds the state before this write (7)
        (self.dir / "affinity.json").write_bytes(b"{ broken")
        again = AffinityStore(self.dir)
        self.assertEqual(again.recovery["used"], "backup")
        self.assertEqual(again.data["users"]["k"]["value"], 7)
        self.assertEqual((self.dir / again.recovery["broken"][0]).read_bytes(), b"{ broken")

    def test_both_broken_starts_empty_and_keeps_the_broken_files(self):
        store = AffinityStore(self.dir)
        store.set_value("k", 7)
        store.set_value("k", 9)
        (self.dir / "affinity.json").write_bytes(b"{ broken")
        (self.dir / "affinity.json.bak").write_bytes(b"[1, 2")
        again = AffinityStore(self.dir)
        self.assertEqual(again.recovery["used"], "empty")
        self.assertEqual(again.data["users"], {})
        self.assertEqual(len(again.recovery["broken"]), 2)
        self.assertEqual(
            sorted((self.dir / name).read_bytes() for name in again.recovery["broken"]),
            sorted([b"{ broken", b"[1, 2"]),
        )


class ConsoleRecoveryNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_carries_the_notices_and_dismiss_clears_them(self):
        from aiohttp import ClientSession
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import (
            ConsoleService,
            create_console_app,
        )
        from test_console_server import _unused_loopback_port

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_bytes(b"{ broken")
            (root / "affinity.json").write_bytes(b"{ broken")
            store = ConfigStore(root)
            affinity = AffinityStore(root)
            port = _unused_loopback_port()
            service = ConsoleService(create_console_app(store, affinity_store=affinity), port=port)
            await service.start()
            try:
                async with ClientSession() as client:
                    base = f"http://127.0.0.1:{port}"
                    async with client.get(base + "/api/status") as response:
                        status = await response.json()
                    files = {item["file"]: item for item in status["data_recovery"]}
                    self.assertEqual(set(files), {"config.json", "affinity.json"})
                    self.assertEqual(files["config.json"]["used"], "default")
                    self.assertEqual(files["affinity.json"]["used"], "empty")
                    async with client.post(base + "/api/recovery/dismiss", json={}) as response:
                        self.assertEqual(response.status, 200)
                    async with client.get(base + "/api/status") as response:
                        status = await response.json()
                    self.assertEqual(status["data_recovery"], [])
            finally:
                await service.stop()


if __name__ == "__main__":
    unittest.main()
