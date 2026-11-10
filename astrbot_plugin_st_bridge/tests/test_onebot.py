"""R3: NapCat (OneBot v11) built into the core: the WebSocket server, events in, actions out, the platform switch."""

import asyncio
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import WSMsgType

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import chat_platform, network_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import BridgeRecoveryGate
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.onebot_inbound import (
    OneBotInbound,
    flatten_message,
    parse_command,
    strip_wake_prefix,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.onebot_link import (
    OneBotActionError,
    OneBotLink,
    OneBotOffline,
    build_messages,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.onebot_ws import OneBotServer, onebot_url
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import PlatformAPI, Segment
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_router import PlatformRouter
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import ScheduleTargets

SELF_ID = "10000"
OWNER = "20001"
STRANGER = "20002"
GROUP = "30001"
OTHER_GROUP = "30002"
REPLY = '[情绪:开心]☆‹「やった」›\n"太好了"'
TOKEN = "secret-token"


def text_segment(text):
    return {"type": "text", "data": {"text": text}}


def at_segment(qq):
    return {"type": "at", "data": {"qq": str(qq)}}


def private_event(text, user=OWNER, segments=None, **fields):
    event = {
        "post_type": "message", "message_type": "private", "sub_type": "friend", "self_id": int(SELF_ID),
        "user_id": int(user), "message_id": 1, "message": segments if segments is not None else [text_segment(text)],
        "raw_message": text, "sender": {"user_id": int(user), "nickname": "主人昵称"},
    }
    event.update(fields)
    return event


def group_event(segments, user=STRANGER, group=GROUP, card="群名片", **fields):
    event = {
        "post_type": "message", "message_type": "group", "sub_type": "normal", "self_id": int(SELF_ID),
        "group_id": int(group), "user_id": int(user), "message_id": 2, "message": segments,
        "raw_message": "", "sender": {"user_id": int(user), "nickname": "昵称", "card": card, "role": "member"},
    }
    event.update(fields)
    return event


class FakeWs:
    """The server side of a NapCat connection: records the actions and answers them like NapCat does."""

    def __init__(self, answer=True, retcode=0):
        self.closed = False
        self.actions = []
        self.answer = answer
        self.retcode = retcode
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def send_str(self, text):
        if self.closed:
            raise ConnectionError("closed")
        action = json.loads(text)
        self.actions.append(action)
        if self.answer:
            self.feed({"status": "ok" if self.retcode == 0 else "failed", "retcode": self.retcode,
                       "data": {"message_id": len(self.actions)}, "echo": action["echo"]})

    async def close(self, code=None):
        self.closed = True
        await self._incoming.put(None)

    def feed(self, data):
        self._incoming.put_nowait(json.dumps(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=WSMsgType.TEXT, data=item)


class FakeConnectorPlatform(PlatformAPI):
    def __init__(self):
        self.sent = []
        self.sent_to = []

    async def send(self, ref, segments):
        self.sent.append((ref, list(segments)))

    async def send_to(self, conversation_id, segments):
        self.sent_to.append((conversation_id, list(segments)))
        return True

    def wake_prefixes(self):
        return ["/"]


class FakeTts:
    def __init__(self, directory):
        self.settings = {"engine": "irodori", "irodori": {"output_mode": "native"}}
        self.common = {"prob": 100, "cooldown": 0, "text_limit": 0, "send_text_with_audio": True}
        self.temp_dir = Path(directory) / "temp_audio"

    async def synthesize(self, text, profile):
        return b"audio"


class OneBotCase(unittest.IsolatedAsyncioTestCase):
    """A core on the NapCat platform with a fake NapCat connection."""

    active = "napcat"
    whitelist = (GROUP,)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.link = OneBotLink(nickname=lambda: "琴音")
        self.connector = FakeConnectorPlatform()
        self.core = BridgeCore(PlatformRouter(self.connector, self.link, lambda: self.core.active_platform()), lambda: self.root)
        self.core._emotion_store = ConfigStore(self.root)
        await self.set_platform(self.active, whitelist=self.whitelist, owner=OWNER)
        self.manager = RequestStateManager(timeout_seconds=30)
        self.core._request_state_manager = self.manager
        self.core._recovery_gate = BridgeRecoveryGate()
        self.core._recovery_gate.confirm_recovery()
        self.core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)
        self.inbound = OneBotInbound(self.core, self.link)
        self.ws = FakeWs()
        self.serving = asyncio.create_task(self.link.serve(self.ws, SELF_ID))
        self.addAsyncCleanup(self.link.stop)
        await self.settle()

    async def set_platform(self, active, whitelist=(GROUP,), owner=OWNER):
        def update(config):
            config["chat_platform"] = {"active": active, "group_whitelist": list(whitelist), "napcat_owner": owner}

        await self.core._emotion_store.update(update)

    @staticmethod
    async def settle(turns=30):
        for _ in range(turns):
            await asyncio.sleep(0)

    async def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.005)

    async def next_request(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            request = self.manager.peek_pending_request()
            if request is not None:
                return request
            if time.monotonic() >= deadline:
                raise AssertionError("the core did not create a request")
            await asyncio.sleep(0.005)

    def answer(self, request, reply=REPLY):
        self.manager.claim_request(request.request_id, "attempt-" + request.request_id)
        self.manager.mark_generation_started(request.request_id)
        self.manager.confirm_generation_finished(request.request_id)
        self.manager.complete_request(request.request_id, reply)

    async def say(self, event):
        """Feed one event and let the core handle it; returns once the handler task ended."""
        self.ws.feed(event)
        await self.wait_for(lambda: True)
        await self.settle()

    async def say_and_answer(self, event, reply=REPLY):
        self.ws.feed(event)
        request = await self.next_request()
        self.answer(request, reply)
        count = len(self.ws.actions)
        await self.wait_for(lambda: len(self.ws.actions) > count)
        return request

    async def quiet(self):
        """No request is created and nothing is sent."""
        await asyncio.sleep(0.05)
        await self.settle()
        self.assertIsNone(self.manager.peek_pending_request())
        self.assertEqual(self.ws.actions, [])


class PureTests(unittest.TestCase):
    def test_settings_default_to_astrbot_and_validate(self):
        self.assertEqual(chat_platform.default_chat_platform_settings(), {"active": "astrbot", "group_whitelist": [], "napcat_owner": ""})
        self.assertEqual(chat_platform.chat_platform_settings({}), chat_platform.default_chat_platform_settings())
        ok = chat_platform.validate_chat_platform_settings({"active": "napcat", "group_whitelist": ["123", 456, "123"], "napcat_owner": " 789 "})
        self.assertEqual(ok, {"active": "napcat", "group_whitelist": ["123", "456"], "napcat_owner": "789"})
        for bad in (
            {"active": "qq"}, {"group_whitelist": "123"}, {"group_whitelist": ["abc"]}, {"group_whitelist": [""]},
            {"group_whitelist": ["0123"]}, {"napcat_owner": "x1"}, {"napcat_owner": "1" * 21},
            {"group_whitelist": [str(number) for number in range(1, 202)]}, "text",
        ):
            with self.assertRaises(EmotionConfigError, msg=repr(bad)):
                chat_platform.validate_chat_platform_settings(bad)
        # An unreadable section counts as "AstrBot".
        self.assertEqual(chat_platform.chat_platform_settings({"chat_platform": {"active": "nonsense"}})["active"], "astrbot")

    def test_conversation_ids_round_trip(self):
        self.assertEqual(chat_platform.parse_conversation(chat_platform.private_conversation("123")), ("private", "123"))
        self.assertEqual(chat_platform.parse_conversation(chat_platform.group_conversation("456")), ("group", "456"))
        for other in ("qq:FriendMessage:abc", "napcat:private:", "napcat:private:x", "napcat:other:1", None, 5):
            self.assertIsNone(chat_platform.parse_conversation(other))
        self.assertEqual(chat_platform.conversation_platform("napcat:group:1"), "napcat")
        self.assertEqual(chat_platform.conversation_platform("qq:GroupMessage:1"), "astrbot")

    def test_flatten_message_keeps_the_text_in_order_and_drops_the_bots_own_mention(self):
        segments = [at_segment(SELF_ID), text_segment(" 你好"), {"type": "image", "data": {"file": "x"}}, at_segment("99"), text_segment("，早"), {"type": "face", "data": {"id": "1"}}]
        self.assertEqual(flatten_message(segments, SELF_ID), ("你好，早", True))
        self.assertEqual(flatten_message([text_segment("a"), at_segment("all")], SELF_ID), ("a", False))
        self.assertEqual(flatten_message([{"type": "image", "data": {}}], SELF_ID), ("", False))
        self.assertEqual(flatten_message(["junk", {"type": "text", "data": None}], SELF_ID), ("", False))

    def test_wake_prefix_is_removed_longest_first(self):
        prefixes = ["/琴音", "琴音"]
        self.assertEqual(strip_wake_prefix("/琴音 你好", prefixes), ("你好", True))
        self.assertEqual(strip_wake_prefix("琴音，你好", prefixes), ("，你好", True))
        self.assertEqual(strip_wake_prefix("你好琴音", prefixes), ("你好琴音", False))
        self.assertEqual(strip_wake_prefix("  /琴音唱 歌", prefixes), ("唱 歌", True))
        self.assertEqual(strip_wake_prefix("你好", []), ("你好", False))

    def test_the_five_commands_with_and_without_a_prefix(self):
        prefixes = ["/琴音", "琴音"]
        self.assertEqual(parse_command("stbridge_ping", prefixes), ("ping", ""))
        self.assertEqual(parse_command("/琴音 好感度", prefixes), ("affinity", ""))
        self.assertEqual(parse_command("好感度设置 50", prefixes), ("affinity_set", "50"))
        self.assertEqual(parse_command("/琴音 好感度设置 50 多余", prefixes), ("affinity_set", "50"))
        self.assertEqual(parse_command("stbridge_recover", prefixes), ("recover", ""))
        self.assertEqual(parse_command("唱 春日影 男声", prefixes), ("sing", "春日影 男声"))
        self.assertEqual(parse_command("琴音 唱 春日影", prefixes), ("sing", "春日影"))
        for text in ("唱歌好听", "好感度设置50", "你好", "", "好感度们"):
            self.assertIsNone(parse_command(text, prefixes), msg=text)

    def test_messages_merge_text_and_images_and_keep_a_voice_alone(self):
        segments = [Segment.of_text("a"), Segment.image(Path("C:/x/y.png")), Segment.voice(Path("C:/x/v.wav")), Segment.of_text(""), Segment.of_text("b")]
        messages = build_messages(segments)
        self.assertEqual([[item["type"] for item in message] for message in messages], [["text", "image"], ["record"], ["text"]])
        self.assertTrue(messages[1][0]["data"]["file"].endswith("v.wav"))
        self.assertNotIn("\\", messages[0][1]["data"]["file"])
        self.assertEqual(build_messages([Segment.of_text("")]), [])
        with self.assertRaises(ValueError):
            build_messages([Segment("video")])

    def test_the_onebot_port_is_a_network_setting_and_reserved(self):
        self.assertEqual(network_config.default_network_settings()["onebot_port"], 11455)
        self.assertIn(11455, network_config.reserved_ports({}))
        self.assertEqual(network_config.network_settings({"network": {"st_port": 12000}})["onebot_port"], 11455)
        for name in ("st_port", "bridge_port", "console_port", "connector_port"):
            with self.assertRaises(EmotionConfigError):
                network_config.validate_network_settings({"onebot_port": network_config.default_network_settings()[name]})
        with self.assertRaises(EmotionConfigError):
            network_config.validate_network_settings({"onebot_port": 80})
        with self.assertRaises(EmotionConfigError):
            network_config.validate_network_settings({"onebot_port": 7000}, tts={"irodori": {"port": 7000}})
        with self.assertRaises(EmotionConfigError):
            network_config.validate_network_settings({"onebot_port": 7001}, cover={"port": 7001})
        self.assertEqual(network_config.validate_network_settings({"onebot_port": 12005})["onebot_port"], 12005)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.link = OneBotLink()
        self.port = free_port()
        self.server = OneBotServer(self.link, token=TOKEN, port=self.port)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    def connect(self, headers=None):
        base = {"Authorization": f"Bearer {TOKEN}", "X-Self-ID": SELF_ID, "x-client-role": "Universal"}
        base.update(headers or {})
        return self.session.ws_connect(onebot_url(self.port).replace("ws://", "http://"), headers={k: v for k, v in base.items() if v is not None})

    async def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.01)

    async def test_wrong_missing_or_empty_tokens_get_401(self):
        for headers in ({"Authorization": "Bearer wrong"}, {"Authorization": None}, {"Authorization": "Bearer "}, {"Authorization": TOKEN}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.connect(headers)
            self.assertEqual(caught.exception.status, 401, msg=repr(headers))
        self.assertFalse(self.link.connected)

    async def test_a_request_with_an_origin_header_is_refused(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.connect({"Origin": "http://evil.example"})
        self.assertEqual(caught.exception.status, 403)

    async def test_a_connection_with_the_token_is_served_and_reported(self):
        ws = await self.connect()
        await self.wait_for(lambda: self.link.connected)
        status = self.link.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["self_id"], SELF_ID)
        self.assertIsNone(status["heartbeat_age"])
        await ws.send_str(json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat", "self_id": int(SELF_ID), "status": {"online": True, "good": True}, "interval": 30000}))
        await self.wait_for(lambda: self.link.status()["heartbeat_age"] is not None)
        self.assertNotIn("token", json.dumps(self.link.status()))
        await ws.close()
        await self.wait_for(lambda: not self.link.connected)
        self.assertFalse(self.link.status()["connected"])

    async def test_the_self_id_comes_from_the_lifecycle_event_when_the_header_is_missing(self):
        ws = await self.connect({"X-Self-ID": None})
        await self.wait_for(lambda: self.link.connected)
        self.assertIsNone(self.link.status()["self_id"])
        await ws.send_str(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": 4242}))
        await self.wait_for(lambda: self.link.status()["self_id"] == "4242")
        await ws.close()

    async def test_a_new_connection_replaces_the_old_one(self):
        first = await self.connect()
        await self.wait_for(lambda: self.link.connected)
        second = await self.connect({"X-Self-ID": "555"})
        await self.wait_for(lambda: self.link.self_id == "555")
        message = await first.receive(timeout=3)
        self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED))
        self.assertTrue(self.link.connected)
        self.assertEqual(self.link.status()["self_id"], "555")
        await second.close()

    async def test_actions_are_paired_by_echo_and_a_failed_retcode_raises(self):
        ws = await self.connect()
        await self.wait_for(lambda: self.link.connected)

        async def napcat():
            frame = json.loads((await ws.receive()).data)
            await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 7}, "echo": frame["echo"]}))
            frame = json.loads((await ws.receive()).data)
            await ws.send_str(json.dumps({"status": "failed", "retcode": 1200, "data": None, "echo": frame["echo"]}))

        helper = asyncio.create_task(napcat())
        self.assertEqual(await self.link.call("send_private_msg", {"user_id": 1, "message": []}), {"message_id": 7})
        with self.assertRaises(OneBotActionError) as caught:
            await self.link.call("send_private_msg", {"user_id": 1, "message": []})
        self.assertEqual(caught.exception.retcode, 1200)
        await helper
        await ws.close()

    async def test_an_action_that_is_never_answered_times_out(self):
        ws = await self.connect()
        await self.wait_for(lambda: self.link.connected)
        with self.assertRaises(OneBotActionError) as caught:
            await self.link.call("send_private_msg", {}, timeout=0.1)
        self.assertEqual(caught.exception.retcode, "timeout")
        await ws.close()

    async def test_pending_actions_fail_at_once_when_the_connection_ends(self):
        ws = await self.connect()
        await self.wait_for(lambda: self.link.connected)
        pending = asyncio.create_task(self.link.call("send_private_msg", {}, timeout=30))
        await asyncio.sleep(0.05)
        await ws.close()
        with self.assertRaises(OneBotOffline):
            await asyncio.wait_for(pending, 3)
        with self.assertRaises(OneBotOffline):
            await self.link.call("send_private_msg", {})

    async def test_stop_closes_the_connection_quickly(self):
        ws = await self.connect()
        await self.wait_for(lambda: self.link.connected)
        started = time.monotonic()
        await self.server.stop()
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(ws.closed or (await ws.receive(timeout=3)).type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED))


class ActionMappingTests(OneBotCase):
    async def test_a_reply_goes_out_as_one_message_per_text_image_run_and_a_separate_voice(self):
        wav = self.root / "v.wav"
        wav.write_bytes(b"RIFF")
        png = self.root / "s.png"
        png.write_bytes(b"png")
        sent = await self.link.send_to(chat_platform.private_conversation(OWNER), [Segment.of_text("早"), Segment.image(png), Segment.voice(wav), Segment.of_text("晚")])
        self.assertTrue(sent)
        kinds = [(action["action"], [item["type"] for item in action["params"]["message"]]) for action in self.ws.actions]
        self.assertEqual(kinds, [("send_private_msg", ["text", "image"]), ("send_private_msg", ["record"]), ("send_private_msg", ["text"])])
        self.assertEqual(self.ws.actions[0]["params"]["user_id"], int(OWNER))
        record = self.ws.actions[1]["params"]["message"][0]["data"]["file"]
        self.assertTrue(Path(record).is_file())
        self.assertNotIn("\\", record)

    async def test_group_and_unknown_conversations(self):
        self.assertTrue(await self.link.send_to(chat_platform.group_conversation(GROUP), [Segment.of_text("hi")]))
        self.assertEqual((self.ws.actions[0]["action"], self.ws.actions[0]["params"]["group_id"]), ("send_group_msg", int(GROUP)))
        self.assertFalse(await self.link.send_to("qq:FriendMessage:x", [Segment.of_text("hi")]))
        with self.assertRaises(RuntimeError):
            await self.link.send("qq:FriendMessage:x", [Segment.of_text("hi")])

    async def test_the_router_sends_each_conversation_to_its_own_platform(self):
        router = self.core._platform
        await router.send_to("napcat:private:5", [Segment.of_text("n")])
        await router.send_to("qq:FriendMessage:5", [Segment.of_text("a")])
        await router.send("napcat:group:6", [Segment.of_text("n2")])
        await router.send("ref-from-astrbot", [Segment.of_text("a2")])
        self.assertEqual([action["action"] for action in self.ws.actions], ["send_private_msg", "send_group_msg"])
        self.assertEqual([item[0] for item in self.connector.sent_to], ["qq:FriendMessage:5"])
        self.assertEqual([item[0] for item in self.connector.sent], ["ref-from-astrbot"])
        self.assertFalse(await router.can_preupload_image("napcat:group:6"))
        self.assertEqual(router.wake_prefixes(), ["/琴音", "琴音"])

    async def test_sending_while_napcat_is_away_fails_with_offline(self):
        await self.link.stop()
        with self.assertRaises(OneBotOffline):
            await self.link.send_to(chat_platform.private_conversation(OWNER), [Segment.of_text("hi")])


class InboundTests(OneBotCase):
    async def test_the_owners_private_message_is_bridged_and_answered(self):
        request = await self.say_and_answer(private_event("你好"))
        self.assertEqual(request.content, "你好")
        (action,) = self.ws.actions
        self.assertEqual(action["action"], "send_private_msg")
        self.assertEqual(action["params"]["user_id"], int(OWNER))
        self.assertEqual(action["params"]["message"], [{"type": "text", "data": {"text": "太好了"}}])

    async def test_a_slash_nickname_in_front_of_private_text_is_dropped_but_a_bare_nickname_stays(self):
        request = await self.say_and_answer(private_event("/琴音 在吗"))
        self.assertEqual(request.content, "在吗")
        request = await self.say_and_answer(private_event("琴音，在吗"))
        self.assertEqual(request.content, "琴音，在吗")

    async def test_strangers_private_messages_get_no_answer_at_all(self):
        await self.say(private_event("你好", user=STRANGER))
        await self.say(private_event("好感度", user=STRANGER))
        await self.say(private_event("唱 春日影", user=STRANGER))
        await self.quiet()

    async def test_own_messages_notices_and_text_less_messages_are_ignored(self):
        await self.say(private_event("你好", user=SELF_ID))
        await self.say({"post_type": "notice", "notice_type": "friend_add", "user_id": int(OWNER)})
        await self.say({"post_type": "request", "request_type": "friend", "user_id": int(OWNER)})
        await self.say({"post_type": "message_sent", "message_type": "private", "user_id": int(OWNER), "message": [text_segment("x")]})
        await self.say(private_event("", segments=[{"type": "image", "data": {"file": "x"}}]))
        await self.quiet()

    async def test_a_message_in_string_format_is_refused_with_a_status_warning(self):
        self.assertFalse(self.link.status()["format_warning"])
        await self.say(private_event("你好", message="你好"))
        await self.quiet()
        self.assertTrue(self.link.status()["format_warning"])

    async def test_a_whitelisted_group_answers_to_an_at_and_labels_the_speaker(self):
        request = await self.say_and_answer(group_event([at_segment(SELF_ID), text_segment(" 大家好")]))
        self.assertEqual(request.content, "【群友】群名片（1）：大家好")
        (action,) = self.ws.actions
        self.assertEqual((action["action"], action["params"]["group_id"]), ("send_group_msg", int(GROUP)))
        self.assertEqual(action["params"]["message"], [{"type": "text", "data": {"text": "太好了"}}])

    async def test_a_nickname_prefix_wakes_the_group_and_is_removed(self):
        for text in ("/琴音 早上好", "琴音早上好"):
            request = await self.say_and_answer(group_event([text_segment(text)]))
            self.assertEqual(request.content, "【群友】群名片（1）：早上好")

    async def test_other_group_messages_do_not_wake_the_character_but_remember_the_group(self):
        self.core._schedule_targets = ScheduleTargets(self.root)
        await self.say(group_event([text_segment("今天天气不错")]))
        await self.say(group_event([text_segment("好感度")]))
        await self.quiet()
        self.assertTrue(self.core._schedule_targets.has_group(chat_platform_group_key(GROUP)))

    async def test_groups_off_the_whitelist_are_ignored_and_not_remembered(self):
        self.core._schedule_targets = ScheduleTargets(self.root)
        await self.say(group_event([at_segment(SELF_ID), text_segment("你好")], group=OTHER_GROUP))
        await self.quiet()
        self.assertFalse(self.core._schedule_targets.has_group(chat_platform_group_key(OTHER_GROUP)))
        self.assertEqual(self.core._schedule_targets.groups(), [])

    async def test_an_empty_whitelist_answers_in_no_group(self):
        await self.set_platform("napcat", whitelist=(), owner=OWNER)
        await self.say(group_event([at_segment(SELF_ID), text_segment("你好")]))
        await self.quiet()

    async def test_a_group_message_with_only_the_mention_is_ignored(self):
        await self.say(group_event([at_segment(SELF_ID)]))
        await self.quiet()

    async def test_the_voice_goes_out_as_its_own_message_after_the_text(self):
        self.core._tts = FakeTts(self.root)
        await self.say_and_answer(private_event("你好"))
        await self.wait_for(lambda: len(self.ws.actions) >= 2)
        self.assertEqual([[item["type"] for item in action["params"]["message"]] for action in self.ws.actions], [["text"], ["record"]])
        self.assertTrue(Path(self.ws.actions[1]["params"]["message"][0]["data"]["file"]).is_file())

    async def test_a_failed_send_does_not_stop_the_rest(self):
        self.ws.retcode = 100
        await self.say_and_answer(private_event("你好"))
        await self.settle()
        self.assertEqual(len(self.ws.actions), 1)


def chat_platform_group_key(group):
    from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import group_key_for

    return group_key_for(chat_platform.group_conversation(group))


class CommandTests(OneBotCase):
    async def first_text(self, event):
        count = len(self.ws.actions)
        self.ws.feed(event)
        await self.wait_for(lambda: len(self.ws.actions) > count)
        return self.ws.actions[count]["params"]["message"][0]["data"]["text"]

    async def test_ping_and_recover_in_private_chat_with_and_without_a_prefix(self):
        self.assertIn("NapCat", await self.first_text(private_event("stbridge_ping")))
        self.assertIn("人工恢复确认已记录", await self.first_text(private_event("/琴音 stbridge_recover")))

    async def test_the_affinity_command_reports_the_value_in_private_and_group_chat(self):
        self.assertIn("好感度", await self.first_text(private_event("好感度")))
        self.assertIn("好感度", await self.first_text(group_event([at_segment(SELF_ID), text_segment("好感度")])))
        self.assertIn("好感度", await self.first_text(group_event([text_segment("/琴音 好感度")])))

    async def test_affinity_set_changes_only_the_owners_value(self):
        self.core._affinity_store = None
        self.assertIn("未执行", await self.first_text(private_event("好感度设置 50")))
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore

        self.core._affinity_store = AffinityStore(self.root)
        self.assertIn("设为 50", await self.first_text(private_event("好感度设置 50")))
        self.assertEqual(self.core._affinity_store.get("napcat:" + OWNER, 0, 0, 100), 50)

    async def test_sing_without_a_name_answers_the_usage_line_and_is_silent_for_strangers(self):
        self.assertIn("唱", await self.first_text(private_event("唱")))
        await self.say(private_event("唱", user=STRANGER))
        self.assertEqual(len(self.ws.actions), 1)

    async def test_a_command_in_a_group_needs_the_wake_up(self):
        await self.say(group_event([text_segment("好感度")]))
        await self.quiet()


class SwitchTests(OneBotCase):
    async def test_with_astrbot_active_every_napcat_message_is_ignored(self):
        await self.set_platform("astrbot", owner=OWNER)
        await self.say(private_event("你好"))
        await self.say(private_event("好感度"))
        await self.say(group_event([at_segment(SELF_ID), text_segment("你好")]))
        await self.quiet()

    async def test_the_switch_is_immediate_in_both_directions(self):
        await self.set_platform("astrbot")
        await self.say(private_event("stbridge_ping"))
        self.assertEqual(self.ws.actions, [])
        await self.set_platform("napcat")
        self.ws.feed(private_event("stbridge_ping"))
        await self.wait_for(lambda: len(self.ws.actions) == 1)
        await self.set_platform("astrbot")
        await self.say(private_event("stbridge_ping"))
        self.assertEqual(len(self.ws.actions), 1)

    async def test_the_owner_follows_the_active_platform(self):
        self.assertEqual(self.core._operator(), ("napcat", OWNER))
        self.core._operator_seed = ("qq-official", "owner-1")
        await self.set_platform("astrbot")
        self.assertEqual(self.core._operator(), ("qq-official", "owner-1"))
        self.assertEqual(self.core.connector_cache()["operator"], {"platform_id": "qq-official", "sender_id": "owner-1"})
        await self.set_platform("napcat", owner="")
        self.assertEqual(self.core._operator(), ("", ""))
        self.assertFalse(self.core._is_operator_private_message(SimpleNamespace(chat_type="private", platform_id="napcat", sender_id=OWNER)))

    async def test_an_old_config_without_the_section_means_astrbot(self):
        store = ConfigStore(self.root)
        store.config.pop("chat_platform", None)
        self.core._emotion_store = store
        self.assertEqual(self.core.active_platform(), "astrbot")
        self.assertEqual(self.core.chat_platform_settings(), chat_platform.default_chat_platform_settings())

    async def test_scheduled_messages_only_go_to_the_active_platform(self):
        self.core._schedule_targets = ScheduleTargets(self.root)
        self.core._schedule_targets.record_owner("qq-official:FriendMessage:abc")
        plan = {"id": "p1", "name": "早安", "prompt": "说早安", "fallback_texts": [], "target": {"type": "private"}}
        out = await self.core._run_proactive(plan, forced=True)
        self.assertEqual(out["result"], "skip:OtherPlatform")
        self.assertEqual((self.ws.actions, self.connector.sent_to), ([], []))
        # Back on AstrBot, a NapCat session is the one that is skipped.
        await self.set_platform("astrbot")
        self.core._schedule_targets.record_owner(chat_platform.private_conversation(OWNER))
        out = await self.core._run_proactive(plan, forced=True)
        self.assertEqual(out["result"], "skip:OtherPlatform")
        self.assertEqual((self.ws.actions, self.connector.sent_to), ([], []))

    async def test_a_scheduled_message_reaches_the_napcat_conversation(self):
        self.core._schedule_targets = ScheduleTargets(self.root)
        self.core._schedule_targets.record_owner(chat_platform.private_conversation(OWNER))
        plan = {"id": "p1", "name": "早安", "prompt": "说早安", "fallback_texts": [], "target": {"type": "private"}}
        running = asyncio.create_task(self.core._run_proactive(plan, forced=True))
        self.answer(await self.next_request())
        out = await running
        self.assertEqual(out["result"], "sent")
        self.assertEqual(self.ws.actions[0]["params"]["user_id"], int(OWNER))
        self.assertEqual(self.ws.actions[0]["params"]["message"][0]["data"]["text"], "太好了")


if __name__ == "__main__":
    unittest.main()
