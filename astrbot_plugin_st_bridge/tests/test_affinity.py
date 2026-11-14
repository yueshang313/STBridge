import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector
from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import emotion_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import (
    AffinityStore,
    group_key,
    strip_update_blocks,
    user_hash,
    user_key,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    stored_affinity_defaults,
    ConfigStore,
    EmotionConfigError,
    affinity_settings,
    affinity_tier_name,
    affinity_track,
    default_affinity_settings,
    validate_affinity_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from test_plugin_lifecycle import (
    _bridge_event,
    _collect,
    _complete_bridge_reply,
    _group_bridge_event,
    _load_plugin_module,
    _ready_plugin,
)


CHARACTER = "藤田琴音"
ANALYSIS_WITH_QUOTES = '<Analysis>网友说了"很可爱"，她很开心 "真的"</Analysis>'


def update_block(*commands, analysis=ANALYSIS_WITH_QUOTES):
    return "<UpdateVariable>" + analysis + "\n" + "\n".join(commands) + "\n</UpdateVariable>"


class AffinitySettingsTests(unittest.TestCase):
    def test_defaults_and_tier_names(self):
        settings = default_affinity_settings()
        self.assertEqual(
            (settings["character"], settings["display_name"], settings["initial_value"], settings["max_delta"]),
            (CHARACTER, "琴音", 0, 5),
        )
        self.assertEqual(validate_affinity_settings(settings), settings)
        for value, name in ((0, "陌生人"), (10, "陌生人"), (11, "有点用的制作人"), (45, "避风港"),
                            (61, "恋慕"), (81, "伴侣"), (100, "伴侣")):
            self.assertEqual(affinity_tier_name(settings, value), name)

    def test_invalid_settings_fail_closed(self):
        cases = (
            {"initial_value": 101},
            {"initial_value": -1},
            {"max_delta": True},
            {"max_delta": 2.5},
            {"character": "a/b"},
            {"display_name": ""},
            {"tiers": []},
            {"tiers": [{"min": 0, "max": 50, "name": "a"}, {"min": 50, "max": 100, "name": "b"}]},
            {"tiers": [{"min": 20, "max": 10, "name": "a"}]},
            {"tiers": [{"min": 0, "max": 100, "name": ""}]},
            {"tiers": [{"min": 0, "max": 100}]},
            [],
        )
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(EmotionConfigError):
                    validate_affinity_settings(settings)

    def test_tiers_are_sorted(self):
        settings = validate_affinity_settings({"tiers": [
            {"min": 51, "max": 100, "name": "高"}, {"min": 0, "max": 50, "name": "低"},
        ]})
        self.assertEqual([tier["name"] for tier in settings["tiers"]], ["低", "高"])

    def test_legacy_config_gets_defaults_and_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {"version": 1, "default_character": "", "default_emotion": "neutral", "models": {}, "characters": {}}
            (root / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
            store = ConfigStore(root)
            self.assertEqual(store.config["affinity"], stored_affinity_defaults())
            self.assertEqual(affinity_settings(store.config)["max_delta"], 5)
            self.assertEqual(affinity_settings({})["character"], CHARACTER)

            broken = dict(legacy, affinity={"max_delta": 500})
            (root / "config.json").write_text(json.dumps(broken), encoding="utf-8")
            store = ConfigStore(root)  # 101e: a config that fails its checks falls back to the defaults
            self.assertEqual(store.recovery["used"], "default")
            self.assertEqual(store.config["affinity"], stored_affinity_defaults())
            self.assertEqual((root / store.recovery["broken"][0]).read_text(encoding="utf-8"), json.dumps(broken))


class UpdateBlockParsingTests(unittest.TestCase):
    def parse(self, text):
        return strip_update_blocks(text, CHARACTER)

    def test_add_variants_are_summed(self):
        cleaned, update = self.parse(
            '☆‹やった›"太好了"\n' + update_block(
                "_.add('藤田琴音.好感度', 3); //夸她",
                '_.add("藤田琴音.好感度[0]", +2.4);',
                "_.add( '藤田琴音.好感度' , -1.5 );",
                "_.add('stat_data.藤田琴音.好感度', 1);",
                "_.add('月村手毬.好感度', 50);",
            )
        )
        self.assertEqual(cleaned.strip(), '☆‹やった›"太好了"')
        # 3 + 2 (2.4 rounds to 2) - 2 (-1.5 rounds half away from zero) + 1
        self.assertEqual(update.resolve(40), 4)

    def test_set_forms_use_new_minus_current(self):
        _, update = self.parse(update_block("_.set('藤田琴音.好感度', 40, 47);"))
        self.assertEqual(update.resolve(40), 7)
        _, update = self.parse(update_block('_.set("藤田琴音.好感度[0]", "30");'))
        self.assertEqual(update.resolve(40), -10)
        _, update = self.parse(update_block("_.add('藤田琴音.好感度', 2);", "_.set('藤田琴音.好感度', 10);",
                                            "_.add('藤田琴音.好感度', 1);"))
        self.assertEqual(update.resolve(40), -29)

    def test_other_characters_and_malformed_commands_are_ignored(self):
        for commands in (
            ("_.add('月村手毬.好感度', 5);",),
            ("_.set('藤田琴音.好感度', [5, '描述']);",),
            ("_.add('藤田琴音.好感度', 'abc');",),
            ("_.add('藤田琴音.好感度', 1, 2);",),
            ("_.add('藤田琴音.信任度', 5);",),
        ):
            with self.subTest(commands=commands):
                cleaned, update = self.parse("正文" + update_block(*commands))
                self.assertEqual(cleaned, "正文")
                self.assertIsNone(update)

    def test_case_insensitive_tags_unclosed_blocks_and_placeholders(self):
        cleaned, update = self.parse(
            '前<updatevariable>_.add("藤田琴音.好感度", 2)</UPDATEVARIABLE>中<StatusPlaceHolderImpl/>'
            '<statusplaceholderimpl />后<UpdateVariable><Analysis>"未闭合"</Analysis>_.add("藤田琴音.好感度", 1)'
        )
        self.assertEqual(cleaned, "前中后")
        self.assertEqual(update.resolve(0), 3)

    def test_text_without_blocks_is_untouched(self):
        text = '☆‹日本語›"中文"'
        self.assertEqual(self.parse(text), (text, None))


class AffinityStoreTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-affinity-")
        self.root = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def test_new_users_start_from_the_initial_value_and_changes_are_clamped(self):
        store = AffinityStore(self.root)
        key = user_key("qq", "openid-a")
        self.assertEqual(store.get(key, 30), 30)
        change = store.apply_delta(key, 9, max_delta=5, initial_value=30, nickname="小明")
        self.assertEqual((change.before, change.applied, change.after), (30, 5, 35))
        change = store.apply_delta(key, -12, max_delta=5, initial_value=30)
        self.assertEqual((change.before, change.applied, change.after), (35, -5, 30))
        store.set_value(key, 98)
        self.assertEqual(store.apply_delta(key, 5, max_delta=5, initial_value=0).after, 100)
        store.set_value(key, 2)
        change = store.apply_delta(key, -5, max_delta=5, initial_value=0)
        self.assertEqual((change.applied, change.after), (-2, 0))

        reloaded = AffinityStore(self.root)
        self.assertEqual(reloaded.get(key, 30), 0)
        self.assertEqual(reloaded.data["users"][key]["nickname"], "小明")
        self.assertTrue(reloaded.data["users"][key]["updated_at"])

    def test_users_are_listed_by_hash_and_editable_by_hash(self):
        store = AffinityStore(self.root)
        key = user_key("qq", "secret-openid")
        store.set_value(key, 40, nickname="name\nwith\x00control")
        listed, = store.users()
        self.assertEqual(listed["id"], user_hash(key))
        self.assertEqual(listed["platform"], "qq")
        self.assertEqual(listed["nickname"], "name with control")
        self.assertNotIn("secret-openid", json.dumps(listed))
        store.set_value_by_hash(user_hash(key), 77)
        self.assertEqual(store.get(key, 0), 77)
        with self.assertRaises(EmotionConfigError) as caught:
            store.set_value_by_hash("deadbeef", 1)
        self.assertEqual(caught.exception.status, 404)
        for value in (101, -1, True, 5.0):
            with self.assertRaises(EmotionConfigError):
                store.set_value(key, value)

    def test_corrupt_file_is_kept_under_a_new_name_and_never_overwritten(self):
        for content in (b"{ broken", b'{"version": 4, "users": {}}', b'{"version": 1, "users": {"k": {"value": "x"}}}'):
            with self.subTest(content=content):
                (self.root / "affinity.json").write_bytes(content)
                store = AffinityStore(self.root)
                self.assertEqual(store.recovery["used"], "empty")
                self.assertEqual(store.data["users"], {})
                self.assertEqual((self.root / store.recovery["broken"][0]).read_bytes(), content)
                (self.root / store.recovery["broken"][0]).unlink()
                (self.root / "affinity.json").unlink()

    def test_writes_retry_transient_permission_errors(self):
        store = AffinityStore(self.root)
        import os
        real_replace = os.replace
        attempts = []

        def flaky(source, target):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("locked")
            return real_replace(source, target)

        with patch.object(emotion_config.os, "replace", flaky), patch.object(emotion_config.time, "sleep", lambda _: None):
            store.set_value(user_key("qq", "a"), 10)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(AffinityStore(self.root).get(user_key("qq", "a"), 0), 10)


class ClaimCarriesAffinityTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_response_includes_affinity_only_when_present(self):
        manager = RequestStateManager(timeout_seconds=10)
        gate = BridgeRecoveryGate()
        gate.confirm_recovery()
        app = create_app(manager, HttpProtocolConfig(allowed_origin="http://localhost:11451", listen_port=11452),
                         recovery_gate=gate)
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        try:
            headers = {"Origin": "http://localhost:11451"}
            writes = [{"path": [CHARACTER, "好感度"], "value": 45}, {"path": ["群友好感度"], "value": None}]
            with_affinity = manager.create_request("hi", affinity_writes=writes)
            response = await client.post(f"/v1/requests/{with_affinity.request_id}/claim",
                                         json={"claim_attempt_id": "a1"}, headers=headers)
            body = await response.json()
            self.assertEqual(body["affinity_writes"], writes)
            self.assertNotIn("affinity", body)
            manager.confirm_generation_not_started(with_affinity.request_id)
            manager.fail_request(with_affinity.request_id, "extension_error")

            plain = manager.create_request("hi again")
            response = await client.post(f"/v1/requests/{plain.request_id}/claim",
                                         json={"claim_attempt_id": "a2"}, headers=headers)
            self.assertNotIn("affinity_writes", await response.json())
        finally:
            await client.close()


class AffinityWritesValidationTests(unittest.TestCase):
    def create(self, writes):
        return RequestStateManager(timeout_seconds=10).create_request("hi", affinity_writes=writes)

    def test_valid_writes_are_copied_and_null_is_allowed(self):
        writes = [{"path": [CHARACTER, "好感度"], "value": -100}, {"path": ["群友好感度"], "value": None}]
        request = self.create(writes)
        self.assertEqual(request.affinity_writes, tuple(writes))
        writes[0]["path"].append("x")
        self.assertEqual(request.affinity_writes[0]["path"], [CHARACTER, "好感度"])
        self.assertIsNone(self.create(None).affinity_writes)
        self.assertEqual(self.create([]).affinity_writes, ())

    def test_invalid_writes_are_rejected(self):
        ok = {"path": ["群友好感度"], "value": 1}
        cases = (
            "text",
            {"path": ["a"], "value": 1},
            [ok] * 5,
            ["not a dict"],
            [{"path": ["a"]}],
            [{"path": ["a"], "value": 1, "extra": 1}],
            [{"path": [], "value": 1}],
            [{"path": ["a", "b", "c", "d"], "value": 1}],
            [{"path": "群友好感度", "value": 1}],
            [{"path": ["a.b"], "value": 1}],
            [{"path": ["a[0]"], "value": 1}],
            [{"path": ["__proto__"], "value": 1}],
            [{"path": ["constructor", "x"], "value": 1}],
            [{"path": [""], "value": 1}],
            [{"path": [1], "value": 1}],
            [{"path": ["a"], "value": 101}],
            [{"path": ["a"], "value": -101}],
            [{"path": ["a"], "value": 1.5}],
            [{"path": ["a"], "value": "5"}],
            [{"path": ["a"], "value": True}],
        )
        for writes in cases:
            with self.subTest(writes=writes):
                with self.assertRaises(ValueError):
                    self.create(writes)


class PluginAffinityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.logger = self.module.test_logger
        for messages in (self.logger.warnings, self.logger.errors, self.logger.infos):
            messages.clear()
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-affinity-plugin-")
        self.root = Path(self._directory.name)
        self.plugin = _ready_plugin(self.module)
        self.plugin._core._affinity_store = AffinityStore(
            self.root, owner_key=user_key("qq-platform", "qq-user")
        )

    async def asyncTearDown(self):
        self._directory.cleanup()

    def value_of(self, sender_id, platform_id="qq-platform", group=False):
        low, high = (-50, 100) if group else (0, 100)
        key = user_key(platform_id, sender_id)
        return self.plugin._core._affinity_store.get(group_key(key) if group else key, 0, low, high)

    async def collect_set(self, event, value):
        return [item async for item in self.plugin.stbridge_affinity_set(event, value)]

    async def private_reply(self, reply, text="hello"):
        event = _bridge_event(self.module, text, sender_name="操作者昵称")
        results = await _complete_bridge_reply(self.plugin, event, reply)
        return event, results

    async def test_private_request_carries_affinity_and_quotes_in_analysis_do_not_break_delivery(self):
        self.plugin._core._affinity_store.set_value(user_key("qq-platform", "qq-user"), 42)
        processing = asyncio.create_task(_collect(
            self.plugin.stbridge_private_message, _bridge_event(self.module, "hello", sender_name="操作者昵称"),
        ))
        for _ in range(100):
            request = self.plugin._core._request_state_manager.peek_pending_request()
            if request:
                break
            await asyncio.sleep(0)
        self.assertEqual(request.content, "hello")
        self.assertEqual(
            request.affinity_writes,
            ({"path": [CHARACTER, "好感度"], "value": 42}, {"path": ["群友好感度"], "value": None}, {"path": ["点歌许可"], "value": 0}),
        )
        manager = self.plugin._core._request_state_manager
        manager.claim_request(request.request_id, "attempt")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(
            request.request_id,
            '☆‹やった›"太好了"<StatusPlaceHolderImpl/>\n' + update_block("_.add('藤田琴音.好感度', 7); //开心"),
        )
        results = await processing
        self.assertEqual([result.text for result in results], ["太好了"])
        self.assertEqual(self.value_of("qq-user"), 47)
        affinity_logs = [line for line in self.logger.infos if "Affinity" in line]
        self.assertEqual(
            affinity_logs,
            [f"[STBridge] Affinity user={user_hash(user_key('qq-platform', 'qq-user'))} "
             "before=42 parsed=+7 applied=+5 after=47 track=owner"],
        )
        logs = "".join(self.logger.infos + self.logger.warnings)
        for secret in ("操作者昵称", "qq-user", "太好了", "开心", "やった"):
            self.assertNotIn(secret, logs)
        self.assertEqual(self.plugin._core._affinity_store.data["users"][user_key("qq-platform", "qq-user")]["nickname"],
                         "操作者昵称")

    async def test_reply_without_commands_keeps_the_value(self):
        await self.private_reply('☆‹日本語›"中文"')
        self.assertEqual(self.value_of("qq-user"), 0)
        self.assertNotIn(user_key("qq-platform", "qq-user"), self.plugin._core._affinity_store.data["users"])
        self.assertEqual(
            [line for line in self.logger.infos if "Affinity" in line],
            [f"[STBridge] Affinity user={user_hash(user_key('qq-platform', 'qq-user'))} "
             "before=0 parsed=none applied=+0 after=0 track=owner"],
        )

    async def test_invalid_reply_or_failure_never_changes_the_value(self):
        event, results = await self.private_reply('没有双语格式' + update_block("_.add('藤田琴音.好感度', 5);"))
        self.assertEqual([result.text for result in results], [self.module.INVALID_REPLY_TEXT])
        self.assertEqual(self.value_of("qq-user"), 0)

        processing = asyncio.create_task(_collect(self.plugin.stbridge_private_message,
                                                  _bridge_event(self.module, "will fail")))
        for _ in range(100):
            request = self.plugin._core._request_state_manager.peek_pending_request()
            if request:
                break
            await asyncio.sleep(0)
        self.plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
        await processing
        self.assertEqual(self.value_of("qq-user"), 0)
        self.assertEqual([line for line in self.logger.infos if "Affinity" in line], [])

    async def test_group_prefix_and_independent_values(self):
        self.plugin._core._affinity_store.set_value(group_key(user_key("qq-platform", "member-a")), 45)
        for sender_id, name, delta, expected_prefix in (
            ("member-a", "小明", 3, "【群友】小明（1）[好感度:45]："),
            ("member-b", "小红", -2, "【群友】小红（1）[好感度:0]："),
        ):
            event = _group_bridge_event(self.module, "你好", sender_id=sender_id, sender_name=name)
            with patch.dict(sys.modules, self.module.TestBotpyModules):
                processing = asyncio.create_task(_collect(self.plugin.stbridge_group_message, event))
                for _ in range(100):
                    request = self.plugin._core._request_state_manager.peek_pending_request()
                    if request:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(request.content, f"{expected_prefix}你好")
                manager = self.plugin._core._request_state_manager
                manager.claim_request(request.request_id, f"attempt-{sender_id}")
                manager.mark_generation_started(request.request_id)
                manager.confirm_generation_finished(request.request_id)
                manager.complete_request(
                    request.request_id,
                    '☆‹日本語›"中文"' + update_block(f"_.add('群友好感度', {delta});"),
                )
                await processing
        self.assertEqual(self.value_of("member-a", group=True), 48)
        # group members may go below zero (range -50..100)
        self.assertEqual(self.value_of("member-b", group=True), -2)

    async def test_without_affinity_store_group_prefix_and_request_are_unchanged(self):
        self.plugin._core._affinity_store = None
        event = _group_bridge_event(self.module, "你好", sender_id="member-c", sender_name="小刚")
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            processing = asyncio.create_task(_collect(self.plugin.stbridge_group_message, event))
            for _ in range(100):
                request = self.plugin._core._request_state_manager.peek_pending_request()
                if request:
                    break
                await asyncio.sleep(0)
            self.assertEqual((request.content, request.affinity_writes), ("【群友】小刚（1）：你好", None))
            self.plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
            await processing

    async def test_query_command_in_private_and_group(self):
        self.plugin._core._affinity_store.set_value(user_key("qq-platform", "qq-user"), 45)
        event = _bridge_event(self.module, "/好感度")
        result, = await _collect(self.plugin.stbridge_affinity, event)
        self.assertEqual(result.text, "琴音对你的好感度：45（避风港）")
        self.assertTrue(event.call_llm)

        group_event = _group_bridge_event(self.module, "/好感度", sender_id="member-z")
        result, = await _collect(self.plugin.stbridge_affinity, group_event)
        self.assertEqual(result.text, "琴音对你的好感度：0（普通网友）")

        self.plugin._core._affinity_store = None
        result, = await _collect(self.plugin.stbridge_affinity, _bridge_event(self.module, "/好感度"))
        self.assertEqual(result.text, "好感度功能当前不可用。")

    async def test_set_command_is_operator_private_only(self):
        result, = await self.collect_set(_bridge_event(self.module, "/好感度设置 66"), "66")
        self.assertEqual(result.text, "已将琴音对你的好感度设为 66（恋慕）。")
        self.assertEqual(self.value_of("qq-user"), 66)

        for event, value in (
            (_bridge_event(self.module, "x", sender_id="someone-else"), "10"),
            (_group_bridge_event(self.module, "x", sender_id="qq-user"), "10"),
        ):
            result, = await self.collect_set(event, value)
            self.assertEqual(result.text, "好感度设置命令未执行。")
        self.assertEqual(self.value_of("qq-user"), 66)
        self.assertEqual(self.value_of("someone-else"), 0)

        for value in ("", "101", "-1", "abc", "5.5"):
            result, = await self.collect_set(_bridge_event(self.module, "x"), value)
            self.assertEqual(result.text, "请提供 0 到 100 之间的整数。")
        self.assertEqual(self.value_of("qq-user"), 66)

    async def test_affinity_commands_are_registered_and_not_forwarded_to_st(self):
        self.assertEqual(self.module.STBridgePlugin.stbridge_affinity.command_name, "好感度")
        self.assertEqual(self.module.STBridgePlugin.stbridge_affinity_set.command_name, "好感度设置")
        for handler_name in ("main_stbridge_affinity", "main_stbridge_affinity_set"):
            private = _bridge_event(self.module, "/好感度", extras={"handlers_parsed_params": {handler_name: {}}})
            self.assertEqual(await _collect(self.plugin.stbridge_private_message, private), [])
            group = _group_bridge_event(self.module, "/好感度")
            group.extras["handlers_parsed_params"] = {handler_name: {}}
            with patch.dict(sys.modules, self.module.TestBotpyModules):
                self.assertEqual(await _collect(self.plugin.stbridge_group_message, group), [])
        self.assertIsNone(self.plugin._core._request_state_manager.peek_pending_request())

    async def test_store_errors_never_block_the_reply(self):
        with patch.object(self.plugin._core._affinity_store, "apply_delta", side_effect=OSError("disk full")):
            _, results = await self.private_reply('☆‹日本語›"中文"' + update_block("_.add('藤田琴音.好感度', 1);"))
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertIn("[STBridge] Affinity update failed (OSError)", self.logger.warnings)


    async def group_turn(self, sender_id, reply, text="你好", name="小明"):
        """One complete group turn; every turn claims with its own attempt id."""
        event = _group_bridge_event(self.module, text, sender_id=sender_id, sender_name=name)
        manager = self.plugin._core._request_state_manager
        self._turns = getattr(self, "_turns", 0) + 1
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            processing = asyncio.create_task(_collect(self.plugin.stbridge_group_message, event))
            for _ in range(100):
                request = manager.peek_pending_request()
                if request:
                    break
                await asyncio.sleep(0)
            manager.claim_request(request.request_id, f"turn-{self._turns}")
            manager.mark_generation_started(request.request_id)
            manager.confirm_generation_finished(request.request_id)
            manager.complete_request(request.request_id, reply)
            return event, await processing

    async def test_writes_list_for_private_and_group_turns(self):
        self.plugin._core._affinity_store.set_value(user_key("qq-platform", "qq-user"), 61)
        self.plugin._core._affinity_store.set_value(group_key(user_key("qq-platform", "member-a")), 12, low=-50, high=100)
        owner_path, group_path = [CHARACTER, "好感度"], ["群友好感度"]

        processing = asyncio.create_task(_collect(self.plugin.stbridge_private_message,
                                                  _bridge_event(self.module, "hello")))
        for _ in range(100):
            request = self.plugin._core._request_state_manager.peek_pending_request()
            if request:
                break
            await asyncio.sleep(0)
        # private: the owner's value, and null for the group variable
        self.assertEqual(request.affinity_writes, (
            {"path": owner_path, "value": 61}, {"path": group_path, "value": None}, {"path": ["点歌许可"], "value": 0},
        ))
        self.plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
        await processing

        event = _group_bridge_event(self.module, "你好", sender_id="member-a", sender_name="小明")
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            processing = asyncio.create_task(_collect(self.plugin.stbridge_group_message, event))
            for _ in range(100):
                request = self.plugin._core._request_state_manager.peek_pending_request()
                if request:
                    break
                await asyncio.sleep(0)
            # group: the owner's value as background, plus the member's own value
            self.assertEqual(request.affinity_writes, (
                {"path": owner_path, "value": 61}, {"path": group_path, "value": 12}, {"path": ["点歌许可"], "value": 0},
            ))
            self.plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
            await processing

    async def test_group_turn_ignores_owner_commands_and_applies_member_commands(self):
        self.plugin._core._affinity_store.set_value(user_key("qq-platform", "qq-user"), 50)
        _, results = await self.group_turn(
            "member-a", '☆‹日本語›"中文"' + update_block(
                "_.add('藤田琴音.好感度', 5);", "_.add('群友好感度', 3);",
            ),
        )
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertEqual(self.value_of("member-a", group=True), 3)
        self.assertEqual(self.value_of("qq-user"), 50)
        affinity_logs = [line for line in self.logger.infos if "Affinity" in line]
        self.assertEqual(
            affinity_logs,
            [f"[STBridge] Affinity user={user_hash(group_key(user_key('qq-platform', 'member-a')))} "
             "before=0 parsed=+3 applied=+3 after=3 track=group"],
        )

    async def test_private_turn_ignores_group_commands(self):
        _, results = await self.private_reply('☆‹日本語›"中文"' + update_block(
            "_.add('群友好感度', 4);", "_.add('藤田琴音.好感度', 2);",
        ))
        self.assertEqual([result.text for result in results], ["中文"])
        self.assertEqual(self.value_of("qq-user"), 2)
        self.assertNotIn(user_key("qq-platform", "群友好感度"), self.plugin._core._affinity_store.data["users"])

    async def test_group_variable_path_spellings(self):
        for index, path in enumerate(("群友好感度", "stat_data.群友好感度", "群友好感度[0]", "stat_data.群友好感度[0]")):
            sender = f"member-{index}"
            await self.group_turn(sender, '☆‹日本語›"中文"' + update_block(f"_.add('{path}', 2);"))
            self.assertEqual(self.value_of(sender, group=True), 2, path)

    async def test_group_member_is_clamped_to_minus_fifty_and_max_delta(self):
        store = self.plugin._core._affinity_store
        store.set_value(group_key(user_key("qq-platform", "member-a")), -49, low=-50, high=100)
        await self.group_turn("member-a", '☆‹日本語›"中文"' + update_block("_.add('群友好感度', -5);"))
        self.assertEqual(self.value_of("member-a", group=True), -50)
        await self.group_turn("member-a", '☆‹日本語›"中文"' + update_block("_.add('群友好感度', -3);"))
        self.assertEqual(self.value_of("member-a", group=True), -50)
        store.set_value(group_key(user_key("qq-platform", "member-a")), 99, low=-50, high=100)
        await self.group_turn("member-a", '☆‹日本語›"中文"' + update_block("_.set('群友好感度', 99, 120);"))
        self.assertEqual(self.value_of("member-a", group=True), 100)

    async def test_group_query_command_shows_group_tier(self):
        self.plugin._core._affinity_store.set_value(group_key(user_key("qq-platform", "member-z")), -30, low=-50, high=100)
        group_event = _group_bridge_event(self.module, "/好感度", sender_id="member-z")
        result, = await _collect(self.plugin.stbridge_affinity, group_event)
        self.assertEqual(result.text, "琴音对你的好感度：-30（黑粉）")


    # The bot gives the owner the same openid in private and group chat ("qq-user" below).

    def use_owner_value(self, value=89):
        self.owner_key = user_key("qq-platform", "qq-user")
        self.plugin._core._affinity_store.set_value(self.owner_key, value)

    async def test_group_turn_reads_and_writes_the_group_key_only(self):
        self.use_owner_value()
        _, results = await self.group_turn(
            "qq-user", '☆‹日本語›"中文"' + update_block("_.add('群友好感度', 1);", "_.add('藤田琴音.好感度', 5);"),
            name="制作人",
        )
        self.assertEqual([result.text for result in results], ["中文"])
        store = self.plugin._core._affinity_store
        self.assertEqual(store.get(group_key(self.owner_key), 0, -50, 100), 1)
        self.assertEqual(store.get(self.owner_key, 0), 89)
        affinity_logs = [line for line in self.logger.infos if "Affinity" in line]
        self.assertEqual(
            affinity_logs,
            [f"[STBridge] Affinity user={user_hash(group_key(self.owner_key))} "
             "before=0 parsed=+1 applied=+1 after=1 track=group"],
        )
        self.assertNotEqual(user_hash(group_key(self.owner_key)), user_hash(self.owner_key))

    async def test_group_request_carries_owner_value_and_the_members_own_zero(self):
        self.use_owner_value()
        event = _group_bridge_event(self.module, "你好", sender_id="qq-user", sender_name="制作人")
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            processing = asyncio.create_task(_collect(self.plugin.stbridge_group_message, event))
            for _ in range(100):
                request = self.plugin._core._request_state_manager.peek_pending_request()
                if request:
                    break
                await asyncio.sleep(0)
            self.assertEqual(request.content, "【群友】制作人（1）[好感度:0]：你好")
            self.assertEqual(request.affinity_writes, (
                {"path": [CHARACTER, "好感度"], "value": 89}, {"path": ["群友好感度"], "value": 0}, {"path": ["点歌许可"], "value": 0},
            ))
            self.plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
            await processing

    async def test_commands_show_each_track_separately(self):
        self.use_owner_value()
        result, = await _collect(self.plugin.stbridge_affinity, _bridge_event(self.module, "/好感度"))
        self.assertEqual(result.text, "琴音对你的好感度：89（伴侣）")
        group_event = _group_bridge_event(self.module, "/好感度", sender_id="qq-user")
        result, = await _collect(self.plugin.stbridge_affinity, group_event)
        self.assertEqual(result.text, "琴音对你的好感度：0（普通网友）")

    async def test_private_turn_still_updates_the_owner_record(self):
        self.use_owner_value()
        await self.private_reply('☆‹日本語›"中文"' + update_block("_.add('藤田琴音.好感度', 1);"))
        self.assertEqual(self.plugin._core._affinity_store.get(self.owner_key, 0), 90)
        self.assertNotIn(group_key(self.owner_key), self.plugin._core._affinity_store.data["users"])


class AffinityTrackSettingsTests(unittest.TestCase):
    def test_group_defaults(self):
        group = default_affinity_settings()["group"]
        self.assertEqual((group["variable"], group["min"], group["max"], group["initial_value"]),
                         ("群友好感度", -50, 100, 0))
        self.assertEqual([(tier["min"], tier["max"], tier["name"]) for tier in group["tiers"]], [
            (-50, -21, "黑粉"), (-20, 20, "普通网友"), (21, 40, "友善网友"),
            (41, 60, "熟悉的支持者"), (61, 80, "忠实粉丝"), (81, 100, "狂热粉丝"),
        ])
        settings = validate_affinity_settings({})
        self.assertEqual(settings, default_affinity_settings())
        self.assertEqual(affinity_tier_name(group, -50), "黑粉")
        self.assertEqual(affinity_tier_name(group, 0), "普通网友")
        self.assertEqual(affinity_tier_name(group, 100), "狂热粉丝")

    def test_group_settings_are_validated(self):
        def tiers(*items):
            return [{"min": low, "max": high, "name": name} for low, high, name in items]

        cases = (
            {"min": -101},
            {"max": 101},
            {"min": 10, "max": 10},
            {"min": 50, "max": 20},
            {"min": True},
            {"variable": ""},
            {"variable": "a.b"},
            {"variable": "__proto__"},
            {"initial_value": -51},
            {"initial_value": 101},
            {"tiers": tiers((-60, 0, "a"), (1, 100, "b"))},
            {"tiers": tiers((-50, 20, "a"), (20, 100, "b"))},
            {"tiers": tiers((-50, 100, ""))},
            {"tiers": tiers((-50, 120, "a"))},
            {"tiers": []},
            "text",
        )
        for group in cases:
            with self.subTest(group=group):
                with self.assertRaises(EmotionConfigError):
                    validate_affinity_settings({"group": group})
        custom = validate_affinity_settings({"group": {
            "min": -100, "max": 50, "initial_value": -10,
            "tiers": tiers((-100, -1, "负"), (0, 50, "正")),
        }})["group"]
        self.assertEqual((custom["min"], custom["max"], custom["initial_value"]), (-100, 50, -10))
        # the owner track keeps its fixed 0-100 scale
        with self.assertRaises(EmotionConfigError):
            validate_affinity_settings({"tiers": tiers((-5, 100, "x"))})

    def test_owner_and_group_tracks_share_max_delta(self):
        settings = validate_affinity_settings({"max_delta": 7})
        owner, group = affinity_track(settings, "owner"), affinity_track(settings, "group")
        self.assertEqual((owner["min"], owner["max"], owner["path"]), (0, 100, [CHARACTER, "好感度"]))
        self.assertEqual((group["min"], group["max"], group["path"]), (-50, 100, ["群友好感度"]))
        self.assertEqual((owner["max_delta"], group["max_delta"]), (7, 7))

    def test_unsafe_character_segments_are_rejected(self):
        for character in ("a.b", "a[0]", "__proto__", "constructor", ""):
            with self.subTest(character=character):
                with self.assertRaises(EmotionConfigError):
                    validate_affinity_settings({"character": character})


class PluginAffinityMigrationTests(unittest.TestCase):
    """The plugin passes the operator's private identity as the owner key."""

    def load(self, config):
        module = _load_plugin_module()
        logger = module.test_logger
        for messages in (logger.warnings, logger.errors, logger.infos):
            messages.clear()
        with tempfile.TemporaryDirectory(prefix="st-bridge-plugin-migration-") as directory:
            root = Path(directory)
            owner = user_key("qq-platform", "qq-user")
            member = user_key("qq-platform", "member-openid")
            (root / "affinity.json").write_text(json.dumps({"version": 2, "users": {
                owner: {"value": 70}, member: {"value": 8},
            }}), encoding="utf-8")
            plugin = module.STBridgePlugin(context=None, config=config)
            with patch.object(module.star, "StarTools", type("Tools", (), {
                "get_data_dir": staticmethod(lambda _name: root),
            }), create=True):
                plugin._core._load_affinity_store()
            store = plugin._core._affinity_store
            return store, logger, owner, member, (root / "affinity.json.bak-p6b1").exists()

    def test_owner_from_operator_config_is_kept_and_members_move_to_group_keys(self):
        store, logger, owner, member, backed_up = self.load(
            {"operator_platform_id": "qq-platform", "operator_sender_id": "qq-user"}
        )
        self.assertEqual(store.get(owner, 0), 70)
        self.assertEqual(store.get(group_key(member), 0, -50, 100), 8)
        self.assertNotIn(member, store.data["users"])
        self.assertTrue(backed_up)
        self.assertIn("[STBridge] Affinity data migrated (group values reset once)", logger.infos)

    def test_missing_operator_config_defers_migration_with_a_warning(self):
        store, logger, owner, member, backed_up = self.load({})
        self.assertEqual(store.get(member, 0), 8)
        self.assertEqual(store.data["version"], 2)
        self.assertFalse(backed_up)
        self.assertIn("[STBridge] Affinity migration deferred (OwnerUnknown)", logger.warnings)


class AffinityMigrationTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-affinity-migration-")
        self.root = Path(self._directory.name)
        self.owner = user_key("qq", "owner-private")
        self.member = user_key("qq", "member-openid")
        self.entries = {
            self.owner: {"value": 90, "nickname": "制作人", "updated_at": "2026-11-01T00:00:00+00:00"},
            self.member: {"value": 7, "nickname": "群友", "updated_at": "2026-11-01T01:00:00+00:00"},
        }
        self.path = self.root / "affinity.json"
        self.write(2, self.entries)

    def write(self, version, users):
        self.path.write_text(json.dumps({"version": version, "users": users}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._directory.cleanup()

    def test_v2_load_backs_up_moves_members_and_keeps_the_owner(self):
        original = self.path.read_bytes()
        store = AffinityStore(self.root, owner_key=self.owner)
        self.assertTrue(store.migrated)
        self.assertEqual((self.root / "affinity.json.bak-p6b1").read_bytes(), original)
        self.assertEqual(store.data["version"], 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["version"], 3)
        # the owner is untouched: key, value (90 is not rolled back), nickname, timestamp
        self.assertEqual(store.data["users"][self.owner], self.entries[self.owner])
        # the member moved with value, nickname and timestamp unchanged
        self.assertNotIn(self.member, store.data["users"])
        self.assertEqual(store.data["users"][group_key(self.member)], self.entries[self.member])
        # the owner has no group record yet; it starts from the group initial value
        self.assertNotIn(group_key(self.owner), store.data["users"])
        self.assertEqual(store.get(group_key(self.owner), 0, -50, 100), 0)

    def test_migration_runs_only_once(self):
        AffinityStore(self.root, owner_key=self.owner)
        store = AffinityStore(self.root, owner_key=self.owner)
        self.assertFalse(store.migrated)
        store.set_value(group_key(self.member), 9, low=-50, high=100)
        backup = (self.root / "affinity.json.bak-p6b1").read_bytes()
        again = AffinityStore(self.root, owner_key=self.owner)
        self.assertFalse(again.migrated)
        self.assertEqual(again.get(group_key(self.member), 0, -50, 100), 9)
        self.assertEqual((self.root / "affinity.json.bak-p6b1").read_bytes(), backup)
        self.assertEqual(json.loads(backup)["version"], 2)

    def test_existing_target_wins_and_prefixed_entries_stay(self):
        target = {"value": 33, "nickname": "已有", "updated_at": "2026-11-02T00:00:00+00:00"}
        self.write(2, {**self.entries, group_key(self.member): target})
        store = AffinityStore(self.root, owner_key=self.owner)
        self.assertEqual(store.data["users"][group_key(self.member)], target)
        self.assertNotIn(self.member, store.data["users"])
        self.assertEqual(set(store.data["users"]), {self.owner, group_key(self.member)})

    def test_existing_backup_is_never_overwritten(self):
        (self.root / "affinity.json.bak-p6b1").write_bytes(b"older backup")
        AffinityStore(self.root, owner_key=self.owner)
        self.assertEqual((self.root / "affinity.json.bak-p6b1").read_bytes(), b"older backup")

    def test_unknown_owner_defers_the_migration(self):
        original = self.path.read_bytes()
        store = AffinityStore(self.root)
        self.assertTrue(store.migration_pending)
        self.assertFalse(store.migrated)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "affinity.json.bak-p6b1").exists())
        self.assertEqual(store.data["version"], 2)
        later = AffinityStore(self.root, owner_key=self.owner)
        self.assertTrue(later.migrated)
        self.assertEqual(later.get(group_key(self.member), 0, -50, 100), 7)

    def test_v1_file_resets_members_then_moves_them(self):
        self.write(1, self.entries)
        store = AffinityStore(self.root, owner_key=self.owner, group_initial_value=5)
        self.assertEqual(store.data["version"], 3)
        self.assertEqual(store.get(self.owner, 0), 90)
        self.assertEqual(store.get(group_key(self.member), 0, -50, 100), 5)
        self.assertTrue((self.root / "affinity.json.bak-p6b").exists())

    def test_new_file_is_created_at_the_current_version(self):
        self.path.unlink()
        self.assertEqual(AffinityStore(self.root).data["version"], 3)

    def test_users_are_tagged_by_key_prefix(self):
        store = AffinityStore(self.root, owner_key=self.owner)
        store.set_value(group_key(self.owner), 4, nickname="制作人（群）", low=-50, high=100)
        users = store.users({"owner": (0, 100), "group": (-50, 100)})
        tracks = {user["id"]: (user["track"], user["platform"]) for user in users}
        self.assertEqual(tracks, {
            user_hash(self.owner): ("owner", "qq"),
            user_hash(group_key(self.owner)): ("group", "qq"),
            user_hash(group_key(self.member)): ("group", "qq"),
        })


class AffinityConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-affinity-console-")
        root = Path(self._directory.name)
        self.store = ConfigStore(root)
        self.key = user_key("qq", "openid-1")
        self.affinity = AffinityStore(root, owner_key=self.key)
        self.affinity.set_value(self.key, 45, nickname="小明")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.service = ConsoleService(create_console_app(self.store, affinity_store=self.affinity), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()
        self._directory.cleanup()

    async def call(self, method, path, payload=None, headers=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload,
                                       headers=headers or {}) as response:
            return response.status, await response.json()

    async def test_list_edit_and_settings(self):
        status, body = await self.call("GET", "/api/affinity")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["max_delta"], 5)
        user, = body["users"]
        self.assertEqual((user["id"], user["nickname"], user["value"], user["tier"]),
                         (user_hash(self.key), "小明", 45, "避风港"))
        self.assertNotIn("openid-1", json.dumps(body))

        status, body = await self.call("PUT", f"/api/affinity/users/{user_hash(self.key)}", {"value": 90})
        self.assertEqual((status, body["tier"]), (200, "伴侣"))
        self.assertEqual(self.affinity.get(self.key, 0), 90)
        for payload, expected in (({"value": 101}, 400), ({"value": "5"}, 400), ({"value": True}, 400)):
            status, _ = await self.call("PUT", f"/api/affinity/users/{user_hash(self.key)}", payload)
            self.assertEqual(status, expected)
        status, _ = await self.call("PUT", "/api/affinity/users/00000000", {"value": 1})
        self.assertEqual(status, 404)

        new_settings = dict(default_affinity_settings(), max_delta=3, initial_value=20)
        status, body = await self.call("POST", "/api/affinity/settings", new_settings)
        self.assertEqual((status, body["settings"]["max_delta"]), (200, 3))
        self.assertEqual(self.store.config["affinity"]["initial_value"], 20)
        status, _ = await self.call("POST", "/api/affinity/settings", {"max_delta": 1000})
        self.assertEqual(status, 400)

    async def test_tracks_ranges_tiers_and_group_settings(self):
        member = group_key(user_key("qq", "member-openid"))
        self.affinity.set_value(member, -30, nickname="群友", low=-50, high=100)
        status, body = await self.call("GET", "/api/affinity")
        self.assertEqual(status, 200)
        users = {user["id"]: user for user in body["users"]}
        self.assertEqual((users[user_hash(self.key)]["track"], users[user_hash(self.key)]["tier"]),
                         ("owner", "避风港"))
        self.assertEqual((users[user_hash(member)]["track"], users[user_hash(member)]["value"],
                          users[user_hash(member)]["tier"]), ("group", -30, "黑粉"))
        self.assertEqual(body["settings"]["group"]["min"], -50)
        self.assertNotIn("member-openid", json.dumps(body))

        # the owner also talks in groups under the same openid: two rows, one per track
        self.affinity.set_value(group_key(self.key), 4, nickname="制作人（群）", low=-50, high=100)
        status, body = await self.call("GET", "/api/affinity")
        rows = {user["id"]: user for user in body["users"]}
        self.assertEqual((rows[user_hash(self.key)]["track"], rows[user_hash(self.key)]["value"]), ("owner", 45))
        self.assertEqual((rows[user_hash(group_key(self.key))]["track"], rows[user_hash(group_key(self.key))]["tier"]),
                         ("group", "普通网友"))
        status, body = await self.call("PUT", f"/api/affinity/users/{user_hash(group_key(self.key))}", {"value": -20})
        self.assertEqual((status, body["track"]), (200, "group"))
        self.assertEqual(self.affinity.get(self.key, 0), 45)

        url = f"/api/affinity/users/{user_hash(member)}"
        status, body = await self.call("PUT", url, {"value": -50})
        self.assertEqual((status, body["track"], body["tier"]), (200, "group", "黑粉"))
        self.assertEqual(self.affinity.get(member, 0, -50, 100), -50)
        for value in (-51, 101):
            status, _ = await self.call("PUT", url, {"value": value})
            self.assertEqual(status, 400)
        # the owner keeps the 0-100 scale
        status, _ = await self.call("PUT", f"/api/affinity/users/{user_hash(self.key)}", {"value": -1})
        self.assertEqual(status, 400)

        group = dict(default_affinity_settings()["group"], min=-100, max=60, initial_value=-5,
                     tiers=[{"min": -100, "max": 0, "name": "冷淡"}, {"min": 1, "max": 60, "name": "热情"}])
        status, body = await self.call("POST", "/api/affinity/settings",
                                       dict(default_affinity_settings(), group=group))
        self.assertEqual((status, body["settings"]["group"]["min"]), (200, -100))
        self.assertEqual(self.store.config["affinity"]["group"]["initial_value"], -5)
        status, _ = await self.call("PUT", url, {"value": -100})
        self.assertEqual(status, 200)
        status, _ = await self.call("PUT", url, {"value": 61})
        self.assertEqual(status, 400)
        bad = dict(default_affinity_settings(), group=dict(group, tiers=[{"min": -100, "max": 70, "name": "越界"}]))
        status, _ = await self.call("POST", "/api/affinity/settings", bad)
        self.assertEqual(status, 400)

    async def test_writes_need_same_origin(self):
        evil = {"Origin": "http://evil.example"}
        status, body = await self.call("PUT", f"/api/affinity/users/{user_hash(self.key)}", {"value": 0}, evil)
        self.assertEqual((status, body), (403, {"error": "forbidden_origin"}))
        status, _ = await self.call("POST", "/api/affinity/settings", default_affinity_settings(), evil)
        self.assertEqual(status, 403)
        self.assertEqual(self.affinity.get(self.key, 0), 45)

    async def test_missing_store_returns_503(self):
        await self.service.stop()
        self.service = ConsoleService(create_console_app(self.store), port=self.port)
        await self.service.start()
        status, body = await self.call("GET", "/api/affinity")
        self.assertEqual((status, body), (503, {"error": "affinity unavailable"}))


if __name__ == "__main__":
    unittest.main()
