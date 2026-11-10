"""R2b: the owner in the console config, the log files and their API, the connector port, and the plugin while the
core is away."""

import asyncio
import io
import json
import logging
import socket
import sys
import tempfile
import unittest
import zipfile
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets, log as core_log, log_files, network_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_client import ConnectorUnavailable
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_hub import ConnectorHub, RemotePlatform
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import MemoryTransport, RpcPeer
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError, default_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.owner_config import (
    default_owner_settings,
    owner_settings,
    validate_owner_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import PRIVATE, IncomingMessage
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION
from test_plugin_lifecycle import _bridge_event, _collect, _group_bridge_event, _load_plugin_module


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OwnerSettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(default_owner_settings(), {"platform_id": "", "sender_id": "", "imported": False})
        self.assertEqual(
            validate_owner_settings({"platform_id": " qq ", "sender_id": " u1 "}),
            {"platform_id": "qq", "sender_id": "u1", "imported": False},
        )
        for bad in (
            "x", None, {"platform_id": "qq"}, {"sender_id": "u"}, {"platform_id": 5, "sender_id": "u"},
            {"platform_id": "a" * 201, "sender_id": "u"}, {"platform_id": "a\nb", "sender_id": "u"},
            {"platform_id": "a", "sender_id": "u", "imported": "yes"},
        ):
            with self.subTest(bad=bad), self.assertRaises(EmotionConfigError):
                validate_owner_settings(bad)

    def test_an_unreadable_section_counts_as_no_owner(self):
        self.assertEqual(owner_settings({"owner": "junk"}), default_owner_settings())
        self.assertEqual(owner_settings({}), default_owner_settings())

    def test_new_and_old_configs_get_the_empty_owner(self):
        self.assertEqual(default_config()["owner"], default_owner_settings())
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            config = default_config()
            del config["owner"]
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(ConfigStore(Path(root)).config["owner"], default_owner_settings())

    def test_a_preset_never_carries_the_owner_and_applying_one_keeps_this_machines_owner(self):
        config = default_config()
        config["owner"] = {"platform_id": "qq", "sender_id": "me", "imported": True}
        self.assertNotIn("owner", config_presets.snapshot(config))
        theirs = config_presets.snapshot(default_config())
        theirs["owner"] = {"platform_id": "someone-else", "sender_id": "x", "imported": True}  # a hand-edited file
        applied = config_presets.normalize_config(theirs, config)
        self.assertEqual(applied["owner"], config["owner"])
        self.assertFalse(config_presets.is_modified(config, config_presets.snapshot(config)))


class OwnerImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.core = BridgeCore(RemotePlatform(), lambda: self.root)
        self.core._emotion_store = ConfigStore(self.root)

    def owner(self):
        return owner_settings(self.core._emotion_store.config)

    def message(self, platform_id, sender_id):
        return IncomingMessage(
            platform="qq_official", platform_id=platform_id, chat_type=PRIVATE, session_id="s", conversation_id="c",
            sender_id=sender_id, sender_name="n", group_id=None, text="hi", message_str="hi",
        )

    async def test_the_first_values_are_imported_once_and_later_ones_are_ignored(self):
        self.assertFalse(self.core.accept_private(self.message("qq", "me")))  # nobody is the owner yet
        await self.core.import_operator("qq", "me")
        self.assertEqual(self.owner(), {"platform_id": "qq", "sender_id": "me", "imported": True})
        self.assertTrue(self.core.accept_private(self.message("qq", "me")))
        await self.core.import_operator("other", "someone")
        self.assertEqual(self.owner()["sender_id"], "me")
        saved = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["owner"]["platform_id"], "qq")

    async def test_the_console_value_wins_at_once_and_an_import_never_overwrites_it(self):
        await self.core._emotion_store.update(lambda config: config.update(owner={"platform_id": "qq", "sender_id": "console", "imported": True}))
        self.assertTrue(self.core.accept_private(self.message("qq", "console")))
        self.assertEqual((self.core._operator_platform_id, self.core._operator_sender_id), ("qq", "console"))
        await self.core.import_operator("qq", "plugin-setting")
        self.assertEqual(self.owner()["sender_id"], "console")
        self.assertFalse(self.core.accept_private(self.message("qq", "plugin-setting")))

    async def test_an_empty_console_owner_after_an_import_is_not_filled_again(self):
        await self.core.import_operator("qq", "me")
        await self.core._emotion_store.update(lambda config: config.update(owner={"platform_id": "", "sender_id": "", "imported": True}))
        await self.core.import_operator("qq", "me")
        self.assertEqual(self.owner(), {"platform_id": "", "sender_id": "", "imported": True})

    async def test_values_that_are_not_text_or_empty_are_not_imported(self):
        for pair in ((None, "x"), ("x", None), ("", "x"), ("x", "  "), (5, 6)):
            with self.subTest(pair=pair):
                await self.core.import_operator(*pair)
                self.assertEqual(self.owner(), default_owner_settings())

    async def test_without_a_config_store_the_values_are_only_remembered(self):
        core = BridgeCore(RemotePlatform(), lambda: self.root)
        await core.import_operator("qq", "me")
        self.assertEqual((core._operator_platform_id, core._operator_sender_id), ("qq", "me"))

    async def test_the_hello_hands_the_plugins_values_over_once(self):
        platform = RemotePlatform()
        core = BridgeCore(platform, lambda: self.root)
        core._emotion_store = ConfigStore(self.root)
        hub = ConnectorHub(core, platform, token="t")

        async def say_hello(owner):
            near, far = MemoryTransport.pair()
            serving = asyncio.create_task(hub.serve(far))
            peer = RpcPeer(near, "c")
            runner = asyncio.create_task(peer.run())
            hello = await peer.call("hello", {"name": "astrbot", "version": CORE_VERSION, "token": "t", "import_owner": owner})
            await peer.close()
            await asyncio.gather(runner, serving, return_exceptions=True)
            return hello

        hello = await say_hello({"platform_id": "qq", "sender_id": "me"})
        self.assertEqual(owner_settings(core._emotion_store.config)["sender_id"], "me")
        self.assertEqual(hello["cache"]["operator"], {"platform_id": "qq", "sender_id": "me"})
        await say_hello({"platform_id": "qq", "sender_id": "someone-else"})
        self.assertEqual(owner_settings(core._emotion_store.config)["sender_id"], "me")


class ConsoleApiCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ConfigStore(self.root)
        self.connector = {"connected": False, "name": None, "version": None, "since": None}
        self.port = free_port()
        self.service = ConsoleService(create_console_app(self.store, connector_status=lambda: self.connector), port=self.port)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", payload=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload) as response:
            if response.content_type == "application/json":
                return response.status, await response.json()
            return response.status, await response.read()


class OwnerApiTests(ConsoleApiCase):
    async def test_get_and_save_the_owner(self):
        status, body = await self.call("/api/owner")
        self.assertEqual((status, body), (200, {"owner": default_owner_settings()}))
        status, body = await self.call("/api/owner", "POST", {"platform_id": " qq ", "sender_id": "me"})
        self.assertEqual((status, body["owner"]), (200, {"platform_id": "qq", "sender_id": "me", "imported": True}))
        self.assertEqual(owner_settings(self.store.config)["sender_id"], "me")
        # Emptying both is allowed (no owner) and still counts as "set by the console".
        status, body = await self.call("/api/owner", "POST", {"platform_id": "", "sender_id": ""})
        self.assertEqual((status, body["owner"]["imported"]), (200, True))

    async def test_bad_owner_bodies_are_refused_without_changing_anything(self):
        for payload in ({"platform_id": "qq"}, {"platform_id": 5, "sender_id": "x"}, {"platform_id": "a" * 201, "sender_id": "x"}, []):
            with self.subTest(payload=payload):
                status, _ = await self.call("/api/owner", "POST", payload)
                self.assertEqual(status, 400)
        self.assertEqual(owner_settings(self.store.config), default_owner_settings())

    async def test_the_status_carries_the_connector(self):
        status, body = await self.call("/api/status")
        self.assertEqual(body["connector"], self.connector)
        self.connector = {"connected": True, "name": "astrbot", "version": "2.0.0", "since": "2026-11-05T10:00:00"}
        status, body = await self.call("/api/status")
        self.assertEqual(body["connector"]["name"], "astrbot")


class LogApiTests(ConsoleApiCase):
    def write_logs(self):
        directory = self.root / "logs"
        directory.mkdir()
        (directory / "bridge.log").write_text("\n".join(f"line {index}" for index in range(1, 1251)) + "\n", encoding="utf-8")
        (directory / "bridge.log.2026-11-04").write_text("yesterday\n", encoding="utf-8")
        (directory / "secrets.txt").write_text("not a log\n", encoding="utf-8")

    async def test_no_logs_yet(self):
        status, body = await self.call("/api/logs")
        self.assertEqual((status, body["lines"], body["files"]), (200, [], []))

    async def test_the_tail_has_the_asked_number_of_lines_and_only_the_offered_choices(self):
        self.write_logs()
        status, body = await self.call("/api/logs?lines=100")
        self.assertEqual((len(body["lines"]), body["lines"][-1], body["lines"][0]), (100, "line 1250", "line 1151"))
        self.assertEqual(len((await self.call("/api/logs?lines=1000"))[1]["lines"]), 1000)
        for value in ("", "5", "999999", "abc", "../../x", "-1", "300.5"):
            with self.subTest(lines=value):
                self.assertEqual(len((await self.call(f"/api/logs?lines={value}"))[1]["lines"]), 300)
        self.assertEqual(len((await self.call("/api/logs"))[1]["lines"]), 300)
        self.assertEqual((await self.call("/api/logs"))[1]["choices"], [100, 300, 1000])

    async def test_only_the_cores_own_log_files_are_listed_and_zipped(self):
        self.write_logs()
        status, body = await self.call("/api/logs")
        self.assertEqual(body["files"], ["bridge.log.2026-11-04", "bridge.log"])
        status, data = await self.call("/api/logs/download")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["bridge.log", "bridge.log.2026-11-04"])
            self.assertIn("yesterday", archive.read("bridge.log.2026-11-04").decode("utf-8"))

    async def test_nothing_outside_the_log_directory_can_be_read(self):
        self.write_logs()
        (self.root / "config.json").write_text(json.dumps(default_config()), encoding="utf-8")
        for path in ("/api/logs?file=../config.json", "/api/logs?path=../config.json", "/api/logs/download?file=../config.json"):
            status, body = await self.call(path)
            self.assertEqual(status, 200)
            if isinstance(body, bytes):
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    self.assertNotIn("config.json", archive.namelist())
            else:
                self.assertFalse(any("version" in line for line in body["lines"]))
        self.assertEqual(log_files.log_directory(self.root), self.root / "logs")

    async def test_the_log_files_helpers_cope_with_a_missing_directory(self):
        empty = self.root / "nowhere"
        self.assertEqual((log_files.log_files(empty), log_files.tail_lines(empty, 100)), ([], []))
        zipped = zipfile.ZipFile(io.BytesIO(log_files.zip_logs(empty)))
        self.assertEqual(zipped.namelist(), [])


class LogConfigurationTests(unittest.TestCase):
    def test_the_log_rotates_daily_keeps_seven_days_and_writes_utf8(self):
        root_logger = logging.getLogger()
        saved = list(root_logger.handlers)
        saved_level = root_logger.level
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            try:
                path = core_log.configure_process_logging(Path(directory), stream=stream)
                self.assertEqual(path, Path(directory) / "logs" / "bridge.log")
                files = [handler for handler in root_logger.handlers if isinstance(handler, TimedRotatingFileHandler)]
                self.assertEqual(len(files), 1)
                handler = files[0]
                self.assertEqual((handler.when, handler.backupCount, handler.encoding), ("MIDNIGHT", 7, "utf-8"))
                logging.getLogger("st_bridge_core").info("[STBridge] 你好")
                logging.getLogger("st_bridge_core").warning("[STBridge] careful")
                for each in root_logger.handlers:
                    each.flush()
                self.assertEqual(stream.getvalue().splitlines(), ["INFO\t[STBridge] 你好", "WARNING\t[STBridge] careful"])
                self.assertIn("[STBridge] 你好", path.read_text(encoding="utf-8"))
            finally:
                for each in list(root_logger.handlers):
                    root_logger.removeHandler(each)
                    each.close()
                for each in saved:
                    root_logger.addHandler(each)
                root_logger.setLevel(saved_level)


class ConnectorPortTests(unittest.TestCase):
    def test_the_default_and_the_range(self):
        self.assertEqual(network_config.default_network_settings()["connector_port"], 11454)
        self.assertEqual(network_config.validate_network_settings({"connector_port": 12345})["connector_port"], 12345)
        for bad in (80, 70000, "x", None, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(EmotionConfigError):
                network_config.validate_network_settings({"connector_port": bad})

    def test_it_must_differ_from_the_other_ports_and_the_services(self):
        for body in ({"connector_port": 11451}, {"connector_port": 11452}, {"connector_port": 11453}, {"bridge_port": 11454}):
            with self.subTest(body=body), self.assertRaises(EmotionConfigError):
                network_config.validate_network_settings(body)
        with self.assertRaises(EmotionConfigError):
            network_config.validate_network_settings({"connector_port": 12100}, tts={"irodori": {"port": 12100}})
        with self.assertRaises(EmotionConfigError):
            network_config.validate_network_settings({"connector_port": 9880})

    def test_an_old_config_without_it_reads_the_default(self):
        self.assertEqual(network_config.network_settings({"network": {"st_port": 12000}})["connector_port"], 11454)
        self.assertIn(11454, network_config.reserved_ports({}))


class OfflineCoreTests(unittest.IsolatedAsyncioTestCase):
    """The plugin while its core is away: messages that would have been bridged get the cached "unavailable" line."""

    class OfflineClient:
        def __init__(self, hello=None):
            self.connected = False
            self.last_hello = hello

        async def ensure(self):
            return False

        async def call(self, *args, **kwargs):
            raise ConnectorUnavailable("core not connected")

        async def stream(self, *args, **kwargs):
            raise ConnectorUnavailable("core not connected")
            yield

    class OfflineRuntime:
        def __init__(self, client):
            self.client = client

    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.config = {"operator_platform_id": "qq-platform", "operator_sender_id": "qq-user"}

    def plugin(self, hello=None, config=None):
        client = self.OfflineClient(hello)
        # A runtime whose core never connects.
        self.module.STBridgePlugin._create_runtime = lambda plugin: OfflineCoreTests.OfflineRuntime(client)
        return self.module.STBridgePlugin(None, self.config if config is None else config)

    async def run_handler(self, handler, event):
        from unittest.mock import patch

        with patch.dict(sys.modules, self.module.TestBotpyModules):
            return await _collect(handler, event)

    async def test_the_owners_private_message_gets_the_built_in_line_before_any_hello(self):
        plugin = self.plugin()
        event = _bridge_event(self.module, "你好")
        results = await self.run_handler(plugin.stbridge_private_message, event)
        self.assertEqual([item.text for item in results], [self.module.UNAVAILABLE_TEXT])
        self.assertTrue(event.call_llm)

    async def test_the_cached_line_and_owner_of_the_last_hello_are_used(self):
        hello = {"cache": {"unavailable_text": "我睡着了，晚点再来。", "operator": {"platform_id": "qq-platform", "sender_id": "cached-owner"}}}
        plugin = self.plugin(hello)
        mine = await self.run_handler(plugin.stbridge_private_message, _bridge_event(self.module, "hi", sender_id="cached-owner"))
        self.assertEqual([item.text for item in mine], ["我睡着了，晚点再来。"])
        # The plugin's own setting is not the owner any more once the core has said who is.
        old = await self.run_handler(plugin.stbridge_private_message, _bridge_event(self.module, "hi"))
        self.assertEqual(old, [])

    async def test_other_people_and_commands_in_private_chat_are_left_alone(self):
        plugin = self.plugin()
        self.assertEqual(await self.run_handler(plugin.stbridge_private_message, _bridge_event(self.module, "hi", sender_id="stranger")), [])
        command = _bridge_event(self.module, "/好感度", extras={"handlers_parsed_params": {"x": 1}})
        self.assertEqual(await self.run_handler(plugin.stbridge_private_message, command), [])

    async def test_an_awakened_group_message_gets_the_line_and_an_unawakened_one_nothing(self):
        plugin = self.plugin()
        awake = await self.run_handler(plugin.stbridge_group_message, _group_bridge_event(self.module, "琴音 你好"))
        self.assertEqual([item.text for item in awake], [self.module.UNAVAILABLE_TEXT])
        quiet = _group_bridge_event(self.module, "路过")
        quiet.is_at_or_wake_command = False
        self.assertEqual(await self.run_handler(plugin.stbridge_group_message, quiet), [])

    async def test_commands_get_the_line_except_sing_for_strangers_and_ping_stays_local(self):
        plugin = self.plugin()
        for handler in (plugin.stbridge_affinity, plugin.stbridge_recover):
            results = await self.run_handler(handler, _bridge_event(self.module, "/x"))
            self.assertEqual([item.text for item in results], [self.module.UNAVAILABLE_TEXT])
        stranger = _bridge_event(self.module, "/唱 晴天", sender_id="stranger")
        self.assertEqual(await self.run_handler(plugin.stbridge_sing, stranger), [])
        owner = await self.run_handler(plugin.stbridge_sing, _bridge_event(self.module, "/唱 晴天"))
        self.assertEqual([item.text for item in owner], [self.module.UNAVAILABLE_TEXT])
        ping = await self.run_handler(plugin.stbridge_ping, _bridge_event(self.module, "/stbridge_ping"))
        self.assertEqual([item.text for item in ping], ["ST Bridge 插件已加载"])

    async def test_without_an_owner_setting_nobody_is_answered_in_private(self):
        plugin = self.plugin(config={})
        self.assertEqual(await self.run_handler(plugin.stbridge_private_message, _bridge_event(self.module, "hi")), [])


class PluginShapeTests(unittest.TestCase):
    def test_the_plugin_hands_the_owner_settings_over_and_keeps_terminate_on_its_own_class(self):
        module = _load_plugin_module()
        self.assertIn("terminate", module.STBridgePlugin.__dict__)
        plugin = module.STBridgePlugin(context=None, config={"operator_platform_id": " qq ", "operator_sender_id": "me"})
        self.assertEqual(plugin._owner_hint, {"platform_id": "qq", "sender_id": "me"})
        self.assertIsNone(module.STBridgePlugin(context=None, config={"operator_platform_id": "qq"})._owner_hint)


if __name__ == "__main__":
    unittest.main()
