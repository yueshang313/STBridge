import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import network_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import RESERVED_PORTS, default_cover_settings, validate_cover_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_config,
    default_tts_settings,
    tts_settings,
    validate_config,
    validate_tts_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from test_console_server import _unused_loopback_port
from test_plugin_lifecycle import _load_plugin_module, submodule


class NetworkSettingsTests(unittest.TestCase):
    def test_defaults_are_the_old_fixed_ports(self):
        self.assertEqual(network_config.default_network_settings(), {"st_port": 11451, "bridge_port": 11452, "console_port": 11453, "connector_port": 11454, "onebot_port": 11455})
        self.assertEqual(network_config.validate_network_settings({}), network_config.default_network_settings())
        self.assertEqual(default_config()["network"], network_config.default_network_settings())

    def test_a_partial_body_keeps_the_other_ports(self):
        self.assertEqual(network_config.validate_network_settings({"bridge_port": 12000})["st_port"], 11451)
        self.assertEqual(network_config.validate_network_settings({"bridge_port": 12000})["bridge_port"], 12000)

    def test_range_and_type_rules(self):
        for bad in (1023, 65536, 0, -1, 12000.5, "12000", None, True, float("nan"), float("inf")):
            with self.assertRaises(EmotionConfigError, msg=repr(bad)):
                network_config.validate_network_settings({"bridge_port": bad})
        self.assertEqual(network_config.validate_network_settings({"bridge_port": 1024})["bridge_port"], 1024)
        self.assertEqual(network_config.validate_network_settings({"bridge_port": 65535.0})["bridge_port"], 65535)
        with self.assertRaises(EmotionConfigError):
            network_config.validate_network_settings([1])

    def test_the_three_ports_must_differ(self):
        for body in ({"bridge_port": 11451}, {"console_port": 11452}, {"st_port": 11453}, {"st_port": 12000, "bridge_port": 12000}):
            with self.assertRaises(EmotionConfigError, msg=str(body)) as caught:
                network_config.validate_network_settings(body)
            self.assertIn("different", caught.exception.message)
        # swapping two is fine as long as the result is distinct
        self.assertEqual(network_config.validate_network_settings({"st_port": 11452, "bridge_port": 11451})["st_port"], 11452)

    def test_a_tts_or_cover_port_is_refused(self):
        tts = default_tts_settings()
        cover = default_cover_settings()
        tts["irodori"]["port"] = 12001
        tts["gpt_sovits"]["port"] = 12002
        cover["port"] = 12003
        for name, port, who in (("bridge_port", 12001, "Irodori"), ("console_port", 12002, "GPT-SoVITS"), ("st_port", 12003, "翻唱")):
            with self.assertRaises(EmotionConfigError, msg=name) as caught:
                network_config.validate_network_settings({name: port}, tts=tts, cover=cover)
            self.assertIn(who, caught.exception.message)
        # without the sections nothing is known to clash
        self.assertEqual(network_config.validate_network_settings({"bridge_port": 12001})["bridge_port"], 12001)

    def test_the_fixed_irodori_and_gpt_sovits_ports_stay_reserved(self):
        for port in (8088, 9880):
            with self.assertRaises(EmotionConfigError, msg=str(port)):
                network_config.validate_network_settings({"bridge_port": port})
        self.assertEqual(network_config.FIXED_RESERVED_PORTS, frozenset({8088, 9880}))

    def test_network_settings_and_reserved_ports_follow_the_config(self):
        self.assertEqual(network_config.network_settings({}), network_config.default_network_settings())
        self.assertEqual(network_config.network_settings({"network": "broken"}), network_config.default_network_settings())
        self.assertEqual(network_config.network_settings({"network": {"bridge_port": 70000}}), network_config.default_network_settings())
        config = {"network": {"st_port": 12000, "bridge_port": 12001, "console_port": 12002}}
        self.assertEqual(network_config.reserved_ports(config), frozenset({8088, 9880, 11454, 11455, 12000, 12001, 12002}))
        self.assertEqual(network_config.reserved_ports({}), RESERVED_PORTS)


class OtherServicesRefuseTheNetworkPortsTests(unittest.TestCase):
    def test_cover_port_checks_follow_the_network_settings(self):
        moved = frozenset({8088, 9880, 12000, 12001, 12002})
        with self.assertRaises(EmotionConfigError):
            validate_cover_settings({"port": 12001}, reserved=moved)
        # a port that used to be reserved is free once the bridge moved away from it
        self.assertEqual(validate_cover_settings({"port": 11452}, reserved=moved)["port"], 11452)
        # without a reserved set: the old fixed list
        with self.assertRaises(EmotionConfigError):
            validate_cover_settings({"port": 11452})
        for port in (8088, 9880):
            with self.assertRaises(EmotionConfigError):
                validate_cover_settings({"port": port}, reserved=moved)

    def test_tts_ports_may_not_be_a_network_port(self):
        network = {"st_port": 12000, "bridge_port": 12001, "console_port": 12002}
        settings = default_tts_settings()
        self.assertEqual(validate_tts_settings(settings, network=network)["irodori"]["port"], 8088)
        for engine in ("irodori", "gpt_sovits"):
            bad = default_tts_settings()
            bad[engine]["port"] = 12001
            with self.assertRaises(EmotionConfigError, msg=engine):
                validate_tts_settings(bad, network=network)
            validate_tts_settings(bad)  # no network given: no cross-check
        self.assertEqual(tts_settings({"network": network, "tts": {"irodori": {"port": 12001}}}), default_tts_settings())

    def test_a_whole_config_is_cross_checked(self):
        config = default_config()
        validate_config(config)
        config["network"]["bridge_port"] = config["tts"]["irodori"]["port"] = 12001
        with self.assertRaises(EmotionConfigError):
            validate_config(config)
        config = default_config()
        config["cover"]["port"] = 11452
        with self.assertRaises(EmotionConfigError):
            validate_config(config)
        config["network"]["bridge_port"] = 12001  # moving the bridge frees 11452 for the worker
        validate_config(config)


class StoreTests(unittest.TestCase):
    def test_an_older_config_gets_the_default_network_section(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config()
            del config["network"]
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            store = ConfigStore(root)
            self.assertEqual(store.config["network"], network_config.default_network_settings())

    def test_the_saved_network_section_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config()
            config["network"] = {"st_port": 12000, "bridge_port": 12001, "console_port": 12002}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(ConfigStore(root).config["network"], {"st_port": 12000, "bridge_port": 12001, "console_port": 12002})


class ConsoleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConfigStore(Path(self.directory.name))
        self.port = _unused_loopback_port()
        self.in_use = dict(network_config.default_network_settings())
        self.service = ConsoleService(create_console_app(self.store, network_in_use=lambda: self.in_use), port=self.port)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", payload=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload) as response:
            return response.status, await response.json()

    async def test_get_and_save_the_ports(self):
        status, body = await self.call("/api/network")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], network_config.default_network_settings())
        self.assertFalse(body["reload_required"])
        self.assertEqual(body["console_url"], "http://127.0.0.1:11453")
        status, body = await self.call("/api/network", "POST", {"st_port": 12000, "bridge_port": 12001, "console_port": 12002})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], {"st_port": 12000, "bridge_port": 12001, "console_port": 12002, "connector_port": 11454, "onebot_port": 11455})
        self.assertTrue(body["reload_required"])
        self.assertEqual(body["in_use"], network_config.default_network_settings())
        self.assertEqual(body["console_url"], "http://127.0.0.1:12002")
        self.assertEqual(self.store.config["network"], body["settings"])
        self.assertEqual(ConfigStore(Path(self.directory.name)).config["network"], body["settings"])
        # saving the running ports again needs no reload
        status, body = await self.call("/api/network", "POST", dict(self.in_use))
        self.assertFalse(body["reload_required"])

    async def test_bad_ports_are_400_and_change_nothing(self):
        before = dict(self.store.config["network"])
        self.store.config["tts"]["irodori"]["port"] = 12009
        for payload in ({"bridge_port": 80}, {"bridge_port": 11451}, {"console_port": 12009}, {"st_port": 8088}, {"bridge_port": "x"}):
            status, body = await self.call("/api/network", "POST", payload)
            self.assertEqual(status, 400, str(payload))
        status, _ = await self.call("/api/network", "POST", [1])
        self.assertEqual(status, 400)
        self.assertEqual(self.store.config["network"], before)


class PluginStartTests(unittest.IsolatedAsyncioTestCase):
    async def start_plugin(self, config, during=None):
        module = _load_plugin_module()
        captured = {}

        class FakeBridgeService:
            def __init__(self, app, http_config, gate):
                captured["http_config"] = http_config
                self.gate = gate
                self.state = HttpServiceState.NEW
                self.app = app

            async def start(self):
                self.state = HttpServiceState.RUNNING

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = HttpServiceState.STOPPED

        class FakeConsole:
            def __init__(self, app, **kwargs):
                captured["console_port"] = kwargs.get("port")
                captured["console_app"] = app

            async def start(self):
                pass

            async def stop(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            if config is not None:
                (Path(directory) / "config.json").write_text(json.dumps(config), encoding="utf-8")
            plugin = module.STBridgePlugin(context=None, config={})
            with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FakeBridgeService), \
                    patch.object(submodule(module, "mixin_lifecycle"), "ConsoleService", FakeConsole), \
                    patch.object(module.star, "StarTools", type("Tools", (), {"get_data_dir": staticmethod(lambda _name: directory)}), create=True):
                await plugin.initialize()
                captured["plugin"] = plugin
                if during is not None:
                    await during(plugin)
                captured["store"] = plugin._core._emotion_store
                yield_value = captured
                await plugin.terminate()
        return yield_value

    async def test_the_plugin_listens_on_the_configured_ports(self):
        config = default_config()
        config["network"] = {"st_port": 12000, "bridge_port": 12001, "console_port": 12002}
        captured = await self.start_plugin(config)
        self.assertEqual(captured["http_config"].allowed_origin, "http://localhost:12000")
        self.assertEqual(captured["http_config"].listen_port, 12001)
        self.assertEqual(captured["http_config"].listen_host, "127.0.0.1")
        self.assertEqual(captured["console_port"], 12002)
        self.assertEqual(captured["plugin"]._core._network_in_use, {"st_port": 12000, "bridge_port": 12001, "console_port": 12002, "connector_port": 11454, "onebot_port": 11455})

    async def test_without_a_network_section_the_old_ports_are_used(self):
        config = default_config()
        del config["network"]
        captured = await self.start_plugin(config)
        self.assertEqual(captured["http_config"].allowed_origin, "http://localhost:11451")
        self.assertEqual(captured["http_config"].listen_port, 11452)
        self.assertIsNone(captured["console_port"])  # the service's own default (11453)

    async def test_a_missing_store_falls_back_to_the_old_ports(self):
        module = _load_plugin_module()
        captured = {}

        class FakeBridgeService:
            def __init__(self, app, http_config, gate):
                captured["http_config"] = http_config
                self.gate = gate
                self.state = HttpServiceState.NEW

            async def start(self):
                self.state = HttpServiceState.RUNNING

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = HttpServiceState.STOPPED

        def failing(_name):
            raise RuntimeError("no data dir")

        plugin = module.STBridgePlugin(context=None, config={})
        with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FakeBridgeService), \
                patch.object(module.star, "StarTools", type("Tools", (), {"get_data_dir": staticmethod(failing)}), create=True):
            await plugin.initialize()
            await plugin.terminate()
        self.assertEqual(captured["http_config"].listen_port, 11452)
        self.assertEqual(captured["http_config"].allowed_origin, "http://localhost:11451")

    async def test_a_saved_change_waits_for_the_next_start(self):
        seen = {}

        async def during(plugin):
            # the running plugin keeps its ports even though the config now holds others
            await plugin._core._emotion_store.update(lambda data: data.update(network={"st_port": 12000, "bridge_port": 12001, "console_port": 12002}))
            seen["in_use"] = dict(plugin._core._network_in_use)
            seen["status"] = plugin._core._network_in_use == network_config.default_network_settings()

        captured = await self.start_plugin(default_config(), during)
        self.assertEqual(captured["http_config"].listen_port, 11452)
        self.assertTrue(seen["status"], seen)


if __name__ == "__main__":
    unittest.main()
