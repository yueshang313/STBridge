"""R5a: the ST injection entries (config, descriptions, macros in the claim, entry / backup routes, console API)."""

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector
from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets, st_injection
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore, group_key, user_key
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_affinity_settings,
    default_config,
    fill_config_defaults,
    validate_affinity_settings,
    validate_config,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from test_console_server import _unused_loopback_port
from test_plugin_lifecycle import _bridge_event, _collect, _group_bridge_event, _load_plugin_module, _ready_plugin

ORIGIN = "http://localhost:11451"


def entry(**changes):
    value = {
        "id": "mine",
        "builtin": False,
        "title": "我的条目",
        "enabled": True,
        "content": "请保持礼貌。",
        "target": "preset",
        "placement": "after_chat_history",
        "depth": 0,
        "role": "system",
        "order": 5,
    }
    value.update(changes)
    return value


class EntrySettingsTests(unittest.TestCase):
    def test_defaults_are_the_nine_builtin_entries_in_the_agreed_places(self):
        settings = st_injection.default_injection_settings()
        self.assertEqual(st_injection.validate_injection_settings(settings), settings)
        self.assertEqual(settings["world_book_name"], "STbridge")
        entries = settings["entries"]
        self.assertEqual(
            [item["id"] for item in entries],
            ["style", "identity", "length", "bilingual", "game", "emotion", "song", "affinity_hint", "affinity_rule"],
        )
        self.assertTrue(all(item["builtin"] and item["enabled"] for item in entries))
        self.assertEqual([item["target"] for item in entries], ["preset"] * 7 + ["worldbook"] * 2)
        self.assertTrue(all(item["placement"] == "after_chat_history" for item in entries))
        self.assertEqual([item["order"] for item in entries], sorted(item["order"] for item in entries))
        by_id = {item["id"]: item["content"] for item in entries}
        self.assertIn("{{stbridge_emotions}}", by_id["emotion"])
        self.assertIn("{{stbridge_song_allowed}}", by_id["song"])
        self.assertIn("{{stbridge_affinity_var}}", by_id["affinity_rule"])
        # the draft's sentence about the switch-off is gone: the macro says "否" instead
        self.assertNotIn("自动不注入", by_id["song"])

    def test_every_macro_an_entry_uses_exists(self):
        import re

        used = set()
        for item in st_injection.default_entries():
            used |= set(re.findall(r"\{\{stbridge_([a-z_]+)\}\}", item["content"]))
        self.assertTrue(used <= set(st_injection.MACRO_KEYS), used - set(st_injection.MACRO_KEYS))
        self.assertEqual(set(st_injection.MACRO_DESCRIPTIONS), set(st_injection.MACRO_KEYS))

    def test_a_custom_entry_and_the_limits(self):
        settings = {"world_book_name": "我的书", "entries": [entry(), entry(id="b2", target="worldbook", placement="depth", depth=4, role="user")]}
        checked = st_injection.validate_injection_settings(settings)
        self.assertEqual(checked["entries"][1]["depth"], 4)
        bad = (
            entry(id="Bad-Id"),
            entry(id=""),
            entry(id="x" * 31),
            entry(id="style"),  # a custom entry may not take a built-in id
            entry(builtin=True),  # ... and a built-in flag needs a built-in id
            entry(title=""),
            entry(title="a" * 41),
            entry(title="两行\n标题"),
            entry(content="   "),
            entry(content="a" * 4001),
            entry(content="bad\x00char"),
            entry(target="somewhere"),
            entry(placement="top"),
            entry(role="tool"),
            entry(depth=-1),
            entry(depth=101),
            entry(depth=1.5),
            entry(depth=True),
            entry(order="1"),
            entry(order=float("inf")),
            entry(enabled="yes"),
        )
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(EmotionConfigError):
                    st_injection.validate_injection_settings({"entries": [value]})
        for value in (
            {"entries": [entry(), entry()]},  # duplicate id
            {"entries": [entry(id=f"e{i}") for i in range(31)]},
            {"entries": "x"},
            {"world_book_name": ""},
            {"world_book_name": "a/b"},
            {"world_book_name": "n" * 61},
            [],
        ):
            with self.subTest(value=value):
                with self.assertRaises(EmotionConfigError):
                    st_injection.validate_injection_settings(value)
        self.assertEqual(len(st_injection.validate_injection_settings({"entries": [entry(id=f"e{i}") for i in range(30)]})["entries"]), 30)

    def test_an_empty_list_is_allowed_and_deleted_builtins_stay_deleted(self):
        self.assertEqual(st_injection.validate_injection_settings({"entries": []})["entries"], [])

    def test_the_section_is_in_the_default_config_and_validated(self):
        config = default_config()
        self.assertEqual(config["st_injection"], st_injection.default_injection_settings())
        config["st_injection"]["entries"][0]["target"] = "nowhere"
        with self.assertRaises(EmotionConfigError):
            validate_config(config)

    def test_an_old_config_gets_the_section_and_a_broken_one_falls_back_to_the_defaults(self):
        config = default_config()
        del config["st_injection"]
        fill_config_defaults(config)
        self.assertEqual(config["st_injection"], st_injection.default_injection_settings())
        self.assertEqual(st_injection.injection_settings({"st_injection": {"entries": "x"}}), st_injection.default_injection_settings())

    def test_the_public_list_is_ordered_by_order_then_position(self):
        config = {"st_injection": {"entries": [entry(id="late", order=9), entry(id="a", order=1), entry(id="b", order=1)]}}
        view = st_injection.public_entries(config)
        self.assertEqual([item["id"] for item in view["entries"]], ["a", "b", "late"])
        self.assertNotIn("builtin", view["entries"][0])
        self.assertEqual(view["world_book_name"], "STbridge")


class TierDescriptionTests(unittest.TestCase):
    def test_defaults_carry_a_description_for_every_tier(self):
        settings = default_affinity_settings()
        self.assertEqual(len(settings["tiers"]), 5)
        self.assertEqual(len(settings["group"]["tiers"]), 6)
        for tier in [*settings["tiers"], *settings["group"]["tiers"]]:
            self.assertTrue(tier["description"].startswith("{{char}}"), tier)
        self.assertEqual(validate_affinity_settings(settings)["tiers"][2]["description"], settings["tiers"][2]["description"])

    def test_validation_keeps_trims_and_limits_the_description(self):
        settings = default_affinity_settings()
        settings["tiers"][0]["description"] = "  好  "
        self.assertEqual(validate_affinity_settings(settings)["tiers"][0]["description"], "好")
        settings["tiers"][0]["description"] = "a" * 501
        with self.assertRaises(EmotionConfigError):
            validate_affinity_settings(settings)
        settings["tiers"][0]["description"] = 5
        with self.assertRaises(EmotionConfigError):
            validate_affinity_settings(settings)
        del settings["tiers"][0]["description"]
        self.assertEqual(validate_affinity_settings(settings)["tiers"][0]["description"], "")

    def test_an_old_config_with_the_default_ranges_gets_the_default_texts(self):
        config = default_config()
        for tiers in (config["affinity"]["tiers"], config["affinity"]["group"]["tiers"]):
            for tier in tiers:
                del tier["description"]
        fill_config_defaults(config)
        self.assertEqual(config["affinity"], default_config()["affinity"])

    def test_reshaped_tiers_stay_empty_and_an_own_description_is_kept(self):
        config = default_config()
        for tier in config["affinity"]["tiers"]:
            del tier["description"]
        config["affinity"]["tiers"][1]["max"] = 39
        config["affinity"]["tiers"][2]["min"] = 40
        config["affinity"]["group"]["tiers"][0]["description"] = "我写的"
        del config["affinity"]["group"]["tiers"][1]["description"]
        fill_config_defaults(config)
        self.assertTrue(all("description" not in tier for tier in config["affinity"]["tiers"]))
        validated = validate_affinity_settings(config["affinity"])
        self.assertTrue(all(tier["description"] == "" for tier in validated["tiers"]))
        self.assertEqual(config["affinity"]["group"]["tiers"][0]["description"], "我写的")
        self.assertTrue(config["affinity"]["group"]["tiers"][1]["description"].startswith("{{char}}"))

    def test_a_tier_that_has_an_empty_description_keeps_it(self):
        config = default_config()
        config["affinity"]["tiers"][0]["description"] = ""
        fill_config_defaults(config)
        self.assertEqual(config["affinity"]["tiers"][0]["description"], "")

    def test_an_old_preset_applies_with_the_section_and_descriptions_filled(self):
        old = config_presets.snapshot(default_config())
        del old["st_injection"]
        for tier in old["affinity"]["tiers"]:
            del tier["description"]
        normalized = config_presets.normalize_config(old, default_config())
        self.assertEqual(normalized["st_injection"], st_injection.default_injection_settings())
        self.assertTrue(normalized["affinity"]["tiers"][0]["description"].startswith("{{char}}"))

    def test_the_section_travels_with_presets(self):
        config = default_config()
        config["st_injection"]["entries"][0]["content"] = "改过的文风"
        self.assertNotIn("st_injection", config_presets.EXCLUDED_SECTIONS)
        self.assertEqual(config_presets.snapshot(config)["st_injection"]["entries"][0]["content"], "改过的文风")
        applied = config_presets.normalize_config(config_presets.snapshot(config), default_config())
        self.assertEqual(applied["st_injection"]["entries"][0]["content"], "改过的文风")


class MacroTests(unittest.TestCase):
    def config(self):
        config = default_config()
        config["characters"] = {"琴音": {"emotions": {"开心": {}, "害羞": {}}, "nickname": "琴音", "st_name": "藤田琴音"}}
        config["default_character"] = "琴音"
        return config

    def test_owner_values(self):
        macros = st_injection.build_macros(self.config(), track="owner", value=45, song_allowed=True)
        self.assertEqual(set(macros), set(st_injection.MACRO_KEYS))
        self.assertTrue(all(isinstance(value, str) for value in macros.values()))
        self.assertEqual(macros["emotions"], "开心、害羞")
        self.assertEqual(macros["group_tag"], "【群友】")
        self.assertEqual(macros["speaker"], "")
        self.assertEqual(macros["affinity"], "45")
        self.assertEqual(macros["affinity_tier"], "避风港")
        self.assertTrue(macros["affinity_hint"].startswith("琴音把对方视为可以示弱的避风港"))
        self.assertEqual(macros["affinity_var"], "藤田琴音.好感度")
        self.assertEqual(macros["affinity_range"], "0～100")
        self.assertEqual(macros["affinity_step"], "5")
        self.assertEqual(macros["song_allowed"], "是")

    def test_group_values(self):
        macros = st_injection.build_macros(self.config(), track="group", value=-30, speaker_label="小明（1）")
        self.assertEqual(macros["speaker"], "小明（1）")
        self.assertEqual(macros["affinity_tier"], "黑粉")
        self.assertIn("黑粉或带有敌意的陌生人", macros["affinity_hint"])
        self.assertNotIn("{{char}}", macros["affinity_hint"])
        self.assertEqual(macros["affinity_var"], "群友好感度")
        self.assertEqual(macros["affinity_range"], "-50～100")
        self.assertEqual(macros["song_allowed"], "否")

    def test_unknown_value_uses_the_initial_one_and_no_character_falls_back(self):
        config = default_config()
        config["characters"], config["default_character"] = {}, ""  # the case is "no character"
        macros = st_injection.build_macros(config, track="owner", value=None)
        self.assertEqual(macros["affinity"], "0")
        self.assertEqual(macros["emotions"], "neutral")
        config["default_emotion"] = ""
        self.assertEqual(st_injection.build_macros(config, track="owner", value=1)["emotions"], "")

    def test_a_reshaped_tier_list_gives_an_empty_hint_and_a_gap_gives_the_unassigned_tier(self):
        config = self.config()
        config["affinity"]["tiers"] = [{"min": 0, "max": 10, "name": "只有一档"}]
        macros = st_injection.build_macros(config, track="owner", value=50)
        self.assertEqual((macros["affinity_tier"], macros["affinity_hint"]), ("未分档", ""))
        self.assertEqual(st_injection.build_macros(config, track="owner", value=3)["affinity_hint"], "")

    def test_the_hint_names_the_active_character_by_its_nickname(self):
        config = self.config()
        config["characters"]["琴音"]["nickname"] = "小琴"
        self.assertTrue(st_injection.build_macros(config, track="owner", value=5)["affinity_hint"].startswith("小琴把"))

    def test_macros_validation(self):
        good = st_injection.build_macros(self.config(), track="owner", value=1)
        self.assertEqual(st_injection.validate_macros(good), good)
        self.assertIsNone(st_injection.validate_macros(None))
        self.assertEqual(st_injection.validate_macros({}), {})
        for bad in ([], {"nope": "x"}, {"emotions": 1}, {"emotions": "a" * 1001}, "x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    st_injection.validate_macros(bad)


class ClaimAndRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.manager = RequestStateManager(timeout_seconds=10)
        self.config = HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452)
        self.backups = st_injection.PresetBackups(self.data)
        self.provider_calls = 0

        def provider():
            self.provider_calls += 1
            return {"world_book_name": "STbridge", "entries": [], "macros": {"emotions": "a"}}

        gate = BridgeRecoveryGate()  # closed: the injection routes do not need it open
        app = create_app(self.manager, self.config, recovery_gate=gate, injection_provider=provider, preset_backup=self.backups.save)
        self.gate = gate
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def headers(self):
        return {"Origin": ORIGIN}

    async def test_claim_carries_the_macros_only_when_the_request_has_them(self):
        self.gate.confirm_recovery()
        macros = {"emotions": "开心", "song_allowed": "否"}
        request = self.manager.create_request("hi", macros=macros)
        response = await self.client.post(f"/v1/requests/{request.request_id}/claim", json={"claim_attempt_id": "a1"}, headers=self.headers())
        self.assertEqual((await response.json())["macros"], macros)
        self.manager.mark_generation_started(request.request_id)
        self.manager.confirm_generation_finished(request.request_id)
        self.manager.complete_request(request.request_id, "done")
        plain = self.manager.create_request("hi")
        response = await self.client.post(f"/v1/requests/{plain.request_id}/claim", json={"claim_attempt_id": "a2"}, headers=self.headers())
        self.assertNotIn("macros", await response.json())

    async def test_bad_macros_are_refused_at_creation(self):
        with self.assertRaises(ValueError):
            self.manager.create_request("hi", macros={"emotions": 1})
        with self.assertRaises(ValueError):
            self.manager.create_request("hi", macros={"bogus": "x"})

    async def test_the_entry_list_is_served_while_the_gate_is_closed_and_needs_the_origin(self):
        response = await self.client.get("/v1/injection", headers=self.headers())
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["world_book_name"], "STbridge")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        denied = await self.client.get("/v1/injection", headers={"Origin": "http://evil.example"})
        self.assertEqual(denied.status, 403)
        # other business routes still wait for the gate
        pending = await self.client.get("/v1/requests/pending", headers=self.headers())
        self.assertEqual(pending.status, 503)

    async def test_without_a_provider_the_routes_say_unavailable(self):
        app = create_app(RequestStateManager(timeout_seconds=10), self.config)
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.assertEqual((await client.get("/v1/injection", headers=self.headers())).status, 503)
        self.assertEqual((await client.post("/v1/injection/backup", json={"name": "a", "preset": {}}, headers=self.headers())).status, 503)

    async def test_a_failing_provider_is_a_500_without_details(self):
        def boom():
            raise RuntimeError("secret detail")

        app = create_app(RequestStateManager(timeout_seconds=10), self.config, injection_provider=boom)
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/v1/injection", headers=self.headers())
        self.assertEqual(response.status, 500)
        self.assertNotIn("secret", await response.text())

    async def backup(self, payload, **kwargs):
        return await self.client.post("/v1/injection/backup", json=payload, headers=self.headers(), **kwargs)

    async def test_backup_writes_the_preset_into_the_backup_folder(self):
        preset = {"name": "我的预设", "prompts": [{"identifier": "main", "content": "你好"}]}
        response = await self.backup({"name": "我的预设", "preset": preset})
        self.assertEqual(response.status, 200)
        name = (await response.json())["file"]
        self.assertRegex(name, r"^我的预设-\d{8}-\d{6}\.json$")
        saved = self.data / "backups" / "st-presets" / name
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), preset)

    async def test_backup_name_is_sanitized(self):
        for given, expected in (("../../evil", "_.._evil"), ("a/b\\c:d*e?\"f<g>h|i", "a_b_c_d_e__f_g_h_i"), ("", "preset"), ("CON", "preset"), ("...", "preset"), ("nul.txt", "preset")):
            with self.subTest(given=given):
                response = await self.backup({"name": given, "preset": {}})
                self.assertEqual(response.status, 200)
                file = (await response.json())["file"]
                self.assertTrue(file.startswith(expected + "-"), file)
                self.assertEqual(Path(file).parent, Path("."))
        self.assertEqual({item.parent for item in (self.data / "backups" / "st-presets").iterdir()}, {self.data / "backups" / "st-presets"})
        self.assertFalse((self.data.parent / "evil").exists())

    async def test_backup_refuses_bad_bodies(self):
        for payload, status in (
            ({"name": "a"}, 400),
            ({"name": "a", "preset": {}, "extra": 1}, 400),
            ({"name": 5, "preset": {}}, 422),
            ({"name": "a", "preset": []}, 422),
            ([], 400),
        ):
            with self.subTest(payload=payload):
                self.assertEqual((await self.backup(payload)).status, status)
        response = await self.client.post("/v1/injection/backup", data=b"{not json", headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response = await self.client.post("/v1/injection/backup", data=b"x", headers={**self.headers(), "Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        self.assertEqual(list((self.data / "backups" / "st-presets").glob("*")) if (self.data / "backups").exists() else [], [])

    async def test_a_big_preset_is_fine_but_an_oversized_body_is_refused(self):
        big = {"prompts": [{"content": "字" * 200_000}]}  # about 600 KB: far above the usual 64 KB body limit
        self.assertEqual((await self.backup({"name": "big", "preset": big})).status, 200)
        huge = {"prompts": [{"content": "x" * (8 * 1024 * 1024)}]}
        self.assertEqual((await self.backup({"name": "huge", "preset": huge})).status, 413)
        # chunked upload without a Content-Length is limited as well
        async def chunks():
            for _ in range(9):
                yield b" " * (1024 * 1024)

        response = await self.client.post("/v1/injection/backup", data=chunks(), headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status, 413)
        self.assertEqual(len(list((self.data / "backups" / "st-presets").glob("*.json"))), 1)

    async def test_other_routes_keep_the_small_body_limit(self):
        self.gate.confirm_recovery()
        request = self.manager.create_request("hi")
        response = await self.client.post(
            f"/v1/requests/{request.request_id}/claim",
            json={"claim_attempt_id": "x" * 100_000},
            headers=self.headers(),
        )
        self.assertEqual(response.status, 413)


class PresetBackupFolderTests(unittest.TestCase):
    def test_only_the_newest_twenty_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_800_000_000.0]
            backups = st_injection.PresetBackups(Path(directory), clock=lambda: clock[0])
            names = []
            for index in range(25):
                clock[0] += 1
                time.sleep(0.02)  # the newest is told by the file's own mtime
                names.append(backups.save("p", json.dumps({"n": index})))
            kept = sorted(path.name for path in backups.directory.glob("*.json"))
            self.assertEqual(len(kept), st_injection.MAX_PRESET_BACKUPS)
            self.assertEqual(kept, sorted(names[5:]))
            self.assertEqual(list(backups.directory.glob("*.tmp")), [])

    def test_two_saves_in_one_second_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            backups = st_injection.PresetBackups(Path(directory), clock=lambda: 1_800_000_000.0)
            first, second = backups.save("p", "1"), backups.save("p", "2")
            self.assertNotEqual(first, second)
            self.assertEqual((backups.directory / first).read_text(), "1")

    def test_safe_stem(self):
        self.assertEqual(st_injection.safe_backup_stem("a" * 100), "a" * 60)
        self.assertEqual(st_injection.safe_backup_stem(None), "preset")
        self.assertEqual(st_injection.safe_backup_stem(" My Preset. "), "My Preset")


class PluginMacroTests(unittest.IsolatedAsyncioTestCase):
    """The macros of each kind of turn, as the plugin builds them when it creates the request."""

    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory(prefix="st-bridge-injection-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.plugin = _ready_plugin(self.module)
        core = self.plugin._core
        core._emotion_store = ConfigStore(self.root)
        core._emotion_store.config["characters"] = {
            "琴音": {"emotions": {"开心": {}, "害羞": {}}, "nickname": "琴音", "st_name": "藤田琴音"}
        }
        core._emotion_store.config["default_character"] = "琴音"
        core._affinity_store = AffinityStore(self.root, owner_key=user_key("qq-platform", "qq-user"))

    async def request_of(self, handler, event):
        manager = self.plugin._core._request_state_manager
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            processing = asyncio.create_task(_collect(handler, event))
            request = None
            for _ in range(200):
                request = manager.peek_pending_request()
                if request:
                    break
                await asyncio.sleep(0)
            manager.fail_request(request.request_id, "generation_failed")
            await processing
        return request

    async def test_private_turn(self):
        self.plugin._core._affinity_store.set_value(user_key("qq-platform", "qq-user"), 45)
        request = await self.request_of(self.plugin.stbridge_private_message, _bridge_event(self.module, "hello"))
        macros = request.macros
        self.assertEqual(set(macros), set(st_injection.MACRO_KEYS))
        self.assertEqual((macros["affinity"], macros["affinity_tier"]), ("45", "避风港"))
        self.assertEqual((macros["speaker"], macros["affinity_var"]), ("", "藤田琴音.好感度"))
        self.assertEqual(macros["emotions"], "开心、害羞")
        self.assertEqual(macros["song_allowed"], "否")  # no cover service in this setup

    async def test_private_turn_with_song_requests_possible(self):
        self.plugin._core._cover = SimpleNamespace()
        request = await self.request_of(self.plugin.stbridge_private_message, _bridge_event(self.module, "hello"))
        self.assertEqual(request.macros["song_allowed"], "是")
        # the same answer the written permission variable got
        self.assertEqual(request.affinity_writes[2]["value"], 1)

    async def test_song_feature_off_says_no(self):
        self.plugin._core._cover = SimpleNamespace()
        self.plugin._core._emotion_store.config["song_request"]["enabled"] = False
        request = await self.request_of(self.plugin.stbridge_private_message, _bridge_event(self.module, "hello"))
        self.assertEqual((request.macros["song_allowed"], request.affinity_writes[2]["value"]), ("否", 0))

    async def test_group_turn_uses_the_member_track_and_label(self):
        self.plugin._core._affinity_store.set_value(group_key(user_key("qq-platform", "member-a")), 45, low=-50, high=100)
        event = _group_bridge_event(self.module, "你好", sender_id="member-a", sender_name="小明")
        request = await self.request_of(self.plugin.stbridge_group_message, event)
        macros = request.macros
        self.assertEqual(macros["speaker"], "小明（1）")
        self.assertEqual((macros["affinity"], macros["affinity_tier"], macros["affinity_var"]), ("45", "熟悉的支持者", "群友好感度"))
        self.assertEqual((macros["affinity_range"], macros["group_tag"]), ("-50～100", "【群友】"))
        self.assertIn("记得对方的名字", macros["affinity_hint"])
        self.assertEqual(request.content, "【群友】小明（1）[好感度:45]：你好")

    async def test_scheduled_turn_is_the_owner_and_never_allows_a_song(self):
        core = self.plugin._core
        core._cover = SimpleNamespace()
        manager = core._request_state_manager

        plan = {
            "id": "0a1b2c3d", "name": "早安", "enabled": True, "kind": "daily", "time": "07:30", "weekdays": [],
            "jitter_minutes": 0, "target": {"type": "private"}, "prompt": "道早安", "fallback_texts": ["早安"],
        }
        task = asyncio.create_task(core._proactive_reply(plan, False, {}))
        request = None
        for _ in range(300):
            request = manager.peek_pending_request()
            if request:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(request)
        self.assertEqual(request.kind, "proactive")
        self.assertEqual((request.macros["song_allowed"], request.macros["speaker"]), ("否", ""))
        self.assertEqual(request.macros["affinity_var"], "藤田琴音.好感度")
        manager.fail_request(request.request_id, "generation_failed")
        await asyncio.wait_for(task, 10)

    async def test_without_a_config_store_no_macros_are_sent_and_the_turn_still_works(self):
        self.plugin._core._emotion_store = None
        request = await self.request_of(self.plugin.stbridge_private_message, _bridge_event(self.module, "hello"))
        self.assertIsNone(request.macros)

    async def test_a_failure_while_computing_macros_never_blocks_the_turn(self):
        with patch.object(st_injection, "build_macros", side_effect=RuntimeError("boom")), patch(
            "data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mixin_speaker.build_macros", side_effect=RuntimeError("boom")
        ):
            request = await self.request_of(self.plugin.stbridge_private_message, _bridge_event(self.module, "hello"))
        self.assertIsNone(request.macros)
        self.assertTrue(any("Macro values unavailable (RuntimeError)" in line for line in self.module.test_logger.warnings))

    async def test_the_entry_view_for_the_extension_has_the_owner_values(self):
        self.plugin._core._affinity_store.set_value(user_key("qq-platform", "qq-user"), 61)
        view = self.plugin._core._injection_view()
        self.assertEqual(view["world_book_name"], "STbridge")
        self.assertEqual(len(view["entries"]), 9)
        self.assertEqual(view["macros"]["affinity"], "61")
        self.assertEqual(view["macros"]["song_allowed"], "否")
        saved = self.plugin._core._backup_st_preset("预设", "{}")
        self.assertTrue((self.root / "backups" / "st-presets" / saved).exists())


class ConsoleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConfigStore(Path(self.directory.name))
        self.port = _unused_loopback_port()
        self.affinity = AffinityStore(Path(self.directory.name), owner_key=user_key("qq", "openid-1"))
        self.service = ConsoleService(create_console_app(self.store, affinity_store=self.affinity), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.service.stop)

    async def call(self, method, path, payload=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload) as response:
            return response.status, await response.json()

    async def test_get_returns_settings_defaults_and_the_macro_list(self):
        status, body = await self.call("GET", "/api/st-injection")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], st_injection.default_injection_settings())
        self.assertEqual(body["defaults"], st_injection.default_injection_settings())
        self.assertEqual([item["key"] for item in body["macros"]], list(st_injection.MACRO_KEYS))
        self.assertTrue(all(item["description"] for item in body["macros"]))

    async def test_save_validates_persists_and_keeps_the_rest_of_the_config(self):
        status, body = await self.call("GET", "/api/st-injection")
        settings = body["settings"]
        settings["entries"][0]["content"] = "新的文风"
        settings["entries"][1]["enabled"] = False
        settings["entries"].append(entry(id="custom1"))
        status, saved = await self.call("POST", "/api/st-injection", settings)
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"]["entries"][0]["content"], "新的文风")
        self.assertEqual(self.store.config["st_injection"]["entries"][-1]["id"], "custom1")
        on_disk = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        self.assertFalse(on_disk["st_injection"]["entries"][1]["enabled"])
        self.assertIn("affinity", on_disk)
        settings["entries"][0]["target"] = "x"
        status, _ = await self.call("POST", "/api/st-injection", settings)
        self.assertEqual(status, 400)
        self.assertEqual(self.store.config["st_injection"]["entries"][0]["target"], "preset")
        status, _ = await self.call("POST", "/api/st-injection", [])
        self.assertEqual(status, 400)

    async def test_a_saved_deleted_builtin_stays_deleted_and_can_be_restored_by_saving_defaults(self):
        status, body = await self.call("POST", "/api/st-injection", {"world_book_name": "STbridge", "entries": []})
        self.assertEqual((status, body["settings"]["entries"]), (200, []))
        status, body = await self.call("POST", "/api/st-injection", body["defaults"])
        self.assertEqual(len(body["settings"]["entries"]), 9)

    async def test_the_affinity_api_round_trips_tier_descriptions(self):
        status, body = await self.call("GET", "/api/affinity")
        self.assertTrue(body["settings"]["tiers"][0]["description"].startswith("{{char}}"))
        settings = body["settings"]
        settings["tiers"][0]["description"] = "新的描述"
        settings["group"]["tiers"][5]["description"] = ""
        status, saved = await self.call("POST", "/api/affinity/settings", settings)
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"]["tiers"][0]["description"], "新的描述")
        self.assertEqual(saved["settings"]["group"]["tiers"][5]["description"], "")
        settings["tiers"][0]["description"] = "x" * 501
        status, _ = await self.call("POST", "/api/affinity/settings", settings)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
