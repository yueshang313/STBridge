"""T1a: schedule config, preset seeds, time math, targets, fired-state and the scheduler."""

import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_config import (
    PLAN_TEMPLATES,
    default_schedule_section,
    default_schedule_settings,
    new_plan_id,
    schedule_section,
    template_list,
    validate_plan,
    validate_schedule_section,
    validate_schedule_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import (
    ScheduleScheduler,
    ScheduleState,
    ScheduleTargets,
    candidate_occurrences,
    group_key_for,
    next_fire_time,
    occurrence_key,
    plan_occurrences,
)

OWNER_UMO = "qq_official:FriendMessage:owner-openid-secret"
GROUP_UMO = "qq_official:GroupMessage:group-openid-secret"
PLAN_ID = "0a1b2c3d"


def daily(**overrides):
    plan = {
        "id": PLAN_ID,
        "name": "早安",
        "enabled": True,
        "kind": "daily",
        "time": "07:30",
        "weekdays": [],
        "jitter_minutes": 0,
        "target": {"type": "private"},
        "prompt": "道早安",
        "fallback_texts": ["早安"],
    }
    plan.update(overrides)
    return plan


def interval(**overrides):
    plan = {
        "id": "1a2b3c4d",
        "name": "喝水",
        "enabled": True,
        "kind": "interval",
        "start": "10:00",
        "end": "20:00",
        "every_minutes": 120,
        "target": {"type": "private"},
        "prompt": "喝水",
        "fallback_texts": [],
    }
    plan.update(overrides)
    return plan


class ScheduleConfigTests(unittest.TestCase):
    def test_settings_defaults_and_bounds(self):
        self.assertEqual(
            default_schedule_settings(),
            {"enabled": True, "quiet_minutes": 10, "max_defer_minutes": 60, "min_gap_minutes": 30, "dedupe_ratio": 0.8},
        )
        self.assertEqual(validate_schedule_settings({"quiet_minutes": 0, "max_defer_minutes": 240})["max_defer_minutes"], 240)
        for bad in (
            {"enabled": "yes"},
            {"quiet_minutes": -1},
            {"quiet_minutes": 121},
            {"max_defer_minutes": 241},
            {"quiet_minutes": 1.5},
            {"quiet_minutes": True},
        ):
            with self.assertRaises(EmotionConfigError, msg=str(bad)):
                validate_schedule_settings(bad)

    def test_valid_plans_are_normalised(self):
        plan = validate_plan(daily(weekdays=[5, 1, 1], fallback_texts=["a", "  ", "b"], prompt="  x  "))
        self.assertEqual(plan["weekdays"], [1, 5])
        self.assertEqual(plan["fallback_texts"], ["a", "b"])
        self.assertEqual(plan["prompt"], "x")
        self.assertNotIn("start", plan)
        iv = validate_plan(interval())
        self.assertEqual((iv["start"], iv["end"], iv["every_minutes"]), ("10:00", "20:00", 120))
        self.assertNotIn("time", iv)

    def test_invalid_plans_are_rejected(self):
        cases = [
            daily(id="nothex"),
            daily(name=""),
            daily(name="x" * 41),
            daily(kind="weekly"),
            daily(time="7:30"),
            daily(time="24:00"),
            daily(weekdays=[7]),
            daily(weekdays="1"),
            daily(jitter_minutes=61),
            daily(prompt=""),
            daily(prompt="x" * 301),
            daily(fallback_texts=["a"] * 6),
            daily(fallback_texts=["x" * 201]),
            daily(target={"type": "group", "key": "nothex"}),
            daily(target={"type": "channel"}),
            daily(enabled="yes"),
            interval(start="20:00", end="10:00"),
            interval(start="10:00", end="10:00"),
            interval(every_minutes=14),
            interval(every_minutes=721),
            interval(end="25:00"),
        ]
        for bad in cases:
            with self.assertRaises(EmotionConfigError, msg=str(bad)):
                validate_plan(bad)

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(EmotionConfigError):
            validate_schedule_section({"plans": [daily(), daily()]})

    def test_section_fallback_for_broken_config(self):
        self.assertEqual(schedule_section({"schedule": {"plans": "x"}})["plans"], [])
        self.assertEqual(schedule_section(None)["settings"], default_schedule_settings())

    def test_new_ids_are_valid(self):
        validate_plan(daily(id=new_plan_id()))

    def test_six_presets_all_disabled_with_text(self):
        section = default_schedule_section()
        self.assertEqual([plan["name"] for plan in section["plans"]],
                         ["早安", "午饭", "晚饭", "喝水", "睡觉提醒", "晚安"])
        self.assertTrue(all(plan["enabled"] is False for plan in section["plans"]))
        for plan in section["plans"]:
            self.assertTrue(plan["prompt"])
            self.assertTrue(2 <= len(plan["fallback_texts"]) <= 3, plan["name"])
            self.assertTrue(all("制作人" in text for text in plan["fallback_texts"]), plan["name"])
        by_name = {plan["name"]: plan for plan in section["plans"]}
        self.assertEqual((by_name["早安"]["time"], by_name["早安"]["jitter_minutes"]), ("09:30", 10))
        self.assertEqual((by_name["午饭"]["time"], by_name["午饭"]["jitter_minutes"]), ("12:00", 10))
        self.assertEqual((by_name["晚饭"]["time"], by_name["晚饭"]["jitter_minutes"]), ("18:30", 10))
        self.assertEqual((by_name["睡觉提醒"]["time"], by_name["睡觉提醒"]["jitter_minutes"]), ("00:00", 5))
        self.assertEqual((by_name["晚安"]["time"], by_name["晚安"]["jitter_minutes"]), ("23:30", 5))
        water = by_name["喝水"]
        self.assertEqual((water["start"], water["end"], water["every_minutes"]), ("10:00", "20:00", 180))

    def test_preset_ids_are_stable(self):
        first = [plan["id"] for plan in default_schedule_section()["plans"]]
        second = [plan["id"] for plan in default_schedule_section()["plans"]]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), 6)

    def test_preset_ids_never_change(self):
        # Fired-state and users' edits are keyed by these; they must stay as released.
        self.assertEqual(
            [plan["id"] for plan in default_schedule_section()["plans"]],
            ["e1d167dc", "d558190d", "72e6f6ae", "84eecbb6", "2fde406e", "e2b2bb56"],
        )

    def test_preset_wording_makes_no_assumption_about_the_producer(self):
        texts = [plan["prompt"] for plan in default_schedule_section()["plans"]]
        texts += [text for plan in default_schedule_section()["plans"] for text in plan["fallback_texts"]]
        texts += [template["prompt"] for template in template_list()]
        texts += [text for template in template_list() for text in template["fallback_texts"]]
        for text in texts:
            for pronoun in ("他", "她", "TA", "ta"):
                self.assertNotIn(pronoun, text.replace("其他", ""))
        self.assertIn("提醒制作人吃早饭", default_schedule_section()["plans"][0]["prompt"])

    def test_templates_are_plan_shaped_without_ids(self):
        templates = template_list()
        self.assertEqual(len(templates), len(PLAN_TEMPLATES))
        for template in templates:
            self.assertNotIn("id", template)
            validate_plan({**template, "id": new_plan_id()})
        templates[0]["name"] = "changed"
        self.assertNotEqual(template_list()[0]["name"], "changed")


class ScheduleSeedTests(unittest.TestCase):
    def test_new_config_gets_presets_and_persists_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            self.assertEqual(len(store.config["schedule"]["plans"]), 6)
            reloaded = ConfigStore(Path(directory))
            self.assertEqual(reloaded.config["schedule"]["plans"], store.config["schedule"]["plans"])

    def test_old_config_without_schedule_gets_disabled_presets(self):
        with tempfile.TemporaryDirectory() as directory:
            ConfigStore(Path(directory))
            path = Path(directory) / "config.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["schedule"]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            store = ConfigStore(Path(directory))
            self.assertEqual(len(store.config["schedule"]["plans"]), 6)
            self.assertTrue(all(not plan["enabled"] for plan in store.config["schedule"]["plans"]))

    def test_deleted_presets_stay_deleted(self):
        async def scenario(directory):
            store = ConfigStore(Path(directory))
            await store.update(lambda config: config["schedule"].update(plans=[]))
            return ConfigStore(Path(directory))

        with tempfile.TemporaryDirectory() as directory:
            reloaded = asyncio.run(scenario(directory))
            self.assertEqual(reloaded.config["schedule"]["plans"], [])

    def test_invalid_schedule_section_is_refused_on_save(self):
        async def scenario(directory):
            store = ConfigStore(Path(directory))
            await store.update(lambda config: config["schedule"].update(plans=[{"id": "x"}]))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Exception):
                asyncio.run(scenario(directory))


class TimeMathTests(unittest.TestCase):
    def test_daily_without_jitter(self):
        self.assertEqual(plan_occurrences(daily(), date(2026, 11, 2)), [datetime(2026, 11, 2, 7, 30)])

    def test_jitter_is_seeded_by_date_and_plan_and_bounded(self):
        plan = daily(jitter_minutes=10)
        day = date(2026, 11, 2)
        first = plan_occurrences(plan, day)
        self.assertEqual(plan_occurrences(plan, day), first)  # a reload gives the same time
        for offset in range(40):
            when = plan_occurrences(plan, day + timedelta(days=offset))[0]
            nominal = datetime.combine(day + timedelta(days=offset), datetime.min.time()) + timedelta(hours=7, minutes=30)
            self.assertLessEqual(abs(when - nominal), timedelta(minutes=10))
        offsets = {
            plan_occurrences(plan, day + timedelta(days=n))[0] - datetime.combine(day + timedelta(days=n), datetime.min.time())
            for n in range(40)
        }
        self.assertGreater(len(offsets), 3)  # it really varies from day to day
        other = daily(id="ffffffff", jitter_minutes=10)
        times = [plan_occurrences(plan, day + timedelta(days=n))[0].time() for n in range(30)]
        other_times = [plan_occurrences(other, day + timedelta(days=n))[0].time() for n in range(30)]
        self.assertNotEqual(times, other_times)

    def test_weekday_filter(self):
        plan = daily(weekdays=[0, 2])  # Monday and Wednesday
        self.assertEqual(len(plan_occurrences(plan, date(2026, 11, 2))), 1)  # Monday
        self.assertEqual(plan_occurrences(plan, date(2026, 11, 3)), [])
        self.assertEqual(len(plan_occurrences(plan, date(2026, 11, 4))), 1)
        self.assertEqual(plan_occurrences(plan, date(2026, 11, 8)), [])  # Sunday

    def test_interval_points_include_the_end(self):
        points = plan_occurrences(interval(), date(2026, 11, 2))
        self.assertEqual([p.strftime("%H:%M") for p in points], ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"])
        points = plan_occurrences(interval(end="19:59"), date(2026, 11, 2))
        self.assertEqual(points[-1].strftime("%H:%M"), "18:00")

    def test_candidates_cover_yesterday_for_jitter_across_midnight(self):
        plan = daily(time="23:58", jitter_minutes=5)
        found = []
        for n in range(20):
            now = datetime(2026, 11, 2 + n % 20, 0, 3)
            found.extend(when for when in candidate_occurrences(plan, now) if when.date() > (now - timedelta(days=1)).date())
        self.assertTrue(found)

    def test_next_fire_skips_fired_and_old(self):
        plan = daily()
        now = datetime(2026, 11, 2, 7, 0)
        self.assertEqual(next_fire_time(plan, now, lambda key: False), datetime(2026, 11, 2, 7, 30))
        key = occurrence_key(plan["id"], datetime(2026, 11, 2, 7, 30))
        self.assertEqual(next_fire_time(plan, now, lambda k: k == key), datetime(2026, 11, 3, 7, 30))
        later = datetime(2026, 11, 2, 7, 45)  # outside the catch-up window
        self.assertEqual(next_fire_time(plan, later, lambda k: False), datetime(2026, 11, 3, 7, 30))
        within = datetime(2026, 11, 2, 7, 35)
        self.assertEqual(next_fire_time(plan, within, lambda k: False), datetime(2026, 11, 2, 7, 30))

    def test_weekday_only_plan_finds_the_next_allowed_day(self):
        plan = daily(weekdays=[0])
        self.assertEqual(next_fire_time(plan, datetime(2026, 11, 3, 9, 0), lambda k: False), datetime(2026, 11, 9, 7, 30))


class TargetsAndStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 11, 2, 12, 0)
        self.targets = ScheduleTargets(Path(self.directory.name), clock=lambda: self.now)

    def test_owner_and_group_are_remembered_across_restarts(self):
        self.assertFalse(self.targets.has_owner)
        self.assertIsNone(self.targets.conversation_for({"type": "private"}))
        self.targets.record_owner(OWNER_UMO)
        key = self.targets.record_group(GROUP_UMO)
        self.assertEqual(key, group_key_for(GROUP_UMO))
        reloaded = ScheduleTargets(Path(self.directory.name), clock=lambda: self.now)
        self.assertEqual(reloaded.conversation_for({"type": "private"}), OWNER_UMO)
        self.assertEqual(reloaded.conversation_for({"type": "group", "key": key}), GROUP_UMO)
        self.assertIsNone(reloaded.conversation_for({"type": "group", "key": "00000000"}))
        self.assertTrue(reloaded.has_group(key))

    def test_activity_is_memory_only(self):
        self.targets.record_owner(OWNER_UMO)
        self.assertEqual(self.targets.last_activity(OWNER_UMO), self.now)
        reloaded = ScheduleTargets(Path(self.directory.name), clock=lambda: self.now)
        self.assertIsNone(reloaded.last_activity(OWNER_UMO))
        content = (Path(self.directory.name) / "schedule_targets.json").read_text(encoding="utf-8")
        self.assertNotIn("activity", content)

    def test_group_view_never_exposes_the_umo(self):
        key = self.targets.record_group(GROUP_UMO)
        view = self.targets.groups()
        self.assertEqual(view[0]["key"], key)
        self.assertEqual(view[0]["alias"], f"群 {key}")
        self.assertNotIn("secret", json.dumps(view, ensure_ascii=False))

    def test_alias_can_be_changed_and_survives_a_restart(self):
        key = self.targets.record_group(GROUP_UMO)
        self.assertTrue(self.targets.set_alias(key, "制作人的群"))
        self.assertFalse(self.targets.set_alias("00000000", "x"))
        self.assertEqual(ScheduleTargets(Path(self.directory.name)).groups()[0]["alias"], "制作人的群")
        self.targets.record_group(GROUP_UMO)  # seen again: alias kept
        self.assertEqual(self.targets.groups()[0]["alias"], "制作人的群")

    def test_tampered_group_entries_are_ignored(self):
        path = Path(self.directory.name) / "schedule_targets.json"
        path.write_text(json.dumps({"groups": {"deadbeef": {"umo": GROUP_UMO}}, "owner": 5}), encoding="utf-8")
        reloaded = ScheduleTargets(Path(self.directory.name))
        self.assertEqual(reloaded.groups(), [])
        self.assertFalse(reloaded.has_owner)

    def test_corrupt_files_start_empty(self):
        (Path(self.directory.name) / "schedule_targets.json").write_text("{not json", encoding="utf-8")
        (Path(self.directory.name) / "schedule_state.json").write_text("[]", encoding="utf-8")
        self.assertFalse(ScheduleTargets(Path(self.directory.name)).has_owner)
        self.assertFalse(ScheduleState(Path(self.directory.name)).fired("x@2026-11-02T07:30"))

    def test_state_persists_and_prunes_old_entries(self):
        state = ScheduleState(Path(self.directory.name))
        state.mark("a@2026-10-20T07:30", datetime(2026, 10, 20, 7, 31))
        state.mark("b@2026-11-02T07:30", datetime(2026, 11, 2, 7, 31))
        reloaded = ScheduleState(Path(self.directory.name))
        self.assertTrue(reloaded.fired("b@2026-11-02T07:30"))
        self.assertFalse(reloaded.fired("a@2026-10-20T07:30"))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 11, 2, 7, 0)
        self.targets = ScheduleTargets(Path(self.directory.name), clock=lambda: self.now)
        self.targets.record_owner(OWNER_UMO)
        self.targets._activity.clear()  # nothing said yet
        self.state = ScheduleState(Path(self.directory.name))
        self.config = {"schedule": {"settings": default_schedule_settings(), "plans": [daily()]}}
        self.calls = []
        self.gate = None
        self.logs = []

        class Logger:
            def info(inner, message):
                self.logs.append(message)

            def error(inner, message):
                self.logs.append(message)

        async def runner(plan, forced):
            self.calls.append((plan["id"], forced, self.now))
            if self.gate is not None:
                await self.gate.wait()
            return {"result": "sent"}

        self.runner = runner
        self.make_scheduler = lambda: ScheduleScheduler(
            get_config=lambda: self.config,
            targets=self.targets,
            state=self.state,
            runner=runner,
            clock=lambda: self.now,
            logger=Logger(),
        )
        self.scheduler = self.make_scheduler()

    async def tick(self, scheduler=None):
        tasks = (scheduler or self.scheduler).tick()
        if tasks:
            await asyncio.gather(*tasks)
        return tasks

    def at(self, hour, minute):
        self.now = datetime(2026, 11, 2, hour, minute)

    async def test_fires_once_when_due(self):
        self.at(7, 29)
        self.assertEqual(await self.tick(), [])
        self.at(7, 30)
        self.assertEqual(len(await self.tick()), 1)
        self.assertEqual(self.calls, [(PLAN_ID, False, datetime(2026, 11, 2, 7, 30))])
        self.at(7, 31)
        self.assertEqual(await self.tick(), [])
        self.assertEqual(len(self.calls), 1)

    async def test_fired_state_survives_a_reload(self):
        self.at(7, 31)
        await self.tick()
        reloaded = ScheduleScheduler(
            get_config=lambda: self.config,
            targets=self.targets,
            state=ScheduleState(Path(self.directory.name)),
            runner=self.runner,
            clock=lambda: self.now,
        )
        self.assertEqual(await self.tick(reloaded), [])
        self.assertEqual(len(self.calls), 1)

    async def test_catch_up_within_ten_minutes_but_not_later(self):
        self.at(7, 39)
        self.assertEqual(len(await self.tick()), 1)
        self.calls.clear()
        self.state = ScheduleState(Path(self.directory.name))
        self.config["schedule"]["plans"] = [daily(id="0b1b2c3d")]
        scheduler = self.make_scheduler()
        self.at(7, 41)  # 11 minutes late: dropped (downtime is not made up)
        self.assertEqual(await self.tick(scheduler), [])
        self.assertTrue(any("skip:Missed" in line for line in self.logs))
        self.assertEqual(self.calls, [])

    async def test_missed_is_logged_only_once(self):
        self.at(8, 0)
        for _ in range(5):
            await self.tick()
        self.assertEqual(sum("skip:Missed" in line for line in self.logs), 1)

    async def test_quiet_time_defers_then_sends(self):
        self.targets.touch(OWNER_UMO)  # said something at 07:00 (self.now)
        self.at(7, 30)
        self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, 25)
        self.assertEqual(await self.tick(), [])
        self.assertEqual(self.calls, [])
        self.at(7, 36)  # more than 10 minutes since the last message
        self.assertEqual(len(await self.tick()), 1)
        self.assertEqual(self.calls[0][2], datetime(2026, 11, 2, 7, 36))

    async def test_quiet_deferral_continues_past_the_catch_up_window(self):
        self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, 30)
        self.at(7, 30)
        await self.tick()
        for minute in (40, 50):
            self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, minute)
            self.at(7, minute + 3)
            self.assertEqual(await self.tick(), [])  # still chatting, 13+ minutes after the plan time
        self.at(8, 5)
        self.assertEqual(len(await self.tick()), 1)  # quiet again, 35 minutes late but inside max_defer

    async def test_deferral_is_abandoned_after_max_defer(self):
        self.config["schedule"]["settings"]["max_defer_minutes"] = 20
        self.at(7, 30)
        self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, 29)
        await self.tick()
        self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, 51)
        self.at(7, 52)
        self.assertEqual(await self.tick(), [])
        self.assertTrue(any("skip:DeferTimeout" in line for line in self.logs))
        self.at(8, 30)
        self.assertEqual(await self.tick(), [])  # given up for good
        self.assertEqual(self.calls, [])

    async def test_quiet_minutes_zero_never_defers(self):
        self.config["schedule"]["settings"]["quiet_minutes"] = 0
        self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, 30)
        self.at(7, 30)
        self.assertEqual(len(await self.tick()), 1)

    async def test_same_plan_never_runs_twice_at_once(self):
        self.config["schedule"]["plans"] = [interval(every_minutes=15, start="07:30", end="08:00")]
        self.gate = asyncio.Event()
        self.at(7, 30)
        first = self.scheduler.tick()
        self.assertEqual(len(first), 1)
        await asyncio.sleep(0)
        self.at(7, 45)  # the next point is due while the first is still running
        self.assertEqual(self.scheduler.tick(), [])
        self.assertTrue(self.scheduler.is_running("1a2b3c4d"))
        self.gate.set()
        await asyncio.gather(*first)
        again = self.scheduler.tick()  # now it can go (still inside the window)
        self.assertEqual(len(again), 1)
        await asyncio.gather(*again)

    async def test_run_now_ignores_time_and_quiet_and_blocks_a_second_run(self):
        self.gate = asyncio.Event()
        self.targets._activity[OWNER_UMO] = self.now
        task = asyncio.create_task(self.scheduler.run_now(daily()))
        await asyncio.sleep(0)
        self.assertEqual(self.calls[0][1], True)
        with self.assertRaises(RuntimeError):
            await self.scheduler.run_now(daily())
        self.gate.set()
        self.assertEqual(await task, {"result": "sent"})
        self.assertFalse(self.scheduler.is_running(PLAN_ID))

    async def test_disabled_plan_and_disabled_schedule(self):
        self.at(7, 30)
        self.config["schedule"]["plans"] = [daily(enabled=False)]
        self.assertEqual(await self.tick(), [])
        self.config["schedule"]["plans"] = [daily()]
        self.config["schedule"]["settings"]["enabled"] = False
        self.assertEqual(await self.tick(), [])
        self.assertEqual(self.calls, [])

    async def test_missing_owner_session_is_skipped_and_logged_without_ids(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.targets = ScheduleTargets(Path(empty.name), clock=lambda: self.now)
        scheduler = self.make_scheduler()
        self.at(7, 30)
        self.assertEqual(await self.tick(scheduler), [])
        self.assertTrue(any("skip:NoOwnerSession" in line for line in self.logs))
        self.assertEqual(self.calls, [])

    async def test_unknown_group_target_is_skipped(self):
        self.config["schedule"]["plans"] = [daily(target={"type": "group", "key": "deadbeef"})]
        self.at(7, 30)
        self.assertEqual(await self.tick(), [])
        self.assertTrue(any("skip:UnknownGroup" in line for line in self.logs))

    async def test_group_plan_uses_the_groups_own_activity(self):
        key = self.targets.record_group(GROUP_UMO)
        self.targets._activity[OWNER_UMO] = datetime(2026, 11, 2, 7, 30)  # the owner is chatting
        self.targets._activity.pop(GROUP_UMO, None)
        self.config["schedule"]["plans"] = [daily(target={"type": "group", "key": key})]
        self.at(7, 30)
        self.assertEqual(len(await self.tick()), 1)

    async def test_runner_errors_are_contained_and_logged_by_type(self):
        async def broken(plan, forced):
            raise ValueError("secret detail")

        scheduler = ScheduleScheduler(
            get_config=lambda: self.config,
            targets=self.targets,
            state=self.state,
            runner=broken,
            clock=lambda: self.now,
        )
        self.at(7, 30)
        await self.tick(scheduler)
        self.assertFalse(scheduler.is_running(PLAN_ID))

    async def test_loop_runs_and_stops_cleanly(self):
        self.at(7, 30)
        self.scheduler.start(interval=0.01)
        for _ in range(100):
            if self.calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls), 1)
        await self.scheduler.stop()
        await self.scheduler.stop()

    async def test_logs_never_contain_session_ids(self):
        self.at(7, 55)
        await self.tick()
        for line in self.logs:
            self.assertNotIn("secret", line)
            self.assertNotIn("openid", line)

    async def test_next_fire_reflects_fired_state(self):
        self.at(6, 0)
        self.assertEqual(self.scheduler.next_fire(daily()), datetime(2026, 11, 2, 7, 30))
        self.at(7, 30)
        await self.tick()
        self.assertEqual(self.scheduler.next_fire(daily()), datetime(2026, 11, 3, 7, 30))


class ProactiveClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_carries_kind_only_for_proactive_requests(self):
        manager = RequestStateManager(timeout_seconds=10)
        gate = BridgeRecoveryGate()
        gate.confirm_recovery()
        app = create_app(manager, HttpProtocolConfig(allowed_origin="http://localhost:11451", listen_port=11452), recovery_gate=gate)
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        try:
            headers = {"Origin": "http://localhost:11451"}
            request = manager.create_request("（系统提示）", kind="proactive")
            self.assertEqual(request.kind, "proactive")
            response = await client.post(f"/v1/requests/{request.request_id}/claim", json={"claim_attempt_id": "a1"}, headers=headers)
            body = await response.json()
            self.assertEqual(body["kind"], "proactive")
            manager.confirm_generation_not_started(request.request_id)
            manager.fail_request(request.request_id, "extension_error")

            plain = manager.create_request("hello")
            self.assertEqual(plain.kind, "chat")
            response = await client.post(f"/v1/requests/{plain.request_id}/claim", json={"claim_attempt_id": "a2"}, headers=headers)
            self.assertNotIn("kind", await response.json())
        finally:
            await client.close()

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            RequestStateManager(timeout_seconds=10).create_request("x", kind="other")


if __name__ == "__main__":
    unittest.main()
