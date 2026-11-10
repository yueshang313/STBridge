"""L1d: proactive messages keep out of the way (activity, busy, give way, gap, duplicates) and the
owner-chat messages (3a): settings, the silence scheduler, the turn itself."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_config,
    fill_config_defaults,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.proactive_chat import (
    ProactiveChatScheduler,
    delivered,
    is_duplicate,
    normalize_text,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.proactive_chat_config import (
    DUPLICATE_NOTE,
    UNANSWERED_NOTE,
    default_proactive_chat,
    in_quiet_hours,
    next_window_start,
    proactive_chat_settings,
    validate_proactive_chat,
    window_state,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_queue import BridgeQueue
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_config import (
    default_schedule_settings,
    schedule_section,
    validate_schedule_section,
    validate_schedule_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import (
    ProactiveState,
    ScheduleScheduler,
    ScheduleState,
    ScheduleTargets,
)
from test_plugin_lifecycle import _load_plugin_module, _ready_plugin, _wait_for_request

OWNER = "qq_official:FriendMessage:owner-openid-secret"
GROUP = "qq_official:GroupMessage:group-openid-secret"
PLAN_ID = "0a1b2c3d"
HAPPY = "开心"
REPLY = f"[情绪:{HAPPY}]☆‹「おはよう」›\n\"早安，制作人。\""
OTHER_REPLY = f"[情绪:{HAPPY}]☆‹「こんにちは」›\n\"今天天气真好，出去走走吧。\""


def plan(**overrides):
    value = {
        "id": PLAN_ID,
        "name": "早安",
        "enabled": True,
        "kind": "daily",
        "time": "07:30",
        "weekdays": [],
        "jitter_minutes": 0,
        "target": {"type": "private"},
        "prompt": "向制作人道早安",
        "fallback_texts": ["制作人，早安。", "第二句"],
    }
    value.update(overrides)
    return value


def chat_config(**overrides):
    """A proactive chat section with no do-not-disturb and no schedule limits unless overridden."""
    value = default_proactive_chat()
    value["quiet_hours"] = {"start": "00:00", "end": "00:00"}
    value["active_windows"] = {"weekday": [], "weekend": []}
    value.update(overrides)
    return value


# ---- settings -----------------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        value = default_proactive_chat()
        self.assertEqual(value["silence_min"], 120)
        self.assertEqual(value["silence_max"], 240)
        self.assertEqual(value["followup_factor"], 1.5)
        self.assertEqual(value["max_unanswered"], 2)
        self.assertEqual(value["daily_limit"], 3)
        self.assertEqual(value["quiet_hours"], {"start": "00:00", "end": "08:00"})
        self.assertEqual(
            value["active_windows"],
            {
                "weekday": [{"start": "12:00", "end": "13:30"}, {"start": "16:30", "end": "23:30"}],
                "weekend": [{"start": "10:00", "end": "23:30"}],
            },
        )
        self.assertTrue(value["enabled"])
        self.assertIn("{{elapsed}}", value["prompt"])
        self.assertEqual(validate_proactive_chat(value), value)

    def test_bounds(self):
        ok = validate_proactive_chat
        self.assertEqual(ok({"silence_min": 30, "silence_max": 30})["silence_min"], 30)
        self.assertEqual(ok({"silence_min": 1440, "silence_max": 1440})["silence_max"], 1440)
        self.assertEqual(ok({"followup_factor": 4.0})["followup_factor"], 4.0)
        self.assertEqual(ok({"followup_factor": 1})["followup_factor"], 1.0)
        self.assertEqual(ok({"max_unanswered": 0})["max_unanswered"], 0)
        self.assertEqual(ok({"max_unanswered": 10})["max_unanswered"], 10)
        self.assertEqual(ok({"daily_limit": 0})["daily_limit"], 0)
        self.assertEqual(ok({"daily_limit": 20})["daily_limit"], 20)
        for bad in (
            {"silence_min": 29},
            {"silence_max": 1441},
            {"silence_min": 200, "silence_max": 100},
            {"silence_min": 60.5},
            {"followup_factor": 0.9},
            {"followup_factor": 4.1},
            {"followup_factor": float("nan")},
            {"followup_factor": True},
            {"max_unanswered": -1},
            {"max_unanswered": 11},
            {"daily_limit": 21},
            {"daily_limit": float("inf")},
            {"enabled": "yes"},
            {"quiet_hours": {"start": "24:00", "end": "08:00"}},
            {"quiet_hours": {"start": "00:00"}},
            {"quiet_hours": "x"},
            {"prompt": ""},
            {"prompt": "   "},
            {"prompt": "字" * 301},
            {"prompt": 5},
            {"active_windows": {"weekday": [{"start": "10:00", "end": "09:00"}], "weekend": []}},
            {"active_windows": {"weekday": [{"start": "10:00", "end": "10:00"}], "weekend": []}},
            {"active_windows": {"weekday": [{"start": "23:00", "end": "01:00"}], "weekend": []}},
            {"active_windows": {"weekday": [{"start": "10:00", "end": "11:00"}] * 7, "weekend": []}},
            {"active_windows": {"weekday": "x", "weekend": []}},
            {"active_windows": [1]},
        ):
            with self.subTest(bad=bad), self.assertRaises(EmotionConfigError):
                validate_proactive_chat(bad)
        self.assertEqual(len(ok({"active_windows": {"weekday": [{"start": "10:00", "end": "11:00"}] * 6, "weekend": []}})["active_windows"]["weekday"]), 6)
        with self.assertRaises(EmotionConfigError):
            validate_proactive_chat([])

    def test_equal_quiet_ends_mean_no_quiet_hours(self):
        value = validate_proactive_chat({"quiet_hours": {"start": "03:00", "end": "03:00"}})
        for minute in (0, 180, 600, 1439):
            self.assertFalse(in_quiet_hours(value, minute))

    def test_quiet_hours_cross_midnight(self):
        value = validate_proactive_chat({"quiet_hours": {"start": "23:00", "end": "07:00"}})
        self.assertTrue(in_quiet_hours(value, 23 * 60))
        self.assertTrue(in_quiet_hours(value, 0))
        self.assertTrue(in_quiet_hours(value, 6 * 60 + 59))
        self.assertFalse(in_quiet_hours(value, 7 * 60))
        self.assertFalse(in_quiet_hours(value, 22 * 60 + 59))
        plain = validate_proactive_chat({"quiet_hours": {"start": "01:00", "end": "05:00"}})
        self.assertTrue(in_quiet_hours(plain, 60))
        self.assertFalse(in_quiet_hours(plain, 300))

    def test_windows_are_sorted_and_split_by_weekday(self):
        value = validate_proactive_chat(
            {"active_windows": {"weekday": [{"start": "16:00", "end": "17:00"}, {"start": "09:00", "end": "10:00"}], "weekend": [{"start": "11:00", "end": "12:00"}]}}
        )
        self.assertEqual(value["active_windows"]["weekday"][0]["start"], "09:00")
        self.assertEqual(window_state(value, 0, 9 * 60), (True, 10 * 60))
        self.assertEqual(window_state(value, 0, 10 * 60), (False, None))
        self.assertEqual(window_state(value, 5, 11 * 60 + 30), (True, 12 * 60))
        self.assertEqual(window_state(value, 6, 9 * 60), (False, None))
        empty = validate_proactive_chat({"active_windows": {"weekday": [], "weekend": [{"start": "11:00", "end": "12:00"}]}})
        self.assertEqual(window_state(empty, 2, 3 * 60), (True, None))  # an empty list means no limit
        self.assertEqual(next_window_start(value, 0, 11 * 60), (0, 16 * 60, 60))
        self.assertEqual(next_window_start(value, 0, 18 * 60), (1, 9 * 60, 60))
        self.assertEqual(next_window_start(value, 4, 18 * 60), (1, 11 * 60, 60))  # Friday evening -> Saturday's window

    def test_schedule_settings_new_keys(self):
        defaults = default_schedule_settings()
        self.assertEqual((defaults["min_gap_minutes"], defaults["dedupe_ratio"]), (30, 0.8))
        self.assertEqual(validate_schedule_settings({"min_gap_minutes": 0})["min_gap_minutes"], 0)
        self.assertEqual(validate_schedule_settings({"min_gap_minutes": 240})["min_gap_minutes"], 240)
        self.assertEqual(validate_schedule_settings({"dedupe_ratio": 0.5})["dedupe_ratio"], 0.5)
        self.assertEqual(validate_schedule_settings({"dedupe_ratio": 1})["dedupe_ratio"], 1.0)
        for bad in (
            {"min_gap_minutes": -1},
            {"min_gap_minutes": 241},
            {"min_gap_minutes": 1.5},
            {"dedupe_ratio": 0.49},
            {"dedupe_ratio": 1.01},
            {"dedupe_ratio": float("nan")},
            {"dedupe_ratio": "0.8"},
            {"dedupe_ratio": True},
        ):
            with self.subTest(bad=bad), self.assertRaises(EmotionConfigError):
                validate_schedule_settings(bad)

    def test_an_old_schedule_section_gets_the_defaults(self):
        old = {"settings": {"enabled": True, "quiet_minutes": 7, "max_defer_minutes": 50}, "plans": []}
        section = validate_schedule_section(old)
        self.assertEqual(section["settings"]["quiet_minutes"], 7)
        self.assertEqual(section["settings"]["min_gap_minutes"], 30)
        self.assertEqual(section["proactive_chat"], default_proactive_chat())
        config = {"schedule": json.loads(json.dumps(old))}
        fill_config_defaults(config)
        self.assertEqual(config["schedule"]["proactive_chat"], default_proactive_chat())
        self.assertEqual(proactive_chat_settings({"schedule": old}), default_proactive_chat())
        self.assertEqual(proactive_chat_settings(None), default_proactive_chat())
        self.assertEqual(proactive_chat_settings({"schedule": {"proactive_chat": {"daily_limit": 99}}}), default_proactive_chat())

    def test_new_and_saved_configs_carry_the_section_and_presets_snapshot_it(self):
        config = default_config()
        self.assertEqual(config["schedule"]["proactive_chat"], default_proactive_chat())
        config["schedule"]["proactive_chat"]["daily_limit"] = 7
        self.assertEqual(config_presets.snapshot(config)["schedule"]["proactive_chat"]["daily_limit"], 7)

    def test_the_store_keeps_the_section(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            self.assertEqual(proactive_chat_settings(store.config), default_proactive_chat())


class TextTests(unittest.TestCase):
    def test_normalize_drops_whitespace_and_punctuation(self):
        self.assertEqual(normalize_text("早安，制作人。 \n你好！"), "早安制作人你好")
        self.assertEqual(normalize_text("…！？ "), "")

    def test_duplicate_threshold(self):
        self.assertTrue(is_duplicate("早安，制作人。", "早安 制作人！", 0.8))
        self.assertTrue(is_duplicate("早安，制作人。", "早安 制作人！", 1.0))  # same after normalising
        near = "今天天气真好，出去走走吧"
        other = "今天天气真好，出去走走吧呀"
        self.assertTrue(is_duplicate(near, other, 0.8))
        self.assertFalse(is_duplicate(near, other, 1.0))  # only an identical text is stopped at 1.0
        self.assertFalse(is_duplicate("早安", "晚安，好梦", 0.8))
        self.assertTrue(is_duplicate("早安制作人", "晚安制作人", 0.5))
        self.assertFalse(is_duplicate("早安制作人", "晚安制作人", 0.9))

    def test_empty_texts_are_never_duplicates(self):
        self.assertFalse(is_duplicate("", "早安", 0.5))
        self.assertFalse(is_duplicate("早安", "", 0.5))
        self.assertFalse(is_duplicate("。。。", "！！！", 0.5))

    def test_delivered(self):
        for value in ("sent", "fallback", "sent:fallback(duplicate)"):
            self.assertTrue(delivered(value))
        for value in ("failed:X", "skip:Y", "deferred:Requeued", None, 3):
            self.assertFalse(delivered(value))


# ---- targets, state, queue ----------------------------------------------------------------------


class TargetsAndStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.now = datetime(2026, 11, 2, 12, 0)

    def test_user_messages_bump_the_sequence_but_touch_does_not(self):
        targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.assertEqual(targets.user_seq(OWNER), 0)
        targets.record_owner(OWNER)
        self.assertEqual(targets.user_seq(OWNER), 1)
        targets.touch(OWNER)
        self.assertEqual(targets.user_seq(OWNER), 1)
        targets.record_group(GROUP)
        targets.record_group(GROUP)
        self.assertEqual(targets.user_seq(GROUP), 2)
        self.assertEqual(targets.owner_conversation, OWNER)

    def test_group_messages_are_not_owner_activity(self):
        targets = ScheduleTargets(self.data, clock=lambda: self.now)
        targets.record_owner(OWNER)
        targets._activity.clear()
        targets.record_group(GROUP)
        self.assertIsNone(targets.last_activity(OWNER))

    def test_last_text_is_per_session_memory_only(self):
        targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.assertEqual(targets.last_text(OWNER), "")
        targets.set_last_text(OWNER, "早安")
        targets.set_last_text(GROUP, "大家好")
        self.assertEqual(targets.last_text(OWNER), "早安")
        targets.set_last_text(OWNER, "晚安")
        self.assertEqual(targets.last_text(OWNER), "晚安")
        targets.record_owner(OWNER)
        self.assertEqual(ScheduleTargets(self.data).last_text(OWNER), "")

    def test_last_text_is_capped(self):
        targets = ScheduleTargets(self.data, clock=lambda: self.now)
        for number in range(520):
            targets.set_last_text(f"c{number}", "x")
        self.assertLessEqual(len(targets._last_text), 500)
        targets.set_last_text("c1", "y" * 1000)
        self.assertLessEqual(len(targets.last_text("c1")), 400)

    def test_state_unmark_is_persistent(self):
        state = ScheduleState(self.data)
        state.mark("a@1", self.now)
        self.assertTrue(ScheduleState(self.data).fired("a@1"))
        state.unmark("a@1")
        self.assertFalse(state.fired("a@1"))
        self.assertFalse(ScheduleState(self.data).fired("a@1"))
        state.unmark("never-marked")  # nothing happens

    def test_proactive_state_counts_per_day_and_persists(self):
        state = ProactiveState(self.data)
        self.assertIsNone(state.last_sent)
        self.assertEqual(state.chat_today(self.now), 0)
        state.record_chat_sent(self.now)
        state.record_chat_sent(self.now + timedelta(minutes=5))
        self.assertEqual(state.chat_today(self.now), 2)
        self.assertEqual(state.unanswered, 2)
        reloaded = ProactiveState(self.data)
        self.assertEqual(reloaded.chat_today(self.now), 2)
        self.assertEqual(reloaded.unanswered, 2)
        self.assertEqual(reloaded.last_sent, datetime(2026, 11, 2, 12, 5))
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(reloaded.chat_today(tomorrow), 0)  # local midnight resets the count
        reloaded.record_chat_sent(tomorrow)
        self.assertEqual(reloaded.chat_today(tomorrow), 1)
        self.assertEqual(reloaded.unanswered, 3)  # unanswered is not a daily count
        reloaded.clear_unanswered()
        self.assertEqual(ProactiveState(self.data).unanswered, 0)

    def test_record_sent_only_moves_the_last_time(self):
        state = ProactiveState(self.data)
        state.record_sent(self.now)
        self.assertEqual(state.chat_today(self.now), 0)
        self.assertEqual(state.unanswered, 0)
        self.assertEqual(ProactiveState(self.data).last_sent, self.now)

    def test_gap_remaining(self):
        state = ProactiveState(self.data)
        self.assertEqual(state.gap_remaining(self.now, 30), timedelta(0))
        state.record_sent(self.now - timedelta(minutes=10))
        self.assertEqual(state.gap_remaining(self.now, 30), timedelta(minutes=20))
        self.assertEqual(state.gap_remaining(self.now, 0), timedelta(0))
        self.assertEqual(state.gap_remaining(self.now + timedelta(minutes=30), 30), timedelta(0))

    def test_corrupt_or_odd_state_files_start_empty_with_a_warning(self):
        path = self.data / "proactive_state.json"
        for content in ("{not json", "[1, 2]", json.dumps({"chat": {"count": "x", "unanswered": -3}, "last_sent_at": 5})):
            path.write_text(content, encoding="utf-8")
            warnings = []
            logger = SimpleNamespace(warning=warnings.append)
            state = ProactiveState(self.data, logger=logger)
            self.assertEqual(state.chat_today(self.now), 0)
            self.assertEqual(state.unanswered, 0)
            self.assertIsNone(state.last_sent)
            if content != json.dumps({"chat": {"count": "x", "unanswered": -3}, "last_sent_at": 5}):
                self.assertEqual(len(warnings), 1)
                self.assertNotIn("not json", warnings[0])

    def test_state_write_failure_is_silent(self):
        state = ProactiveState(self.data / "missing-directory")
        state.record_chat_sent(self.now)  # cannot be saved; stays in memory
        self.assertEqual(state.chat_today(self.now), 1)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_and_user_waiting(self):
        queue = BridgeQueue()
        self.assertFalse(queue.busy)
        self.assertFalse(queue.user_waiting)
        proactive = queue.enqueue(None, "x", max_waiting=5, proactive=True)
        self.assertTrue(queue.busy)
        self.assertFalse(queue.user_waiting)
        person = queue.enqueue("u1", "hi", max_waiting=5)
        self.assertTrue(queue.user_waiting)
        self.assertFalse(proactive.waited)
        self.assertTrue(person.waited)
        queue.release(person)
        queue.release(proactive)
        self.assertFalse(queue.busy)

    async def test_an_active_entry_makes_it_busy_until_released(self):
        queue = BridgeQueue(poll_interval=0.001)
        entry = queue.enqueue("u1", "hi", max_waiting=5)
        await queue.wait_turn(entry, max_wait_seconds=5, dispatch=lambda current: object(), available=lambda: True)
        self.assertTrue(queue.busy)
        self.assertFalse(queue.user_waiting)
        queue.release(entry)
        self.assertFalse(queue.busy)


# ---- T1 scheduler: busy, gap, give way ---------------------------------------------------------


class T1Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.now = datetime(2026, 11, 2, 7, 0)
        self.targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.targets.record_owner(OWNER)
        self.targets._activity.clear()
        self.state = ScheduleState(self.data)
        self.proactive = ProactiveState(self.data)
        self.config = {"schedule": {"settings": default_schedule_settings(), "plans": [plan()]}}
        self.busy = None
        self.results = []
        self.calls = []
        self.logs = []

        class Logger:
            def info(inner, message):
                self.logs.append(message)

            def error(inner, message):
                self.logs.append(message)

        async def runner(the_plan, forced):
            self.calls.append((the_plan["id"], forced, self.now))
            return self.results.pop(0) if self.results else {"result": "sent"}

        self.scheduler = ScheduleScheduler(
            get_config=lambda: self.config,
            targets=self.targets,
            state=self.state,
            runner=runner,
            clock=lambda: self.now,
            logger=Logger(),
            busy_reason=lambda conversation: self.busy,
            proactive=self.proactive,
        )

    async def tick(self):
        tasks = self.scheduler.tick()
        if tasks:
            await asyncio.gather(*tasks)
        return tasks

    def at(self, hour, minute):
        self.now = datetime(2026, 11, 2, hour, minute)

    def settings(self, **values):
        self.config["schedule"]["settings"].update(values)

    def lines(self, text):
        return [line for line in self.logs if text in line]


class T1BusyTests(T1Base):
    async def test_a_busy_queue_or_a_send_in_progress_defers_then_sends(self):
        self.at(7, 30)
        self.busy = "Queue"
        self.assertEqual(await self.tick(), [])
        self.assertEqual(self.calls, [])
        self.busy = "Sending"
        self.assertEqual(await self.tick(), [])
        self.busy = None
        self.at(7, 31)
        self.assertEqual(len(await self.tick()), 1)
        self.assertEqual(self.calls[0][2], datetime(2026, 11, 2, 7, 31))

    async def test_a_busy_deferral_is_logged_once_and_distinct(self):
        self.at(7, 30)
        self.busy = "Queue"
        for _ in range(3):
            await self.tick()
        deferred = self.lines("result=defer:Busy")
        self.assertEqual(len(deferred), 1)
        self.assertIn("plan=0a1b2c3d", deferred[0])
        self.assertFalse(any("defer:Quiet" in line for line in self.logs))  # the old silent quiet deferral stays silent

    async def test_deferral_past_max_defer_is_abandoned_and_names_the_last_reason(self):
        self.settings(max_defer_minutes=20)
        self.at(7, 30)
        self.busy = "Queue"
        await self.tick()
        self.at(7, 51)
        self.assertEqual(await self.tick(), [])
        timeout = self.lines("skip:DeferTimeout")
        self.assertEqual(len(timeout), 1)
        self.assertIn("last=Busy", timeout[0])
        self.busy = None
        self.at(8, 30)
        self.assertEqual(await self.tick(), [])
        self.assertEqual(self.calls, [])

    async def test_busy_deferral_continues_past_the_catch_up_window(self):
        self.at(7, 30)
        self.busy = "Queue"
        await self.tick()
        self.at(7, 50)  # twenty minutes late, still inside max_defer 60
        self.assertEqual(await self.tick(), [])
        self.busy = None
        self.at(7, 55)
        self.assertEqual(len(await self.tick()), 1)

    async def test_forced_runs_ignore_busy(self):
        self.busy = "Queue"
        await self.scheduler.run_now(plan())
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1])


class T1GapTests(T1Base):
    async def test_the_minimum_gap_defers_and_the_log_names_it(self):
        self.proactive.record_sent(datetime(2026, 11, 2, 7, 15))
        self.at(7, 30)
        self.assertEqual(await self.tick(), [])
        self.assertEqual(len(self.lines("result=defer:Gap")), 1)
        self.at(7, 44)
        self.assertEqual(await self.tick(), [])
        self.at(7, 45)  # 30 minutes after 07:15
        self.assertEqual(len(await self.tick()), 1)

    async def test_gap_zero_never_defers(self):
        self.settings(min_gap_minutes=0)
        self.proactive.record_sent(datetime(2026, 11, 2, 7, 29))
        self.at(7, 30)
        self.assertEqual(len(await self.tick()), 1)

    async def test_gap_deferral_is_bounded_by_max_defer(self):
        self.settings(max_defer_minutes=10, min_gap_minutes=240)
        self.proactive.record_sent(datetime(2026, 11, 2, 7, 25))
        self.at(7, 30)
        await self.tick()
        self.at(7, 45)
        await self.tick()
        self.assertTrue(any("skip:DeferTimeout" in line and "last=Gap" in line for line in self.logs))
        self.assertEqual(self.calls, [])

    async def test_a_group_plan_is_held_by_the_gap_too(self):
        group_key = self.targets.record_group(GROUP)
        self.targets._activity.clear()
        self.config["schedule"]["plans"] = [plan(target={"type": "group", "key": group_key})]
        self.proactive.record_sent(datetime(2026, 11, 2, 7, 20))
        self.at(7, 30)
        self.assertEqual(await self.tick(), [])


class T1GiveWayTests(T1Base):
    async def requeue_then_quiet(self):
        """First run gives way (somebody started talking); the user's message is the activity."""
        self.results = [{"result": "deferred:Requeued"}]
        self.at(7, 30)
        await self.tick()
        self.targets._activity[OWNER] = datetime(2026, 11, 2, 7, 30)

    async def test_a_given_up_run_is_taken_back_and_sent_once_when_quiet(self):
        await self.requeue_then_quiet()
        self.assertEqual(len(self.calls), 1)
        key = f"{PLAN_ID}@2026-11-02T07:30"
        self.assertFalse(self.state.fired(key))
        self.assertFalse(ScheduleState(self.data).fired(key))  # not left marked on disk
        self.assertEqual(len(self.lines("result=defer:Requeued")), 0)  # logged by the runner / not by Busy
        self.at(7, 35)
        self.assertEqual(await self.tick(), [])  # still within the quiet time
        self.at(7, 41)
        self.assertEqual(len(await self.tick()), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.state.fired(key))
        self.at(7, 50)
        self.assertEqual(await self.tick(), [])  # sent exactly once
        self.assertEqual(len(self.calls), 2)

    async def test_a_given_up_private_plan_keeps_blocking_the_chat_until_it_goes(self):
        await self.requeue_then_quiet()
        self.assertTrue(self.scheduler.blocks_chat())
        self.at(7, 41)
        await self.tick()
        self.assertFalse(self.scheduler.blocks_chat())

    async def test_given_up_runs_stay_bounded_by_max_defer(self):
        self.settings(max_defer_minutes=15)
        await self.requeue_then_quiet()
        self.at(7, 50)
        self.targets._activity[OWNER] = datetime(2026, 11, 2, 7, 49)
        await self.tick()
        self.assertTrue(any("skip:DeferTimeout" in line for line in self.logs))
        self.assertEqual(len(self.calls), 1)

    async def test_a_send_that_started_stays_marked(self):
        self.at(7, 30)
        await self.tick()  # sent
        self.assertTrue(self.state.fired(f"{PLAN_ID}@2026-11-02T07:30"))
        self.assertFalse(self.scheduler.blocks_chat())


class T1ChatPriorityTests(T1Base):
    async def test_a_running_plan_or_a_private_deferral_blocks_the_chat(self):
        self.assertFalse(self.scheduler.blocks_chat())
        gate = asyncio.Event()

        async def slow(the_plan, forced):
            await gate.wait()
            return {"result": "sent"}

        self.scheduler._runner = slow
        self.at(7, 30)
        tasks = self.scheduler.tick()
        self.assertTrue(self.scheduler.blocks_chat())
        gate.set()
        await asyncio.gather(*tasks)
        self.assertFalse(self.scheduler.blocks_chat())

    async def test_a_private_plan_deferring_blocks_but_a_group_plan_does_not(self):
        self.targets._activity[OWNER] = datetime(2026, 11, 2, 7, 29)
        self.at(7, 30)
        await self.tick()
        self.assertTrue(self.scheduler.blocks_chat())
        # Now the same plan aimed at a group: its deferral does not hold the owner's chat back.
        key = self.targets.record_group(GROUP)
        self.targets._activity[GROUP] = datetime(2026, 11, 2, 7, 29)
        scheduler_dir = Path(self.directory.name) / "second"
        scheduler_dir.mkdir()
        second = ScheduleScheduler(
            get_config=lambda: {"schedule": {"settings": default_schedule_settings(), "plans": [plan(target={"type": "group", "key": key})]}},
            targets=self.targets,
            state=ScheduleState(scheduler_dir),
            runner=self.scheduler._runner,
            clock=lambda: self.now,
        )
        second.tick()
        self.assertFalse(second.blocks_chat())


# ---- 3a scheduler -------------------------------------------------------------------------------


class ChatBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.now = datetime(2026, 11, 2, 13, 0)  # a Monday
        self.targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.targets.record_owner(OWNER)
        self.targets._activity.clear()
        self.targets._chat_base = None  # nothing was said since start
        self.state = ProactiveState(self.data)
        self.config = {"schedule": {"settings": default_schedule_settings(), "plans": [], "proactive_chat": chat_config()}}
        self.rolls = []
        self.default_roll = 0.0
        self.calls = []
        self.result = {"result": "sent"}
        self.busy = None
        self.t1 = False
        self.logs = []

        class Logger:
            def info(inner, message):
                self.logs.append(message)

            def error(inner, message):
                self.logs.append(message)

        self.logger = Logger()

        async def runner(forced, elapsed, unanswered):
            self.calls.append((forced, elapsed, unanswered, self.now))
            result = self.result.pop(0) if isinstance(self.result, list) else self.result
            if result.get("result") == "sent":
                self.targets.touch(OWNER)  # what the real runner does when something went out
                self.targets.touch_chat(OWNER)
            return result

        self.runner = runner
        self.scheduler = self.make()

    def rng(self):
        self.rng_calls = getattr(self, "rng_calls", 0) + 1
        return self.rolls.pop(0) if self.rolls else self.default_roll

    def make(self):
        return ProactiveChatScheduler(
            get_config=lambda: self.config,
            targets=self.targets,
            state=self.state,
            runner=self.runner,
            clock=lambda: self.now,
            rng=self.rng,
            logger=self.logger,
            busy_reason=lambda conversation: self.busy,
            t1_blocking=lambda: self.t1,
        )

    def chat(self, **values):
        self.config["schedule"]["proactive_chat"].update(values)

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)

    async def tick(self):
        task = self.scheduler.tick()
        if task is not None:
            await task
        return task

    def reason(self):
        return self.scheduler.snapshot()["reason"]

    def lines(self, text="ProactiveChat"):
        return [line for line in self.logs if text in line]


class SilenceTests(ChatBase):
    async def test_the_silence_target_is_drawn_in_range_from_the_start(self):
        self.default_roll = 0.5  # 120 + 0.5 * 120 = 180 minutes
        self.advance(179)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Waiting")
        self.advance(1)
        self.assertIsNotNone(await self.tick())
        self.assertEqual(self.calls[0][1:3], (180, 0))
        self.assertFalse(self.calls[0][0])

    async def test_the_shortest_roll_gives_the_minimum(self):
        self.default_roll = 0.0
        self.advance(119)
        self.assertIsNone(await self.tick())
        self.advance(1)
        self.assertIsNotNone(await self.tick())

    async def test_the_longest_roll_gives_the_maximum(self):
        self.default_roll = 0.999999
        self.advance(239)
        self.assertIsNone(await self.tick())
        self.advance(1)
        self.assertIsNotNone(await self.tick())

    async def test_activity_redraws_the_target(self):
        self.advance(100)
        self.targets.touch(OWNER)  # a reply finished at +100
        self.targets.touch_chat(OWNER)
        self.advance(119)
        self.assertIsNone(await self.tick())
        self.advance(1)
        self.assertIsNotNone(await self.tick())

    async def test_the_target_is_not_redrawn_while_nothing_happens(self):
        self.rolls = [0.5]
        self.advance(10)
        await self.tick()
        self.advance(10)
        await self.tick()
        self.assertEqual(self.rng_calls, 1)

    async def test_the_message_starts_from_a_restart_not_from_old_activity(self):
        # Nothing was said since start: even though the last real activity was long ago, nothing is sent at once.
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Waiting")
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_disabled_and_no_owner(self):
        self.chat(enabled=False)
        self.advance(500)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Disabled")
        self.chat(enabled=True)
        empty = Path(self.directory.name) / "other"
        empty.mkdir()
        self.targets = ScheduleTargets(empty, clock=lambda: self.now)
        self.scheduler = self.make()
        self.assertIsNone(await self.tick())
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "NoOwner")
        self.assertFalse(snapshot["owner_known"])
        self.assertEqual(self.calls, [])

    async def test_group_activity_is_not_private_activity(self):
        self.targets.record_group(GROUP)
        self.advance(120)
        self.targets.record_group(GROUP)  # people keep chatting in the group
        self.assertIsNotNone(await self.tick())  # the private silence still counts

    async def test_the_prompt_arguments_carry_elapsed_minutes_and_unanswered(self):
        self.default_roll = 0.0
        self.advance(130)
        await self.tick()
        self.assertEqual(self.calls[0][1], 130)


class UnansweredTests(ChatBase):
    async def send_once(self, wait):
        self.advance(wait)
        task = await self.tick()
        self.assertIsNotNone(task, f"nothing sent after {wait} minutes")

    async def test_each_unanswered_message_stretches_the_next_silence(self):
        self.chat(max_unanswered=5)
        await self.send_once(120)  # 120
        self.assertEqual(self.state.unanswered, 1)
        self.advance(179)  # 120 * 1.5 = 180
        self.assertIsNone(await self.tick())
        self.advance(1)
        self.assertIsNotNone(await self.tick())
        self.assertEqual(self.state.unanswered, 2)
        self.advance(269)  # 120 * 1.5 ** 2 = 270
        self.assertIsNone(await self.tick())
        self.advance(1)
        self.assertIsNotNone(await self.tick())
        self.assertEqual([call[2] for call in self.calls], [0, 1, 2])

    async def test_it_stops_when_the_followups_are_used_up_and_the_owner_speaking_restarts_it(self):
        self.chat(max_unanswered=1, followup_factor=1.0, daily_limit=20)
        await self.send_once(120)
        await self.send_once(120)  # the one allowed follow-up
        self.assertEqual(self.state.unanswered, 2)
        self.advance(1000)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "UnansweredLimit")
        self.assertEqual(len(self.calls), 2)
        self.targets.record_owner(OWNER)  # the owner finally writes
        self.assertEqual(self.state.unanswered, 2)
        self.assertEqual(self.reason(), "Waiting")  # evaluating notices and clears it
        self.assertEqual(self.state.unanswered, 0)
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_zero_followups_means_one_message_then_quiet(self):
        self.chat(max_unanswered=0, daily_limit=20)
        await self.send_once(120)
        self.advance(5000)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "UnansweredLimit")

    async def test_the_limit_is_logged_once(self):
        self.chat(max_unanswered=0, daily_limit=20)
        await self.send_once(120)
        for _ in range(4):
            self.advance(300)
            await self.tick()
        self.assertEqual(len(self.lines("skip:UnansweredLimit")), 1)

    async def test_the_owners_message_clears_the_count_even_after_a_restart(self):
        self.state.record_chat_sent(self.now)
        self.state.record_chat_sent(self.now)
        self.scheduler = self.make()
        self.assertEqual(self.state.unanswered, 2)
        self.targets.record_owner(OWNER)
        await self.tick()
        self.assertEqual(self.state.unanswered, 0)


class DailyLimitTests(ChatBase):
    async def test_the_limit_counts_sent_messages_and_resets_at_midnight(self):
        self.chat(daily_limit=2, max_unanswered=10, followup_factor=1.0)
        for _ in range(2):
            self.advance(120)
            self.assertIsNotNone(await self.tick())
        self.advance(100)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "DailyLimit")
        self.assertEqual(self.scheduler.snapshot()["today"], 2)
        self.assertEqual(len(self.lines("skip:DailyLimit")), 1)
        self.now = datetime(2026, 11, 3, 0, 5)
        self.assertEqual(self.scheduler.snapshot()["today"], 0)
        self.targets.touch(OWNER)
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_zero_means_never(self):
        self.chat(daily_limit=0)
        self.advance(3000)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "DailyLimit")

    async def test_failures_do_not_count(self):
        self.result = {"result": "failed:StFailed"}
        self.advance(120)
        await self.tick()
        self.assertEqual(self.state.chat_today(self.now), 0)
        self.assertEqual(self.state.unanswered, 0)

    async def test_the_count_survives_a_restart(self):
        self.chat(daily_limit=1)
        self.advance(120)
        await self.tick()
        self.scheduler = self.make()
        self.state = ProactiveState(self.data)
        self.scheduler = self.make()
        self.advance(300)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "DailyLimit")


class QuietAndWindowTests(ChatBase):
    async def test_quiet_hours_across_midnight_hold_it_until_they_end(self):
        self.chat(quiet_hours={"start": "00:00", "end": "08:00"})
        self.now = datetime(2026, 11, 3, 0, 30)
        self.targets.touch(OWNER)
        self.advance(120)  # 02:30: due, inside the quiet hours
        self.assertIsNone(await self.tick())
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "QuietHours")
        self.assertEqual(snapshot["next_at"], "2026-11-03T08:00")
        self.assertEqual(len(self.lines("skip:QuietHours")), 1)
        self.now = datetime(2026, 11, 3, 7, 59)
        self.assertIsNone(await self.tick())
        self.now = datetime(2026, 11, 3, 8, 0)
        self.assertIsNotNone(await self.tick())

    async def test_quiet_hours_that_cross_midnight_in_the_evening(self):
        self.chat(quiet_hours={"start": "23:00", "end": "07:00"})
        self.now = datetime(2026, 11, 2, 21, 0)
        self.targets.touch(OWNER)
        self.advance(120)  # 23:00
        self.assertIsNone(await self.tick())
        self.assertEqual(self.scheduler.snapshot()["next_at"], "2026-11-03T07:00")

    async def test_outside_the_windows_it_waits_for_the_next_one_plus_a_random_delay(self):
        self.chat(active_windows={"weekday": [{"start": "12:00", "end": "13:30"}, {"start": "16:30", "end": "23:30"}], "weekend": []})
        self.rolls = [0.0, 0.4]  # silence 120 minutes, then a delay of 0.4 * 15 = 6 minutes
        self.advance(120)  # 15:00, between the two windows
        self.assertIsNone(await self.tick())
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "OutsideWindow")
        self.assertEqual(snapshot["next_at"], "2026-11-02T16:36")
        self.assertEqual(len(self.lines("skip:OutsideWindow")), 1)
        self.now = datetime(2026, 11, 2, 16, 30)
        self.assertIsNone(await self.tick())  # the window is open but the delay has not passed
        self.now = datetime(2026, 11, 2, 16, 35)
        self.assertIsNone(await self.tick())
        self.now = datetime(2026, 11, 2, 16, 36)
        self.assertIsNotNone(await self.tick())

    async def test_inside_a_window_it_goes_at_once(self):
        self.chat(active_windows={"weekday": [{"start": "12:00", "end": "23:30"}], "weekend": []})
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_weekend_uses_its_own_windows(self):
        self.chat(active_windows={"weekday": [{"start": "20:00", "end": "21:00"}], "weekend": [{"start": "10:00", "end": "23:30"}]})
        self.now = datetime(2026, 11, 7, 12, 0)  # a Saturday
        self.scheduler = self.make()
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_an_empty_window_list_means_no_limit(self):
        self.chat(active_windows={"weekday": [], "weekend": [{"start": "10:00", "end": "11:00"}]})
        self.now = datetime(2026, 11, 2, 3, 0)
        self.scheduler = self.make()
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_the_delay_waits_into_tomorrows_window_when_none_is_left_today(self):
        self.chat(active_windows={"weekday": [{"start": "09:00", "end": "10:00"}], "weekend": []})
        self.rolls = [0.0, 0.5]
        self.advance(120)  # 15:00, today's window is over
        self.assertIsNone(await self.tick())
        self.assertEqual(self.scheduler.snapshot()["next_at"], "2026-11-03T09:07")

    async def test_quiet_hours_and_windows_both_apply(self):
        self.chat(
            quiet_hours={"start": "00:00", "end": "08:00"},
            active_windows={"weekday": [{"start": "10:00", "end": "23:30"}], "weekend": []},
        )
        self.rolls = [0.0, 0.0, 0.0]
        self.now = datetime(2026, 11, 3, 0, 30)
        self.targets.touch(OWNER)
        self.advance(120)  # 02:30: quiet
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "QuietHours")
        self.now = datetime(2026, 11, 3, 8, 0)  # quiet is over but the window opens at 10:00
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "OutsideWindow")
        self.now = datetime(2026, 11, 3, 10, 0)
        self.assertIsNotNone(await self.tick())


class BlockerTests(ChatBase):
    async def test_a_busy_queue_or_send_holds_it_without_new_draws_then_it_goes(self):
        self.advance(120)
        self.busy = "Queue"
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Busy")
        self.assertEqual(len(self.lines("skip:Busy")), 1)
        self.busy = None
        self.assertIsNotNone(await self.tick())

    async def test_a_conversation_in_progress_holds_it(self):
        self.config["schedule"]["settings"]["quiet_minutes"] = 120
        self.chat(silence_min=30, silence_max=30)
        self.advance(30)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Conversing")
        self.advance(90)
        self.assertIsNotNone(await self.tick())

    async def test_the_minimum_gap_holds_it(self):
        self.advance(120)
        self.state.record_sent(self.now - timedelta(minutes=10))
        self.assertIsNone(await self.tick())
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "Gap")
        self.assertEqual(snapshot["next_at"], "2026-11-02T15:20")
        self.advance(20)
        self.assertIsNotNone(await self.tick())

    async def test_t1_goes_first(self):
        self.advance(120)
        self.t1 = True
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "T1Priority")
        self.assertEqual(len(self.lines("skip:T1Priority")), 1)
        self.t1 = False
        self.assertIsNotNone(await self.tick())
        self.assertEqual(len(self.calls), 1)

    async def test_waiting_and_ready_are_not_logged(self):
        self.advance(10)
        await self.tick()
        self.advance(110)
        await self.tick()
        self.assertEqual(self.lines("skip:"), [])


class FailureTests(ChatBase):
    async def test_a_failure_retries_in_thirty_minutes_without_a_new_draw(self):
        self.result = [{"result": "failed:StFailed"}, {"result": "sent"}]
        self.advance(120)
        self.assertIsNotNone(await self.tick())
        draws = self.rng_calls
        self.assertEqual(self.reason(), "Retry")
        self.advance(5)
        self.assertIsNone(await self.tick())
        self.assertEqual(len(self.lines("skip:Retry")), 1)
        self.advance(24)
        self.assertIsNone(await self.tick())
        self.assertEqual(len(self.lines("skip:Retry")), 1)
        self.assertEqual(self.rng_calls, draws)  # the retry does not draw a new silence target
        self.advance(1)
        self.assertIsNotNone(await self.tick())
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.state.chat_today(self.now), 1)

    async def test_failures_neither_count_nor_make_an_unanswered_one(self):
        for message in ("failed:Duplicate", "skip:Whatever", "failed:QueueFull"):
            self.result = {"result": message}
            self.scheduler = self.make()
            self.state = ProactiveState(self.data)
            self.scheduler = self.make()
            self.now += timedelta(minutes=120)
            self.assertIsNotNone(await self.tick())
            self.assertEqual(self.state.unanswered, 0)
            self.assertEqual(self.state.chat_today(self.now), 0)

    async def test_giving_way_is_not_a_failure(self):
        self.result = {"result": "deferred:Requeued"}
        self.advance(120)
        await self.tick()
        self.assertNotEqual(self.reason(), "Retry")

    async def test_a_runner_exception_is_logged_and_retried(self):
        async def broken(forced, elapsed, unanswered):
            raise RuntimeError("secret-detail")

        self.scheduler._runner = broken
        self.advance(120)
        await self.tick()
        self.assertEqual(self.reason(), "Retry")
        self.assertTrue(any("failed:RuntimeError" in line for line in self.logs))
        self.assertFalse(any("secret-detail" in line for line in self.logs))

    async def test_it_never_runs_twice_at_once(self):
        gate = asyncio.Event()

        async def slow(forced, elapsed, unanswered):
            self.calls.append((forced, elapsed, unanswered, self.now))
            await gate.wait()
            return {"result": "sent"}

        self.scheduler._runner = slow
        self.advance(120)
        task = self.scheduler.tick()
        self.assertIsNotNone(task)
        self.assertTrue(self.scheduler.running)
        self.assertIsNone(self.scheduler.tick())
        self.assertEqual(self.scheduler.snapshot()["reason"], "Running")
        gate.set()
        await task
        self.assertFalse(self.scheduler.running)
        self.assertEqual(len(self.calls), 1)


class RunNowTests(ChatBase):
    async def test_the_test_button_ignores_time_limits_and_silence_but_does_not_count(self):
        self.chat(daily_limit=0, enabled=True)
        result = await self.scheduler.run_now()
        self.assertEqual(result, {"result": "sent"})
        self.assertTrue(self.calls[0][0])
        self.assertEqual(self.state.chat_today(self.now), 0)
        self.assertEqual(self.state.unanswered, 0)

    async def test_the_elapsed_time_is_from_the_last_activity(self):
        self.targets._chat_base = self.now - timedelta(minutes=95)
        await self.scheduler.run_now()
        self.assertEqual(self.calls[0][1], 95)

    async def test_a_second_run_is_refused_while_one_runs(self):
        gate = asyncio.Event()

        async def slow(forced, elapsed, unanswered):
            await gate.wait()
            return {"result": "sent"}

        self.scheduler._runner = slow
        first = asyncio.create_task(self.scheduler.run_now())
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            await self.scheduler.run_now()
        gate.set()
        await first


class SnapshotTests(ChatBase):
    async def test_the_console_view(self):
        self.advance(5)
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "Waiting")
        self.assertEqual(snapshot["next_at"], "2026-11-02T15:00")
        self.assertEqual((snapshot["today"], snapshot["daily_limit"]), (0, 3))
        self.assertEqual((snapshot["unanswered"], snapshot["max_unanswered"]), (0, 2))
        self.assertTrue(snapshot["owner_known"])
        self.assertFalse(snapshot["running"])

    async def test_loop_starts_and_stops_cleanly(self):
        self.scheduler.start(interval=0.01)
        await asyncio.sleep(0.05)
        await self.scheduler.stop()
        await self.scheduler.stop()


# ---- the plugin: activity, give way, duplicates, the chat turn ---------------------------------


class PluginBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.now = datetime(2026, 11, 2, 7, 30)
        self.plugin = _ready_plugin(self.module)
        self.core = self.plugin._core
        self.core._emotion_store = ConfigStore(self.data)
        self.core._schedule_now = lambda: self.now
        self.core._schedule_random = lambda: 0.0
        self.core._schedule_choice = lambda items: items[0]
        self.targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.targets.record_owner(OWNER)
        self.core._schedule_targets = self.targets
        self.core._proactive_state = ProactiveState(self.data)
        self.sends = []

        async def send_message(umo, chain):
            self.sends.append((umo, type(chain.chain[0]).__name__, chain.chain))
            return True

        self.plugin.context = SimpleNamespace(send_message=send_message)
        self.moods = []

        def record(parsed, *, owner):
            self.moods.append(owner)

        self.core._record_mood = record
        self.attempts = 0
        self.module.test_logger.infos.clear()

    def owner_event(self):
        """A private message of the owner (the fake event has no session id of its own)."""
        from test_plugin_lifecycle import _bridge_event

        event = _bridge_event(self.module, "hi")
        event.unified_msg_origin = OWNER
        return event

    def texts(self):
        return [chain[0].text for _umo, kind, chain in self.sends if kind == "FakePlain"]

    async def run_turn(self, runner, *, reply=REPLY, st="reply"):
        """Run a proactive coroutine and answer its ST request (``st=None``: nobody answers)."""
        task = asyncio.create_task(runner)
        manager = self.core._request_state_manager
        request = None
        if st is not None:
            request = await _wait_for_request(manager)
            self.attempts += 1
            if st == "reply":
                manager.claim_request(request.request_id, f"turn-{self.attempts}")
                manager.mark_generation_started(request.request_id)
                manager.confirm_generation_finished(request.request_id)
                manager.complete_request(request.request_id, reply)
            else:
                manager.fail_request(request.request_id, "generation_failed")
        result = await asyncio.wait_for(task, 10)
        return result, request


class ActivityTests(PluginBase):
    async def test_a_finished_proactive_message_is_activity_and_starts_the_gap(self):
        self.targets._activity.clear()
        later = datetime(2026, 11, 2, 7, 40)
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.targets.last_activity(OWNER), self.now)
        self.assertEqual(self.core._proactive_state.last_sent, self.now)
        self.assertGreater(later, self.now)

    async def test_a_failed_proactive_message_is_not_activity(self):
        self.targets._activity.clear()
        self.plugin.context = SimpleNamespace(send_message=self.failing_send)
        result, _ = await self.run_turn(self.core._run_proactive(plan(fallback_texts=[]), False), st="fail")
        self.assertTrue(result["result"].startswith("skip:") or result["result"].startswith("failed:"))
        self.assertIsNone(self.targets.last_activity(OWNER))
        self.assertIsNone(self.core._proactive_state.last_sent)

    async def failing_send(self, umo, chain):
        raise RuntimeError("down")

    async def test_a_reply_that_finished_is_activity_after_the_last_batch(self):
        self.targets._activity.clear()
        events = []
        original_touch = self.targets.touch

        def touch(conversation):
            events.append("touch")
            original_touch(conversation)

        self.targets.touch = touch
        event = self.owner_event()
        batches = []

        async def consume():
            async for item in self.plugin.stbridge_private_message(event):
                events.append("item")
                batches.append(item)
                events.append("sent")

        task = asyncio.create_task(consume())
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, REPLY)
        await task
        self.assertEqual(events[-1], "touch")
        self.assertEqual(events[:-1], ["item", "sent"])
        self.assertEqual(self.targets.last_activity(self.targets.owner_conversation), self.now)
        self.assertEqual(self.core._sending, {})

    async def test_split_replies_count_as_activity_only_when_the_last_group_is_out(self):
        from test_plugin_lifecycle import RecordingTts

        self.core._tts = RecordingTts(split_messages=True, split_interval=0, split_max=3)
        self.targets._activity.clear()
        events = []
        original_touch = self.targets.touch
        self.targets.touch = lambda conversation: (events.append("touch"), original_touch(conversation))

        async def pause(seconds):
            events.append("pause")

        self.core._split_pause = pause
        event = self.owner_event()

        async def consume():
            async for _item in self.plugin.stbridge_private_message(event):
                events.append("batch")

        task = asyncio.create_task(consume())
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, '[情绪:开心]☆‹「一」›"甲"[情绪:开心]☆‹「二」›"乙"')
        await task
        self.assertEqual(events.count("batch"), 2)
        self.assertEqual(events[-1], "touch")
        self.assertEqual(events.count("touch"), 1)

    async def test_the_session_counts_as_being_sent_to_while_a_reply_is_handled(self):
        event = self.owner_event()
        seen = []

        async def consume():
            async for _item in self.plugin.stbridge_private_message(event):
                seen.append(dict(self.core._sending))
                seen.append(self.core._proactive_busy_reason(self.targets.owner_conversation))

        task = asyncio.create_task(consume())
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, REPLY)
        await task
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(list(seen[0].values()), [1])
        self.assertIsNotNone(seen[1])  # the queue slot is held until the reply went out
        self.assertEqual(self.core._sending, {})
        self.assertIsNone(self.core._proactive_busy_reason(self.targets.owner_conversation))

    async def test_a_proactive_send_counts_as_sending_then_clears(self):
        seen = []
        original = self.core._proactive_send_chain

        async def spy(conversation, segments):
            seen.append(self.core._proactive_busy_reason(conversation))
            await original(conversation, segments)

        self.core._proactive_send_chain = spy
        await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertTrue(seen)
        self.assertTrue(all(reason is not None for reason in seen))
        self.assertEqual(self.core._sending, {})

    async def test_busy_reasons(self):
        self.assertIsNone(self.core._proactive_busy_reason(OWNER))
        self.core._sending_begin(OWNER)
        self.assertEqual(self.core._proactive_busy_reason(OWNER), "Sending")
        self.assertIsNone(self.core._proactive_busy_reason(GROUP))
        self.core._sending_end(OWNER)
        self.core._sending_end(OWNER)  # extra ends are harmless
        self.assertEqual(self.core._sending, {})
        entry = self.core._queue.enqueue("u1", "hi", max_waiting=5)
        self.assertEqual(self.core._proactive_busy_reason(GROUP), "Queue")
        self.core._queue.release(entry)
        self.core._chat_scheduler = SimpleNamespace(running=True)
        self.assertEqual(self.core._proactive_busy_reason(OWNER), "Chat")

    async def test_a_song_on_its_way_counts_as_being_sent_to(self):
        gate = asyncio.Event()

        async def delivery(*args):
            await gate.wait()

        self.core._run_song_delivery = delivery
        self.core._start_song_delivery(OWNER, "key-1", "name", {"name": "歌"}, None, "owner", None, None)
        await asyncio.sleep(0)
        self.assertEqual(self.core._proactive_busy_reason(OWNER), "Sending")
        gate.set()
        await asyncio.sleep(0.01)
        self.assertIsNone(self.core._proactive_busy_reason(OWNER))
        self.assertEqual(self.targets.last_activity(OWNER), self.now)


class GiveWayTests(PluginBase):
    async def occupy_st(self):
        """ST is busy with something else: the proactive turn waits at the head of the queue."""
        manager = self.core._request_state_manager
        return manager.create_request("someone else", timeout_seconds=60)

    async def test_a_new_message_in_the_session_makes_it_give_way_without_asking_st(self):
        await self.occupy_st()
        task = asyncio.create_task(self.core._run_proactive(plan(), False))
        await asyncio.sleep(0.3)
        self.assertFalse(task.done())
        self.targets.record_owner(OWNER)  # the user speaks while it waits
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result["result"], "deferred:Requeued")
        self.assertEqual(self.sends, [])
        self.assertFalse(self.core._queue.busy)  # it left the queue
        self.assertTrue(any("result=deferred:Requeued" in line for line in self.module.test_logger.infos))
        self.assertIsNone(self.core._proactive_state.last_sent)

    async def test_a_person_waiting_behind_it_makes_it_give_way(self):
        await self.occupy_st()
        task = asyncio.create_task(self.core._run_proactive(plan(), False))
        await asyncio.sleep(0.3)
        person = self.core._queue.enqueue("u1", "hi", max_waiting=5)
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result["result"], "deferred:Requeued")
        self.assertTrue(self.core._queue.user_waiting)
        self.core._queue.release(person)

    async def test_nothing_changes_for_a_turn_nobody_interrupts(self):
        result, request = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(request.kind, "proactive")

    async def test_a_forced_run_does_not_give_way(self):
        await self.occupy_st()
        task = asyncio.create_task(self.core._run_proactive(plan(), True))
        await asyncio.sleep(0.3)
        self.targets.record_owner(OWNER)
        await asyncio.sleep(0.3)
        self.assertFalse(task.done())  # still waiting for ST, not given up
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_a_user_turn_in_front_does_not_make_it_give_way_by_itself(self):
        # A person's turn that is already being served is not "waiting": the proactive one simply waits its turn.
        first = self.core._queue.enqueue("u1", "hi", max_waiting=5)
        await self.core._queue.wait_turn(first, max_wait_seconds=5, dispatch=lambda current: object(), available=lambda: True)
        task = asyncio.create_task(self.core._run_proactive(plan(), False))
        await asyncio.sleep(0.3)
        self.assertFalse(task.done())
        self.core._queue.release(first)
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, REPLY)
        result = await asyncio.wait_for(task, 10)
        self.assertEqual(result["result"], "sent")


class DuplicateTests(PluginBase):
    async def test_a_plan_sends_its_fallback_when_st_repeats_the_last_message(self):
        self.targets.set_last_text(OWNER, "早安，制作人。")
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent:fallback(duplicate)")
        self.assertEqual(self.texts(), ["制作人，早安。"])
        self.assertEqual(self.moods, [])  # the dropped reply is not a mood
        self.assertEqual(result["voice"], "skip:Fallback")
        self.assertTrue(any("result=sent:fallback(duplicate)" in line for line in self.module.test_logger.infos))
        self.assertEqual(self.core._proactive_state.last_sent, self.now)  # the fallback went out

    async def test_the_fallback_becomes_the_last_message(self):
        self.targets.set_last_text(OWNER, "早安，制作人。")
        await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(self.targets.last_text(OWNER), "制作人，早安。")

    async def test_a_duplicate_without_fallback_text_sends_nothing(self):
        self.targets.set_last_text(OWNER, "早安，制作人。")
        result, _ = await self.run_turn(self.core._run_proactive(plan(fallback_texts=[]), False))
        self.assertEqual(result["result"], "skip:NoFallbackText")
        self.assertEqual(self.sends, [])

    async def test_a_new_text_goes_out_and_is_remembered_and_recorded(self):
        self.targets.set_last_text(OWNER, "完全不同的一句话，关于午饭")
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.texts(), ["早安，制作人。"])
        self.assertEqual(self.targets.last_text(OWNER), "早安，制作人。")
        self.assertEqual(self.moods, [True])

    async def test_a_group_plan_records_a_group_mood_and_compares_with_that_groups_text(self):
        key = self.targets.record_group(GROUP)
        self.targets.set_last_text(OWNER, "早安，制作人。")  # the owner's chat, not the group's
        result, _ = await self.run_turn(self.core._run_proactive(plan(target={"type": "group", "key": key}), False))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.moods, [False])
        self.assertEqual(self.targets.last_text(GROUP), "早安，制作人。")

    async def test_the_threshold_comes_from_the_settings(self):
        self.targets.set_last_text(OWNER, "早安，制作人啊。")  # 5 of 6 characters shared: ratio 0.91
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent:fallback(duplicate)")
        # At 1.0 only an identical text is stopped.
        def update(config):
            config["schedule"]["settings"]["dedupe_ratio"] = 1.0

        await self.core._emotion_store.update(update)
        self.sends.clear()
        self.targets.set_last_text(OWNER, "早安，制作人啊。")
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent")
        self.sends.clear()
        self.targets.set_last_text(OWNER, "早安制作人")
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent:fallback(duplicate)")  # identical after dropping punctuation

    async def test_a_forced_run_is_not_filtered(self):
        self.targets.set_last_text(OWNER, "早安，制作人。")
        result, _ = await self.run_turn(self.core._run_proactive(plan(), True))
        self.assertEqual(result["result"], "sent")

    async def test_a_normal_reply_becomes_the_last_message_to_compare_with(self):
        event = self.owner_event()

        async def consume():
            return [item async for item in self.plugin.stbridge_private_message(event)]

        task = asyncio.create_task(consume())
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, REPLY)
        await task
        self.assertEqual(self.targets.last_text(self.targets.owner_conversation), "早安，制作人。")
        # A scheduled message that says the same thing is now a duplicate.
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent:fallback(duplicate)")


class ChatTurnTests(PluginBase):
    async def test_the_request_is_proactive_with_the_prompt_and_the_fixed_sentence(self):
        result, request = await self.run_turn(self.core._run_proactive_chat(False, 200, 0))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(request.kind, "proactive")
        self.assertIn("现在是 2026-11-02 星期一 07:30", request.content)
        self.assertIn("上次说话是 3 小时 前", request.content)
        self.assertNotIn("{{elapsed}}", request.content)
        self.assertNotIn(UNANSWERED_NOTE, request.content)
        self.assertTrue(request.content.endswith(f"{DUPLICATE_NOTE}）"))

    async def test_an_unanswered_message_adds_the_sentence(self):
        _, request = await self.run_turn(self.core._run_proactive_chat(False, 90, 1))
        self.assertIn(UNANSWERED_NOTE, request.content)
        self.assertLess(request.content.index(UNANSWERED_NOTE), request.content.index(DUPLICATE_NOTE))
        self.assertIn("上次说话是 1 小时 前", request.content)

    async def test_the_prompt_is_the_one_from_the_settings(self):
        def update(config):
            config["schedule"]["proactive_chat"]["prompt"] = "想他了。距离上次 {{elapsed}}。"

        await self.core._emotion_store.update(update)
        _, request = await self.run_turn(self.core._run_proactive_chat(False, 5, 0))
        self.assertIn("想他了。距离上次 5 分钟。", request.content)

    async def test_it_goes_to_the_owners_private_chat_with_the_owners_mood(self):
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 120, 0))
        self.assertEqual(result["result"], "sent")
        self.assertEqual({umo for umo, _kind, _chain in self.sends}, {OWNER})
        self.assertEqual(self.moods, [True])
        self.assertTrue(any(line.startswith("[STBridge] ProactiveChat result=sent") for line in self.module.test_logger.infos))
        self.assertFalse(any("Schedule plan=" in line for line in self.module.test_logger.infos))

    async def test_a_duplicate_is_dropped_without_a_fallback_or_a_mood(self):
        self.targets.set_last_text(OWNER, "早安，制作人。")
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 120, 0))
        self.assertEqual(result["result"], "failed:Duplicate")
        self.assertEqual(self.sends, [])
        self.assertEqual(self.moods, [])
        self.assertIsNone(self.core._proactive_state.last_sent)
        self.assertEqual(self.core._proactive_state.chat_today(self.now), 0)

    async def test_failures_say_why(self):
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 120, 0), st="fail")
        self.assertEqual(result["result"], "failed:StFailed")
        self.assertEqual(self.sends, [])
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 120, 0), reply="没有格式")
        self.assertEqual(result["result"], "failed:InvalidReply")

    async def test_st_unavailable_is_a_failure_not_a_fallback(self):
        self.core._recovery_gate = None
        result = await self.core._run_proactive_chat(False, 120, 0)
        self.assertEqual(result["result"], "failed:StUnavailable")
        self.assertEqual(self.sends, [])

    async def test_a_send_failure_is_a_failure(self):
        async def failing(umo, chain):
            raise RuntimeError("down")

        self.plugin.context = SimpleNamespace(send_message=failing)
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 120, 0))
        self.assertTrue(result["result"].startswith("failed:"))
        self.assertIsNone(self.core._proactive_state.last_sent)

    async def test_no_owner_session_is_skipped(self):
        empty = self.data / "empty"
        empty.mkdir()
        self.core._schedule_targets = ScheduleTargets(empty, clock=lambda: self.now)
        result = await self.core._run_proactive_chat(False, 120, 0)
        self.assertEqual(result["result"], "skip:NoOwnerSession")

    async def test_it_gives_way_like_a_plan(self):
        manager = self.core._request_state_manager
        manager.create_request("someone else", timeout_seconds=60)
        task = asyncio.create_task(self.core._run_proactive_chat(False, 120, 0))
        await asyncio.sleep(0.3)
        self.targets.record_owner(OWNER)
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result["result"], "deferred:Requeued")

    def test_elapsed_text(self):
        text = self.core._elapsed_text
        self.assertEqual(text(0), "1 分钟")
        self.assertEqual(text(5), "5 分钟")
        self.assertEqual(text(59), "59 分钟")
        self.assertEqual(text(60), "1 小时")
        self.assertEqual(text(200), "3 小时")
        self.assertEqual(text(24 * 60), "1 天")
        self.assertEqual(text(3 * 24 * 60 + 100), "3 天")


class NarratorTextTests(unittest.TestCase):
    def test_the_fixed_sentence_ends_every_proactive_text(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        now = datetime(2026, 11, 2, 7, 30)
        private = plugin._core._proactive_text(plan(), now, False)
        group = plugin._core._proactive_text(plan(), now, True)
        self.assertTrue(private.endswith(f"向制作人道早安{DUPLICATE_NOTE}）"))
        self.assertTrue(group.endswith("（这条消息会发到群里）"))
        self.assertIn(DUPLICATE_NOTE, group)


# ---- console routes -----------------------------------------------------------------------------


def _unused_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeChat:
    def __init__(self):
        self.runs = 0
        self.error = None
        self.result = {"result": "sent", "voice": "skip:Probability", "sticker": "skip:NoTag", "st_ms": 12, "total_ms": 34}

    def snapshot(self):
        return {
            "reason": "Waiting", "next_at": "2026-11-03T15:00", "today": 1, "daily_limit": 3,
            "unanswered": 0, "max_unanswered": 2, "owner_known": True, "running": False,
        }

    async def run_now(self):
        self.runs += 1
        if self.error is not None:
            raise self.error
        return self.result


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import ClientSession, TCPConnector

        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app

        self.directory = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.directory.name))
        self.chat = FakeChat()
        self.port = _unused_port()
        self.service = ConsoleService(create_console_app(self.store, chat_scheduler=self.chat), port=self.port)
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

    async def test_schedule_get_carries_the_chat_settings_and_status(self):
        status, body = await self.call("/api/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(body["chat"]["settings"], default_proactive_chat())
        self.assertEqual(body["chat"]["status"]["reason"], "Waiting")
        self.assertTrue(body["chat"]["available"])
        self.assertEqual(body["settings"]["min_gap_minutes"], 30)
        self.assertEqual(body["settings"]["dedupe_ratio"], 0.8)

    async def test_chat_get(self):
        status, body = await self.call("/api/schedule/chat")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"]["today"], 1)
        self.assertEqual(body["settings"]["silence_min"], 120)

    async def test_chat_save_roundtrip_and_validation(self):
        payload = {**default_proactive_chat(), "daily_limit": 5, "silence_min": 60, "silence_max": 90}
        status, body = await self.call("/api/schedule/chat", "POST", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["daily_limit"], 5)
        self.assertEqual(self.store.config["schedule"]["proactive_chat"]["silence_min"], 60)
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 6)  # plans untouched
        self.assertEqual(self.store.config["schedule"]["settings"]["quiet_minutes"], 10)
        for bad in ({"silence_min": 10}, {"silence_min": 500, "silence_max": 100}, {"followup_factor": 9}, {"prompt": ""}, {"enabled": "x"}):
            status, _ = await self.call("/api/schedule/chat", "POST", {**default_proactive_chat(), **bad})
            self.assertEqual(status, 400, str(bad))
        self.assertEqual(self.store.config["schedule"]["proactive_chat"]["daily_limit"], 5)

    async def test_settings_save_keeps_the_chat_section_and_takes_the_new_keys(self):
        await self.call("/api/schedule/chat", "POST", {**default_proactive_chat(), "daily_limit": 9})
        status, _ = await self.call(
            "/api/schedule/settings", "POST",
            {"enabled": True, "quiet_minutes": 4, "max_defer_minutes": 50, "min_gap_minutes": 15, "dedupe_ratio": 0.9},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.store.config["schedule"]["settings"]["min_gap_minutes"], 15)
        self.assertEqual(self.store.config["schedule"]["proactive_chat"]["daily_limit"], 9)
        for bad in ({"min_gap_minutes": 241}, {"dedupe_ratio": 0.3}, {"dedupe_ratio": 2}):
            status, _ = await self.call("/api/schedule/settings", "POST", bad)
            self.assertEqual(status, 400)

    async def test_chat_test_button(self):
        status, body = await self.call("/api/schedule/chat/test", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["result"], "sent")
        self.assertEqual(self.chat.runs, 1)
        self.chat.error = RuntimeError("busy")
        status, _ = await self.call("/api/schedule/chat/test", "POST")
        self.assertEqual(status, 409)

    async def test_without_a_chat_scheduler(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app

        port = _unused_port()
        service = ConsoleService(create_console_app(self.store), port=port)
        await service.start()
        try:
            async with self.client.get(f"http://127.0.0.1:{port}/api/schedule") as response:
                body = await response.json()
            self.assertFalse(body["chat"]["available"])
            self.assertIsNone(body["chat"]["status"])
            async with self.client.post(f"http://127.0.0.1:{port}/api/schedule/chat/test") as response:
                self.assertEqual(response.status, 503)
        finally:
            await service.stop()

    async def test_origin_is_checked_on_every_write(self):
        evil = {"Origin": "http://evil.example"}
        for path, payload in (("/api/schedule/chat", default_proactive_chat()), ("/api/schedule/chat/test", None)):
            status, _ = await self.call(path, "POST", payload, headers=evil)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.chat.runs, 0)

    async def test_no_session_ids_in_the_chat_view(self):
        _, body = await self.call("/api/schedule/chat")
        self.assertNotIn("openid", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
