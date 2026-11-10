"""L1c: mood continuity (recording, fading text, the two macros, settings, console API)."""

import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_config,
    default_mood_settings,
    fill_config_defaults,
    mood_settings,
    validate_mood_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mood_store import (
    EMPTY_TEXT,
    MAX_ENTRIES,
    MoodStore,
    format_mood,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.st_injection import MACRO_DESCRIPTIONS, MACRO_KEYS, build_macros
from test_plugin_lifecycle import (
    RecordingTts,
    _bridge_event,
    _group_bridge_event,
    _load_plugin_module,
    _ready_plugin,
    _wait_for_request,
)

NOW = 1_800_000_000.0
MIN = 60.0
SETTINGS = {"decay_minutes": 60, "recent": 3}


def item(emotion, ago_seconds):
    return {"emotion": emotion, "at": NOW - ago_seconds}


def text(entries, **settings):
    return format_mood(entries, NOW, {**SETTINGS, **settings})


class FormatTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(text([]), EMPTY_TEXT)
        self.assertEqual(EMPTY_TEXT, "还没有记录（按平常的状态）")

    def test_single_entry(self):
        self.assertEqual(text([item("开心", 10)]), "开心（刚才，还很明显）")

    def test_three_entries_oldest_first_with_the_spec_example(self):
        entries = [item("生气", 2 * 3600), item("认真", 15 * MIN), item("开心", 10)]
        self.assertEqual(
            text(entries),
            "生气（2 小时前，已经平复） → 认真（15 分钟前，还很明显） → 开心（刚才，还很明显）",
        )
        # unsorted input is put in time order
        self.assertEqual(text(list(reversed(entries))), text(entries))

    def test_degree_example_with_a_faded_middle_item(self):
        entries = [item("认真", 30 * MIN), item("开心", 10)]
        self.assertEqual(text(entries), "认真（30 分钟前，淡了一些） → 开心（刚才，还很明显）")

    def test_age_tiers(self):
        cases = (
            (0, "刚才"), (119, "刚才"), (120, "2 分钟前"), (59 * MIN + 59, "59 分钟前"),
            (3600, "1 小时前"), (23 * 3600 + 3599, "23 小时前"), (86400, "1 天前"), (3 * 86400 + 5, "3 天前"),
        )
        for ago, expected in cases:
            with self.subTest(ago=ago):
                self.assertIn(f"（{expected}，", text([item("开心", ago)], decay_minutes=1440))

    def test_a_future_timestamp_counts_as_just_now(self):
        self.assertEqual(text([item("开心", -500)]), "开心（刚才，还很明显）")

    def test_degree_boundaries(self):
        # W = 60: still obvious below 20 minutes, faded from 20 up to 60, over from 60
        self.assertIn("还很明显", text([item("开心", 20 * MIN - 1)]))
        self.assertIn("淡了一些", text([item("开心", 20 * MIN)]))
        self.assertIn("淡了一些", text([item("开心", 60 * MIN - 1)]))
        self.assertTrue(text([item("开心", 60 * MIN)]).startswith("开心（1 小时前，已经平复）"))
        self.assertIn("还很明显", text([item("开心", 100 * MIN)], decay_minutes=1440))
        self.assertIn("淡了一些", text([item("开心", 5 * MIN)], decay_minutes=10))
        self.assertIn("已经平复", text([item("开心", 10 * MIN)], decay_minutes=10))

    def test_calm_sentence_only_when_the_latest_has_passed(self):
        self.assertTrue(text([item("开心", 3 * 3600)]).endswith("；现在心情平静"))
        self.assertTrue(text([item("生气", 5 * 3600), item("开心", 3 * 3600)]).endswith("已经平复）；现在心情平静"))
        self.assertNotIn("现在心情平静", text([item("生气", 5 * 3600), item("开心", 10)]))
        self.assertNotIn("现在心情平静", text([item("生气", 10), item("开心", 30 * MIN)]))

    def test_consecutive_same_emotions_merge_with_a_count_and_the_latest_time(self):
        entries = [item("开心", 30 * MIN), item("开心", 20 * MIN), item("开心", 10)]
        self.assertEqual(text(entries), "开心（刚才，连续 3 次，还很明显）")
        # not consecutive: no merge
        split = [item("开心", 30 * MIN), item("生气", 20 * MIN), item("开心", 10)]
        self.assertEqual(text(split).count("开心"), 2)

    def test_merging_happens_before_taking_the_most_recent(self):
        entries = [item("伤心", 50 * MIN), item("生气", 40 * MIN), item("开心", 3 * MIN), item("开心", 2 * MIN), item("开心", 10)]
        # five entries, three items after merging: all three are shown although recent = 3
        self.assertEqual(
            text(entries),
            "伤心（50 分钟前，淡了一些） → 生气（40 分钟前，淡了一些） → 开心（刚才，连续 3 次，还很明显）",
        )
        # recent = 2 takes the last two merged items
        self.assertEqual(
            text(entries, recent=2),
            "生气（40 分钟前，淡了一些） → 开心（刚才，连续 3 次，还很明显）",
        )

    def test_recent_limit(self):
        entries = [item(name, ago) for name, ago in (("害羞", 50 * MIN), ("激动", 40 * MIN), ("惊讶", 30 * MIN), ("开心", 10 * MIN), ("认真", 10))]
        self.assertEqual(text(entries, recent=1), "认真（刚才，还很明显）")
        self.assertEqual(text(entries, recent=4).count(" → "), 3)
        self.assertEqual(text(entries, recent=10).count(" → "), 4)

    def test_over_long_text_drops_the_oldest_items(self):
        names = [f"{index}" + "情" * 300 for index in range(5)]
        entries = [item(name, (len(names) - index) * 3 * MIN) for index, name in enumerate(names)]
        result = text(entries, recent=10)
        self.assertLessEqual(len(result), 1000)
        self.assertIn(names[-1], result)
        self.assertNotIn(names[0], result)

    def test_a_single_huge_item_is_cut_to_the_limit(self):
        self.assertEqual(len(text([item("情" * 2000, 1)])), 1000)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = NOW
        self.store = MoodStore(self.directory.name, clock=lambda: self.now)

    def test_records_in_the_chosen_lists_with_the_clock(self):
        self.store.record("开心", ("global", "owner"))
        self.store.record("生气", ("global",))
        self.assertEqual([entry["emotion"] for entry in self.store.entries("global")], ["开心", "生气"])
        self.assertEqual([entry["emotion"] for entry in self.store.entries("owner")], ["开心"])
        self.assertEqual(self.store.entries("owner")[0]["at"], NOW)

    def test_survives_a_restart(self):
        self.store.record("开心", ("global", "owner"))
        again = MoodStore(self.directory.name, clock=lambda: self.now + 5)
        self.assertEqual(again.entries("owner"), self.store.entries("owner"))

    def test_keeps_only_the_newest_twenty(self):
        for index in range(MAX_ENTRIES + 5):
            self.store.record(f"e{index}", ("global",), at=NOW + index)
        entries = self.store.entries("global")
        self.assertEqual(len(entries), MAX_ENTRIES)
        self.assertEqual(entries[0]["emotion"], "e5")
        self.assertEqual(entries[-1]["emotion"], f"e{MAX_ENTRIES + 4}")
        self.assertEqual(len(MoodStore(self.directory.name).entries("global")), MAX_ENTRIES)

    def test_clear_one_list(self):
        self.store.record("开心", ("global", "owner"))
        self.store.clear("owner")
        self.assertEqual(self.store.entries("owner"), [])
        self.assertEqual(len(self.store.entries("global")), 1)
        self.assertEqual(MoodStore(self.directory.name).entries("owner"), [])
        with self.assertRaises(ValueError):
            self.store.clear("everyone")

    def test_broken_files_read_as_empty_with_a_warning(self):
        module = _load_plugin_module()
        path = Path(self.directory.name) / "mood.json"
        for raw in ("{not json", "[]", '{"global": "x", "owner": 5}', ""):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                module.test_logger.warnings.clear()
                store = MoodStore(self.directory.name)
                self.assertEqual((store.entries("global"), store.entries("owner")), ([], []))
        path.write_text("{not json", encoding="utf-8")
        with patch("data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mood_store.logger") as log:
            MoodStore(self.directory.name)
        log.warning.assert_called_once()

    def test_bad_entries_inside_a_good_file_are_skipped(self):
        path = Path(self.directory.name) / "mood.json"
        path.write_text(json.dumps({"global": [
            {"emotion": "开心", "at": NOW}, {"emotion": "", "at": NOW}, {"emotion": "x"}, {"emotion": "y", "at": "z"},
            {"emotion": "z", "at": True}, "junk",
        ]}), encoding="utf-8")
        self.assertEqual([entry["emotion"] for entry in MoodStore(self.directory.name).entries("global")], ["开心"])

    def test_a_failed_write_is_logged_and_never_raises(self):
        def boom():
            raise OSError("disk full")

        self.store._write = boom
        with patch("data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mood_store.logger") as log:
            self.store.record("开心", ("global",))
            self.store.clear("global")
        self.assertEqual(log.warning.call_count, 2)

    def test_text_uses_the_store_clock(self):
        self.store.record("开心", ("global",))
        self.assertEqual(self.store.text("global", SETTINGS), "开心（刚才，还很明显）")
        self.now += 2 * 3600
        self.assertEqual(self.store.text("global", SETTINGS), "开心（2 小时前，已经平复）；现在心情平静")
        self.assertEqual(self.store.text("owner", SETTINGS), EMPTY_TEXT)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_ranges(self):
        self.assertEqual(default_mood_settings(), {"decay_minutes": 60, "recent": 3})
        self.assertEqual(default_config()["mood"], default_mood_settings())
        for field, value in (
            ("decay_minutes", 4), ("decay_minutes", 1441), ("decay_minutes", 60.5), ("decay_minutes", True), ("decay_minutes", "x"),
            ("recent", 0), ("recent", 11), ("recent", 2.5), ("recent", None),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(EmotionConfigError):
                    validate_mood_settings({field: value})
        with self.assertRaises(EmotionConfigError):
            validate_mood_settings([])
        self.assertEqual(validate_mood_settings({"decay_minutes": 5, "recent": 10}), {"decay_minutes": 5, "recent": 10})
        self.assertEqual(validate_mood_settings({"decay_minutes": 1440, "recent": 1}), {"decay_minutes": 1440, "recent": 1})
        self.assertEqual(validate_mood_settings({"recent": 5}), {"decay_minutes": 60, "recent": 5})

    def test_old_config_without_the_section(self):
        config = default_config()
        del config["mood"]
        self.assertEqual(mood_settings(config), default_mood_settings())
        fill_config_defaults(config)
        self.assertEqual(config["mood"], default_mood_settings())
        self.assertEqual(mood_settings({"mood": {"decay_minutes": "bad"}}), default_mood_settings())
        self.assertEqual(mood_settings(None), default_mood_settings())

    def test_presets_carry_the_section_and_old_presets_get_defaults(self):
        config = default_config()
        config["mood"] = {"decay_minutes": 120, "recent": 5}
        shot = config_presets.snapshot(config)
        self.assertEqual(shot["mood"], {"decay_minutes": 120, "recent": 5})
        restored = config_presets.normalize_config(shot, default_config())
        self.assertEqual(restored["mood"], {"decay_minutes": 120, "recent": 5})
        del shot["mood"]
        self.assertEqual(config_presets.normalize_config(shot, default_config())["mood"], default_mood_settings())


class MacroTests(unittest.TestCase):
    def test_keys_and_descriptions(self):
        self.assertIn("mood", MACRO_KEYS)
        self.assertIn("owner_mood", MACRO_KEYS)
        self.assertEqual(set(MACRO_KEYS), set(MACRO_DESCRIPTIONS))
        self.assertEqual(
            MACRO_DESCRIPTIONS["mood"], "琴音最近的心情走向（所有人和定时消息都会影响），带距今时间和是否已经平复"
        )
        self.assertEqual(
            MACRO_DESCRIPTIONS["owner_mood"], "琴音对主人的心情走向（只有主人私聊和发给主人的定时消息会影响）"
        )

    def test_build_macros_always_has_both_keys(self):
        values = build_macros(default_config(), track="owner", value=None)
        self.assertEqual((values["mood"], values["owner_mood"]), (EMPTY_TEXT, EMPTY_TEXT))
        values = build_macros(default_config(), track="owner", value=None, mood="a", owner_mood="b")
        self.assertEqual((values["mood"], values["owner_mood"]), ("a", "b"))
        self.assertEqual(set(values), set(MACRO_KEYS))


EMOTIONS = {"开心": {}, "生气": {}, "认真": {}}


class FlowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = _ready_plugin(self.module, voice=True)
        self.core = self.plugin._core
        self.core._tts.common["prob"] = 0  # text only: this file is about the mood
        self.store = ConfigStore(Path(self.directory.name))
        await self.store.update(lambda config: config["characters"].update({
            "琴音": {"name": "琴音", "model_name": "", "default_emotion": "开心", "emotions": dict(EMOTIONS)}
        }) or config.update(default_character="琴音"))
        self.core._emotion_store = self.store
        self.now = NOW
        self.moods = MoodStore(Path(self.directory.name), clock=lambda: self.now)
        self.core._mood_store = self.moods
        self.attempts = 0

    async def reply(self, text, *, group=False, fail=False):
        if group:
            event = _group_bridge_event(self.module, "hi")
            handler = self.plugin.stbridge_group_message
        else:
            event = _bridge_event(self.module, "hi")
            handler = self.plugin.stbridge_private_message
        task = asyncio.create_task(self._collect(handler, event))
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        self.attempts += 1
        if fail:
            manager.fail_request(request.request_id, "generation_failed")
        else:
            manager.claim_request(request.request_id, f"attempt-{self.attempts}")
            manager.mark_generation_started(request.request_id)
            manager.confirm_generation_finished(request.request_id)
            manager.complete_request(request.request_id, text)
        return request, await task

    async def _collect(self, handler, event):
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            return [item async for item in handler(event)]

    def emotions(self, track):
        return [entry["emotion"] for entry in self.moods.entries(track)]


class RecordingTests(FlowBase):
    async def test_owner_private_chat_records_both_lists_with_the_clock_time(self):
        await self.reply('[情绪:开心]☆‹a›"1"')
        self.assertEqual((self.emotions("global"), self.emotions("owner")), (["开心"], ["开心"]))
        self.assertEqual(self.moods.entries("global")[0]["at"], NOW)

    async def test_group_chat_records_only_the_overall_list(self):
        await self.reply('[情绪:生气]☆‹a›"1"', group=True)
        self.assertEqual((self.emotions("global"), self.emotions("owner")), (["生气"], []))

    async def test_invalid_reply_and_failed_request_record_nothing(self):
        await self.reply('[情绪:开心]没有台词')
        await self.reply("", fail=True)
        self.assertEqual((self.emotions("global"), self.emotions("owner")), ([], []))

    async def test_several_groups_record_the_last_groups_emotion(self):
        await self.reply('[情绪:开心]☆‹a›"1"[情绪:生气]☆‹b›"2"')
        self.assertEqual(self.emotions("global"), ["生气"])

    async def test_a_reply_without_any_tag_records_nothing(self):
        await self.reply('☆‹a›"1"')
        self.assertEqual(self.emotions("global"), [])

    async def test_an_emotion_that_is_not_configured_is_not_recorded(self):
        self.module.test_logger.infos.clear()
        await self.reply('[情绪:惊讶]☆‹a›"1"')
        self.assertEqual((self.emotions("global"), self.emotions("owner")), ([], []))
        self.assertTrue(any("Mood not recorded" in line for line in self.module.test_logger.infos))

    async def test_a_failing_store_never_changes_the_reply(self):
        def boom():
            raise OSError("disk full")

        self.moods._write = boom
        _, items = await self.reply('[情绪:开心]☆‹a›"1"')
        self.assertEqual([item.text for item in items], ["1"])

    async def test_a_missing_store_changes_nothing_either(self):
        self.core._mood_store = None
        _, items = await self.reply('[情绪:开心]☆‹a›"1"')
        self.assertEqual([item.text for item in items], ["1"])


class MacroFlowTests(FlowBase):
    async def test_every_turn_carries_both_mood_macros(self):
        request, _ = await self.reply('[情绪:开心]☆‹a›"1"')
        self.assertEqual(request.macros["mood"], EMPTY_TEXT)
        self.assertEqual(request.macros["owner_mood"], EMPTY_TEXT)
        self.now += 10 * MIN
        request, _ = await self.reply('[情绪:生气]☆‹a›"1"')
        self.assertEqual(request.macros["mood"], "开心（10 分钟前，还很明显）")
        self.assertEqual(request.macros["owner_mood"], "开心（10 分钟前，还很明显）")

    async def test_a_group_turn_gets_the_owner_mood_as_background_but_does_not_change_it(self):
        await self.reply('[情绪:开心]☆‹a›"1"')
        self.now += 5 * MIN
        request, _ = await self.reply('[情绪:生气]☆‹a›"1"', group=True)
        self.assertEqual(request.macros["owner_mood"], "开心（5 分钟前，还很明显）")
        self.assertEqual(self.emotions("owner"), ["开心"])
        self.assertEqual(self.emotions("global"), ["开心", "生气"])

    async def test_settings_from_the_config_apply(self):
        await self.store.update(lambda config: config.update(mood={"decay_minutes": 10, "recent": 1}))
        await self.reply('[情绪:开心]☆‹a›"1"')
        self.now += 11 * MIN
        request, _ = await self.reply('[情绪:生气]☆‹a›"1"')
        self.assertEqual(request.macros["mood"], "开心（11 分钟前，已经平复）；现在心情平静")

    async def test_the_injection_view_and_claim_values_include_the_new_keys_and_help(self):
        view = self.core._injection_view()
        self.assertIn("mood", view["macros"])
        self.assertIn("owner_mood", view["macros"])
        self.assertEqual(view["macro_help"]["mood"], MACRO_DESCRIPTIONS["mood"])
        self.assertEqual(set(view["macro_help"]), set(MACRO_KEYS))

    async def test_no_store_means_the_empty_text(self):
        self.core._mood_store = None
        request, _ = await self.reply('[情绪:开心]☆‹a›"1"')
        self.assertEqual(request.macros["mood"], EMPTY_TEXT)


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.directory.name))
        self.now = NOW
        self.moods = MoodStore(Path(self.directory.name), clock=lambda: self.now)
        self.port = _unused_port()
        self.service = ConsoleService(create_console_app(self.store, mood=self.moods), port=self.port)
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
            return response.status, await response.json()

    async def test_read_state(self):
        status, body = await self.call("/api/mood")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], default_mood_settings())
        self.assertEqual(body["global"], {"text": EMPTY_TEXT, "count": 0})
        self.assertTrue(body["available"])
        self.moods.record("开心", ("global", "owner"))
        self.moods.record("生气", ("global",))
        _, body = await self.call("/api/mood")
        self.assertEqual(body["global"]["count"], 2)
        self.assertEqual(body["global"]["text"], "开心（刚才，还很明显） → 生气（刚才，还很明显）")
        self.assertEqual(body["owner"], {"text": "开心（刚才，还很明显）", "count": 1})

    async def test_reset_clears_only_the_named_list(self):
        self.moods.record("开心", ("global", "owner"))
        status, body = await self.call("/api/mood/reset", "POST", {"track": "owner"})
        self.assertEqual(status, 200)
        self.assertEqual(body["owner"], {"text": EMPTY_TEXT, "count": 0})
        self.assertEqual(body["global"]["count"], 1)
        status, body = await self.call("/api/mood/reset", "POST", {"track": "global"})
        self.assertEqual(body["global"], {"text": EMPTY_TEXT, "count": 0})
        self.assertEqual(self.moods.entries("global"), [])

    async def test_reset_refuses_an_unknown_list(self):
        for payload in ({"track": "all"}, {}, {"track": 1}):
            with self.subTest(payload=payload):
                status, _ = await self.call("/api/mood/reset", "POST", payload)
                self.assertEqual(status, 400)

    async def test_save_settings(self):
        status, body = await self.call("/api/mood/settings", "POST", {"decay_minutes": 90, "recent": 4})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], {"decay_minutes": 90, "recent": 4})
        self.assertEqual(self.store.config["mood"], {"decay_minutes": 90, "recent": 4})
        status, _ = await self.call("/api/mood/settings", "POST", {"decay_minutes": 2, "recent": 4})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.config["mood"], {"decay_minutes": 90, "recent": 4})

    async def test_writes_refuse_foreign_origins(self):
        evil = {"Origin": "http://evil.example"}
        status, _ = await self.call("/api/mood/reset", "POST", {"track": "global"}, headers=evil)
        self.assertEqual(status, 403)
        status, _ = await self.call("/api/mood/settings", "POST", {"decay_minutes": 90, "recent": 4}, headers=evil)
        self.assertEqual(status, 403)

    async def test_without_a_store_it_reports_unavailable(self):
        service = ConsoleService(create_console_app(self.store), port=_unused_port())
        await service.start()
        try:
            async with self.client.get(f"http://127.0.0.1:{service._port}/api/mood") as response:
                body = await response.json()
            self.assertFalse(body["available"])
            async with self.client.post(
                f"http://127.0.0.1:{service._port}/api/mood/reset", json={"track": "global"}
            ) as response:
                self.assertEqual(response.status, 503)
        finally:
            await service.stop()


if __name__ == "__main__":
    unittest.main()
