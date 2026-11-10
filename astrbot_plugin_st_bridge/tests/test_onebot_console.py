"""R3: the chat platform in the console API, the presets, the AstrBot connector while NapCat is active, the start script."""

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.chat_platform import chat_platform_settings, default_chat_platform_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_hub import ConnectorHub, RemotePlatform
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import MemoryTransport, RpcPeer, message_to_dict
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, default_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import PRIVATE, IncomingMessage, PlatformAPI


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ChatPlatformApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ConfigStore(self.root)
        self.onebot = {
            "port": 11455, "url": "ws://127.0.0.1:11455/onebot", "token": "tok",
            "status": {"connected": False, "self_id": None, "since": None, "heartbeat_age": None, "format_warning": False},
        }
        self.port = free_port()
        self.service = ConsoleService(
            create_console_app(self.store, onebot_info=lambda: self.onebot, wake_prefix=lambda: ["/琴音", "琴音"]), port=self.port
        )
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", payload=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload) as response:
            return response.status, await response.json()

    async def test_the_default_is_astrbot_and_the_url_and_token_are_shown(self):
        status, body = await self.call("/api/chat-platform")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], default_chat_platform_settings())
        self.assertEqual((body["onebot"]["url"], body["onebot"]["token"], body["onebot"]["port"]), ("ws://127.0.0.1:11455/onebot", "tok", 11455))
        self.assertEqual(body["wake_prefix"], ["/琴音", "琴音"])

    async def test_saving_switches_the_platform_at_once_and_keeps_a_clean_list(self):
        status, body = await self.call("/api/chat-platform", "POST", {"active": "napcat", "group_whitelist": ["1", 2, "1"], "napcat_owner": " 99 "})
        self.assertEqual((status, body["settings"]), (200, {"active": "napcat", "group_whitelist": ["1", "2"], "napcat_owner": "99"}))
        self.assertEqual(chat_platform_settings(self.store.config)["active"], "napcat")
        reread = (await self.call("/api/chat-platform"))[1]
        self.assertEqual(reread["settings"]["group_whitelist"], ["1", "2"])

    async def test_bad_bodies_are_refused_and_change_nothing(self):
        for payload in ({"active": "qq"}, {"group_whitelist": ["x"]}, {"napcat_owner": "abc"}, [], "napcat"):
            with self.subTest(payload=payload):
                status, _ = await self.call("/api/chat-platform", "POST", payload)
                self.assertEqual(status, 400)
        self.assertEqual(chat_platform_settings(self.store.config), default_chat_platform_settings())

    async def test_the_status_carries_the_platform_and_the_onebot_state_but_not_the_token(self):
        status, body = await self.call("/api/status")
        self.assertEqual(body["platform"], "astrbot")
        self.assertEqual(body["onebot"], self.onebot["status"])
        self.assertNotIn("tok", str(body))
        await self.call("/api/chat-platform", "POST", {"active": "napcat"})
        self.assertEqual((await self.call("/api/status"))[1]["platform"], "napcat")

    async def test_without_a_core_the_onebot_part_is_empty(self):
        self.onebot = None
        body = (await self.call("/api/chat-platform"))[1]
        self.assertEqual(body["onebot"], {"url": None, "token": None, "port": None, "status": None})
        self.assertIsNone((await self.call("/api/status"))[1]["onebot"])

    async def test_the_network_api_carries_the_onebot_port(self):
        body = (await self.call("/api/network"))[1]
        self.assertEqual(body["settings"]["onebot_port"], 11455)
        status, saved = await self.call("/api/network", "POST", {**body["settings"], "onebot_port": 12455})
        self.assertEqual((status, saved["settings"]["onebot_port"]), (200, 12455))
        status, _ = await self.call("/api/network", "POST", {**body["settings"], "onebot_port": body["settings"]["connector_port"]})
        self.assertEqual(status, 400)


class PresetTests(unittest.TestCase):
    def test_the_platform_choice_stays_on_this_machine(self):
        config = default_config()
        config["chat_platform"] = {"active": "napcat", "group_whitelist": ["123"], "napcat_owner": "9"}
        self.assertNotIn("chat_platform", config_presets.snapshot(config))
        local = default_config()
        local["chat_platform"] = {"active": "napcat", "group_whitelist": ["123"], "napcat_owner": "9"}
        other = config_presets.snapshot(default_config())
        other["chat_platform"] = {"active": "astrbot", "group_whitelist": [], "napcat_owner": ""}
        applied = config_presets.normalize_config(other, local)
        self.assertEqual(applied["chat_platform"], local["chat_platform"])

    def test_an_old_config_and_an_old_preset_get_the_default_section(self):
        config = default_config()
        del config["chat_platform"]
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import fill_config_defaults, validate_config

        fill_config_defaults(config)
        validate_config(config)
        self.assertEqual(config["chat_platform"], default_chat_platform_settings())
        applied = config_presets.normalize_config(config_presets.snapshot(config), {})
        self.assertEqual(applied["chat_platform"], default_chat_platform_settings())


class StubPlatform(PlatformAPI):
    pass


class ConnectorWhileNapcatIsActive(unittest.IsolatedAsyncioTestCase):
    """AstrBot's connector keeps working as a connection, but its messages get no answer while NapCat is active."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.core = BridgeCore(StubPlatform(), lambda: self.root, operator_platform_id="qq", operator_sender_id="me")
        self.core._emotion_store = ConfigStore(self.root)
        self.hub = ConnectorHub(self.core, RemotePlatform(), token=None, log_connections=False)
        left, right = MemoryTransport.pair()
        self.serving = asyncio.create_task(self.hub.serve(left))
        self.peer = RpcPeer(right, "c", handlers={}, streams={})
        self.running = asyncio.create_task(self.peer.run())
        await self.peer.call("hello", {"name": "astrbot", "version": "1", "capabilities": {}})
        self.addAsyncCleanup(self.peer.close)

    async def set_active(self, active):
        await self.core._emotion_store.update(lambda config: config.__setitem__("chat_platform", {**default_chat_platform_settings(), "active": active}))

    @staticmethod
    def message(**fields):
        values = dict(
            platform="qq_official", platform_id="qq", chat_type=PRIVATE, session_id="s", conversation_id="qq:FriendMessage:me",
            sender_id="me", sender_name="我", group_id=None, text="你好", message_str="你好", ref="r",
        )
        values.update(fields)
        return IncomingMessage(**values)

    async def stream_all(self, method, msg):
        return [item async for item in self.peer.stream(method, {"message": message_to_dict(msg)})]

    async def test_with_astrbot_active_the_owner_is_accepted(self):
        self.assertTrue(await self.peer.call("accept_private", {"message": message_to_dict(self.message())}))

    async def test_with_napcat_active_the_connector_is_silent(self):
        await self.set_active("napcat")
        self.assertFalse(await self.peer.call("accept_private", {"message": message_to_dict(self.message())}))
        await self.peer.call("accept_group", {"message": message_to_dict(self.message(chat_type="group", group_id="g", conversation_id="qq:GroupMessage:g"))})
        self.assertEqual(self.core._schedule_targets, None)
        for method in ("handle_private", "handle_group", "handle_command"):
            self.assertEqual(await self.stream_all(method, self.message(command="recover")), [], msg=method)

    async def test_switching_back_restores_the_connector_at_once(self):
        await self.set_active("napcat")
        self.assertEqual(await self.stream_all("handle_command", self.message(command="recover")), [])
        await self.set_active("astrbot")
        batches = await self.stream_all("handle_command", self.message(command="recover"))
        self.assertEqual(len(batches), 1)


if __name__ == "__main__":
    unittest.main()
