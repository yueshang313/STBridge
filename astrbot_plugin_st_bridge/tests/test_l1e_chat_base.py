"""L1e: the owner-chat silence counts from real chat only (messages, replies, songs, proactive chat messages),
never from a scheduled plan, and survives a restart."""

import asyncio
import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import mixin_bridge, mixin_song, schedule_service
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.proactive_chat import ProactiveChatScheduler
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import ScheduleTargets, TARGETS_FILE

from test_l1d_proactive import GROUP, OWNER, REPLY, ChatBase, PluginBase, plan
from test_plugin_lifecycle import _wait_for_request


class TargetsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.now = datetime(2026, 11, 2, 13, 0)
        self.targets = ScheduleTargets(self.data, clock=lambda: self.now)

    def reload(self):
        return ScheduleTargets(self.data, clock=lambda: self.now)

    def write_file(self, **fields):
        (self.data / TARGETS_FILE).write_text(json.dumps(fields), encoding="utf-8")

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def test_the_owner_message_sets_the_chat_base(self):
        self.assertIsNone(self.targets.chat_base(OWNER))
        self.targets.record_owner(OWNER)
        self.assertEqual(self.targets.chat_base(OWNER), self.now)

    def test_plain_touch_is_activity_but_not_chat(self):
        self.targets.record_owner(OWNER)
        started = self.now
        self.advance(3600)
        self.targets.touch(OWNER)  # a plan went out
        self.assertEqual(self.targets.last_activity(OWNER), self.now)
        self.assertEqual(self.targets.chat_base(OWNER), started)

    def test_touch_chat_moves_the_base(self):
        self.targets.record_owner(OWNER)
        self.advance(600)
        self.targets.touch_chat(OWNER)
        self.assertEqual(self.targets.chat_base(OWNER), self.now)

    def test_groups_have_no_chat_base_and_do_not_move_the_owners(self):
        self.targets.record_owner(OWNER)
        started = self.now
        self.advance(600)
        self.targets.record_group(GROUP)
        self.targets.touch(GROUP)
        self.targets.touch_chat(GROUP)
        self.assertIsNone(self.targets.chat_base(GROUP))
        self.assertEqual(self.targets.chat_base(OWNER), started)

    def test_other_inputs_are_ignored(self):
        self.targets.record_owner(OWNER)
        started = self.now
        self.advance(600)
        for value in (None, "", 5):
            self.targets.touch_chat(value)
        self.assertIsNone(self.targets.chat_base(None))
        self.assertIsNone(self.targets.chat_base(""))
        self.assertEqual(self.targets.chat_base(OWNER), started)

    def test_a_new_owner_session_restarts_the_base(self):
        self.targets.record_owner(OWNER)
        self.advance(7200)
        self.targets.record_owner("onebot:FriendMessage:42")
        self.assertIsNone(self.targets.chat_base(OWNER))
        self.assertEqual(self.targets.chat_base("onebot:FriendMessage:42"), self.now)

    # -- persistence

    def test_the_base_is_written_with_the_owner_and_read_back(self):
        self.targets.record_owner(OWNER)
        data = json.loads((self.data / TARGETS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["owner_chat_base"], {"umo": OWNER, "at": "2026-11-02T13:00:00"})
        self.advance(600)
        self.assertEqual(self.reload().chat_base(OWNER), datetime(2026, 11, 2, 13, 0))

    def test_a_proactive_chat_delivery_is_always_written(self):
        self.targets.record_owner(OWNER)
        self.advance(5)
        self.targets.touch_chat(OWNER, force_save=True)
        self.assertEqual(self.reload().chat_base(OWNER), self.now)

    def test_writes_are_throttled_to_one_a_minute(self):
        self.targets.record_owner(OWNER)
        with mock.patch.object(schedule_service, "_write_json", wraps=schedule_service._write_json) as write:
            self.advance(30)
            self.targets.record_owner(OWNER)
            self.targets.touch_chat(OWNER)
            self.assertEqual(write.call_count, 0)
            self.advance(30)  # a full minute since the last write
            self.targets.touch_chat(OWNER)
            self.assertEqual(write.call_count, 1)
            self.advance(10)
            self.targets.record_owner(OWNER)
            self.assertEqual(write.call_count, 1)
            self.targets.touch_chat(OWNER, force_save=True)
            self.assertEqual(write.call_count, 2)
        self.assertEqual(self.reload().chat_base(OWNER), self.now)

    def test_a_failed_write_keeps_the_value_in_memory_and_is_not_retried_every_message(self):
        self.targets.record_owner(OWNER)
        with mock.patch.object(schedule_service, "_write_json", side_effect=OSError("disk")) as write:
            self.advance(120)
            self.targets.touch_chat(OWNER)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(self.targets.chat_base(OWNER), self.now)
            self.advance(5)
            self.targets.touch_chat(OWNER)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(self.targets.chat_base(OWNER), self.now)

    def test_a_file_from_before_has_no_base(self):
        self.write_file(owner={"umo": OWNER}, groups={})
        targets = self.reload()
        self.assertEqual(targets.owner_conversation, OWNER)
        self.assertIsNone(targets.chat_base(OWNER))

    def test_a_base_of_another_session_is_ignored(self):
        self.write_file(owner={"umo": OWNER}, groups={}, owner_chat_base={"umo": "other", "at": "2026-11-02T12:00:00"})
        self.assertIsNone(self.reload().chat_base(OWNER))

    def test_a_base_in_the_future_is_ignored(self):
        self.write_file(owner={"umo": OWNER}, groups={}, owner_chat_base={"umo": OWNER, "at": "2026-11-02T13:00:01"})
        self.assertIsNone(self.reload().chat_base(OWNER))

    def test_a_base_without_an_owner_is_ignored(self):
        self.write_file(owner=None, groups={}, owner_chat_base={"umo": OWNER, "at": "2026-11-02T12:00:00"})
        self.assertIsNone(self.reload().chat_base(OWNER))

    def test_broken_fields_do_not_raise(self):
        for broken in (
            {"umo": OWNER, "at": "yesterday"},
            {"umo": OWNER, "at": 5},
            {"umo": OWNER},
            {"at": "2026-11-02T12:00:00"},
            {"umo": 5, "at": "2026-11-02T12:00:00"},
            "text",
            [1, 2],
            None,
        ):
            with self.subTest(broken=broken):
                self.write_file(owner={"umo": OWNER}, groups={}, owner_chat_base=broken)
                targets = self.reload()
                self.assertEqual(targets.owner_conversation, OWNER)
                self.assertIsNone(targets.chat_base(OWNER))

    def test_the_stored_time_is_valid_back_to_the_second(self):
        self.write_file(owner={"umo": OWNER}, groups={}, owner_chat_base={"umo": OWNER, "at": "2026-11-02T12:34:56"})
        self.assertEqual(self.reload().chat_base(OWNER), datetime(2026, 11, 2, 12, 34, 56))

    def test_a_group_save_keeps_the_base(self):
        self.targets.record_owner(OWNER)
        self.advance(5)
        self.targets.record_group(GROUP)  # saves the file as well
        self.assertEqual(self.reload().chat_base(OWNER), datetime(2026, 11, 2, 13, 0))


class SchedulerTests(ChatBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.targets.record_owner(OWNER)
        self.targets._activity.clear()
        self.targets._chat_base = None
        self.scheduler = self.make()

    def next_at(self):
        return self.scheduler.snapshot()["next_at"]

    async def test_a_plan_going_out_neither_moves_the_base_nor_the_due_time(self):
        before = self.next_at()
        self.assertEqual(before, "2026-11-02T15:00")
        self.advance(60)
        self.targets.touch(OWNER)  # a private T1 plan was delivered
        self.assertEqual(self.targets.last_activity(OWNER), self.now)
        self.assertIsNone(self.targets.chat_base(OWNER))
        self.assertEqual(self.next_at(), before)
        self.advance(30)
        self.targets.touch(OWNER)
        self.assertEqual(self.next_at(), before)

    async def test_hourly_plans_do_not_hold_the_chat_message_back(self):
        self.default_roll = 0.0  # due after 120 minutes
        for _ in range(4):
            self.advance(30)
            self.targets.touch(OWNER)  # a plan every half hour
            self.assertIsNone(await self.tick())
        self.advance(1)  # 121 minutes: the plans' own activity was long enough ago
        self.targets._activity[OWNER] = self.now - timedelta(minutes=30)
        self.assertIsNotNone(await self.tick())

    async def test_every_kind_of_chat_redraws_the_due_time(self):
        for label, action in (
            ("owner message", lambda: self.targets.record_owner(OWNER)),
            ("finished reply", lambda: self.targets.touch_chat(OWNER)),
            ("delivered song", lambda: self.targets.touch_chat(OWNER)),
            ("proactive chat", lambda: self.targets.touch_chat(OWNER, force_save=True)),
        ):
            with self.subTest(label):
                self.advance(30)
                action()
                self.assertEqual(self.targets.chat_base(OWNER), self.now)
                self.assertEqual(self.next_at(), (self.now + timedelta(minutes=120)).isoformat(timespec="minutes"))

    async def test_group_traffic_does_not_move_the_due_time(self):
        before = self.next_at()
        self.advance(40)
        self.targets.record_group(GROUP)
        self.targets.touch(GROUP)
        self.targets.touch_chat(GROUP)
        self.assertEqual(self.next_at(), before)

    async def test_a_plan_just_sent_makes_the_message_wait_then_the_gap_applies(self):
        self.advance(130)  # long past the due time
        self.targets.touch(OWNER)  # a plan goes out now ...
        self.state.record_sent(self.now)  # ... which also starts the minimum gap
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Conversing")
        self.advance(10)  # quiet_minutes are over
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Gap")
        self.advance(20)  # min_gap_minutes (30) are over
        self.assertIsNotNone(await self.tick())

    async def test_a_plan_does_not_delay_the_message_once_quiet_and_gap_are_over(self):
        self.advance(100)
        self.targets.touch(OWNER)
        self.state.record_sent(self.now)
        self.advance(30)  # 130 minutes since the last real chat, 30 since the plan
        self.assertIsNotNone(await self.tick())

    async def test_after_a_delivery_the_next_one_is_not_due_at_once(self):
        self.chat(max_unanswered=5)
        self.advance(120)
        self.assertIsNotNone(await self.tick())
        self.assertEqual(self.targets.chat_base(OWNER), self.now)
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "Waiting")
        self.assertEqual(self.next_at(), (self.now + timedelta(minutes=180)).isoformat(timespec="minutes"))
        self.advance(179)
        self.assertIsNone(await self.tick())
        self.advance(1)
        self.assertIsNotNone(await self.tick())

    async def test_the_test_button_counts_from_the_chat_base(self):
        self.targets._chat_base = self.now - timedelta(minutes=95)
        self.targets._activity[OWNER] = self.now - timedelta(minutes=5)
        await self.scheduler.run_now()
        self.assertEqual(self.calls[0][1], 95)

    async def test_a_restart_goes_on_counting_from_the_stored_base(self):
        self.targets.record_owner(OWNER)  # 13:00, written to the file
        self.advance(150)  # the core was off, then started again
        self.targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.scheduler = self.make()
        self.assertEqual(self.reason(), "Ready")
        self.assertIsNotNone(await self.tick())
        self.assertEqual(self.calls[0][1], 150)

    async def test_a_restart_without_a_stored_base_starts_counting_at_the_start(self):
        self.advance(500)
        self.targets = ScheduleTargets(self.data, clock=lambda: self.now)
        self.targets._chat_base = None
        self.scheduler = self.make()
        self.assertEqual(self.reason(), "Waiting")
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_unanswered_counting_is_unchanged(self):
        self.chat(max_unanswered=5)
        self.advance(120)
        await self.tick()
        self.assertEqual(self.state.unanswered, 1)
        self.targets.record_owner(OWNER)
        self.assertEqual(self.reason(), "Waiting")
        self.assertEqual(self.state.unanswered, 0)


class PluginTests(PluginBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.old = datetime(2026, 11, 2, 5, 0)
        self.targets._chat_base = self.old
        self.targets._activity.clear()

    def reloaded_base(self):
        return ScheduleTargets(self.data, clock=lambda: self.now).chat_base(OWNER)

    async def test_a_private_plan_is_activity_but_not_chat(self):
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.targets.last_activity(OWNER), self.now)
        self.assertEqual(self.targets.chat_base(OWNER), self.old)

    async def test_a_plan_to_a_group_changes_nothing_of_the_owner_chat(self):
        key = self.targets.record_group(GROUP)
        self.targets._activity.clear()
        result, _ = await self.run_turn(
            self.core._run_proactive(plan(target={"type": "group", "key": key}), False)
        )
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.targets.last_activity(GROUP), self.now)
        self.assertIsNone(self.targets.last_activity(OWNER))
        self.assertEqual(self.targets.chat_base(OWNER), self.old)

    async def test_a_delivered_chat_message_moves_the_base_and_is_written_at_once(self):
        # the throttle would skip the write: the last one was a moment ago
        self.targets._chat_base_saved = self.now
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 200, 0))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.targets.chat_base(OWNER), self.now)
        self.assertEqual(self.reloaded_base(), self.now)

    async def test_a_chat_message_that_did_not_go_out_leaves_the_base(self):
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 200, 0), st="fail")
        self.assertNotEqual(result["result"], "sent")
        self.assertEqual(self.targets.chat_base(OWNER), self.old)

    async def test_the_chat_base_is_moved_before_the_scheduler_books_the_delivery(self):
        order = []
        original = self.targets.touch_chat
        self.targets.touch_chat = lambda *a, **k: (order.append("base"), original(*a, **k))[1]
        scheduler = ProactiveChatScheduler(
            get_config=lambda: {"schedule": {"proactive_chat": {}}},
            targets=self.targets,
            state=self.core._proactive_state,
            runner=lambda forced, elapsed, unanswered: self.core._run_proactive_chat(forced, elapsed, unanswered),
            clock=lambda: self.now,
        )
        original_finish = scheduler._finish
        scheduler._finish = lambda result: (order.append("finish"), original_finish(result))[1]
        task = asyncio.create_task(scheduler._run(200, 0))
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "turn-order")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, REPLY)
        await asyncio.wait_for(task, 10)
        self.assertEqual(order, ["base", "finish"])

    async def test_a_finished_reply_moves_the_base(self):
        event = self.owner_event()

        async def consume():
            async for _item in self.plugin.stbridge_private_message(event):
                pass

        task = asyncio.create_task(consume())
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, REPLY)
        await task
        self.assertEqual(self.targets.chat_base(OWNER), self.now)

    async def test_touching_through_the_core(self):
        self.core._touch_conversation(OWNER)
        self.assertEqual(self.targets.last_activity(OWNER), self.now)
        self.assertEqual(self.targets.chat_base(OWNER), self.old)
        self.core._touch_conversation(OWNER, chat=True)
        self.assertEqual(self.targets.chat_base(OWNER), self.now)

    async def test_a_group_touch_through_the_core_is_not_owner_chat(self):
        self.core._touch_conversation(GROUP, chat=True)
        self.assertEqual(self.targets.chat_base(OWNER), self.old)

    def test_the_reply_and_the_song_delivery_count_as_chat(self):
        self.assertIn("_touch_conversation(conversation, chat=True)", inspect.getsource(mixin_bridge))
        self.assertIn("_touch_conversation(conversation, chat=True)", inspect.getsource(mixin_song))


if __name__ == "__main__":
    unittest.main()
