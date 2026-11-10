"""L1d.1: an empty window list opens that day at 00:00; a stuck queue slot no longer counts as busy."""

import asyncio
import unittest
from datetime import datetime

from package_context import install_plugin_namespace

install_plugin_namespace()

import test_l1d_proactive as base
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.proactive_chat_config import (
    next_window_start,
    validate_proactive_chat,
    window_state,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_queue import BridgeQueue

WEEKDAY_WINDOWS = [{"start": "12:00", "end": "13:30"}, {"start": "16:30", "end": "23:30"}]
WEEKEND_WINDOW = [{"start": "10:00", "end": "23:30"}]
FRIDAY = (2026, 11, 6)  # 2026-11-02 is a Monday
SATURDAY = (2026, 11, 7)
SUNDAY = (2026, 11, 8)
MONDAY = (2026, 11, 9)


def windows(weekday, weekend):
    return validate_proactive_chat({"active_windows": {"weekday": weekday, "weekend": weekend}})


class NextWindowTests(unittest.TestCase):
    def test_an_empty_next_day_opens_at_midnight_for_the_whole_day(self):
        value = windows(WEEKDAY_WINDOWS, [])
        self.assertEqual(next_window_start(value, 4, 23 * 60 + 40), (1, 0, 1440))  # Friday night -> Saturday
        value = windows([], WEEKEND_WINDOW)
        self.assertEqual(next_window_start(value, 6, 23 * 60 + 40), (1, 0, 1440))  # Sunday night -> Monday

    def test_a_day_with_windows_is_found_as_before(self):
        value = windows(WEEKDAY_WINDOWS, WEEKEND_WINDOW)
        self.assertEqual(next_window_start(value, 4, 23 * 60 + 40), (1, 10 * 60, 810))
        self.assertEqual(next_window_start(value, 0, 14 * 60), (0, 16 * 60 + 30, 420))

    def test_today_empty_is_inside_and_is_never_asked(self):
        value = windows(WEEKDAY_WINDOWS, [])
        self.assertEqual(window_state(value, 5, 3 * 60), (True, None))

    def test_a_later_empty_day_is_found_past_the_rest_of_today(self):
        value = windows(WEEKDAY_WINDOWS, [])
        self.assertEqual(next_window_start(value, 4, 23 * 60 + 40), (1, 0, 1440))
        self.assertEqual(next_window_start(value, 2, 23 * 60 + 40), (1, 12 * 60, 90))  # Wednesday -> Thursday 12:00


class EmptyDayTests(base.ChatBase):
    def start_at(self, day, hour, minute):
        self.now = datetime(*day, hour, minute)
        self.targets._activity.clear()
        self.scheduler = self.make()

    async def test_friday_night_due_goes_out_on_saturday_not_monday(self):
        self.chat(active_windows={"weekday": WEEKDAY_WINDOWS, "weekend": []})
        self.rolls = [0.0, 0.4]  # silence 120 minutes, then a delay of 6 minutes
        self.start_at(FRIDAY, 21, 40)
        self.advance(120)  # 23:40, past the last window
        self.assertIsNone(await self.tick())
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "OutsideWindow")
        self.assertEqual(snapshot["next_at"], "2026-11-07T00:06")
        self.now = datetime(*SATURDAY, 0, 5)
        self.assertIsNone(await self.tick())
        self.now = datetime(*SATURDAY, 0, 6)
        self.assertIsNotNone(await self.tick())

    async def test_the_delay_is_still_at_most_fifteen_minutes(self):
        self.chat(active_windows={"weekday": WEEKDAY_WINDOWS, "weekend": []})
        self.rolls = [0.0, 0.999]
        self.start_at(FRIDAY, 21, 40)
        self.advance(120)
        await self.tick()
        self.assertEqual(self.scheduler.snapshot()["next_at"], "2026-11-07T00:14")

    async def test_weekday_list_empty_works_the_same_way(self):
        self.chat(active_windows={"weekday": [], "weekend": WEEKEND_WINDOW})
        self.rolls = [0.0, 0.0]
        self.start_at(SUNDAY, 21, 40)
        self.advance(120)  # 23:40, past the weekend window
        self.assertIsNone(await self.tick())
        self.assertEqual(self.scheduler.snapshot()["next_at"], "2026-11-09T00:00")
        self.now = datetime(*MONDAY, 0, 0)
        self.assertIsNotNone(await self.tick())

    async def test_both_lists_empty_means_all_day(self):
        self.chat(active_windows={"weekday": [], "weekend": []})
        self.start_at(FRIDAY, 1, 0)
        self.advance(120)
        self.assertIsNotNone(await self.tick())

    async def test_quiet_hours_still_hold_it_until_they_end(self):
        self.chat(
            quiet_hours={"start": "00:00", "end": "08:00"},
            active_windows={"weekday": WEEKDAY_WINDOWS, "weekend": []},
        )
        self.rolls = [0.0, 0.0]
        self.start_at(FRIDAY, 21, 40)
        self.advance(120)  # 23:40
        self.assertIsNone(await self.tick())
        self.assertEqual(self.reason(), "OutsideWindow")
        self.now = datetime(*SATURDAY, 0, 10)  # the day is open but it is quiet hours
        self.assertIsNone(await self.tick())
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["reason"], "QuietHours")
        self.assertEqual(snapshot["next_at"], "2026-11-07T08:00")
        self.now = datetime(*SATURDAY, 7, 59)
        self.assertIsNone(await self.tick())
        self.now = datetime(*SATURDAY, 8, 0)
        self.assertIsNotNone(await self.tick())


class StuckSlotTests(unittest.IsolatedAsyncioTestCase):
    def make_queue(self, limit=10.0):
        self.clock = [100.0]
        return BridgeQueue(clock=lambda: self.clock[0], poll_interval=0.001, active_hold_limit=limit)

    async def occupy(self, queue):
        entry = queue.enqueue("u1", "hi", max_waiting=5)
        await queue.wait_turn(entry, max_wait_seconds=1000, dispatch=lambda current: object(), available=lambda: True)
        return entry

    async def test_a_slot_held_past_the_limit_with_nobody_waiting_is_not_busy(self):
        queue = self.make_queue()
        await self.occupy(queue)
        self.clock[0] += 5
        self.assertTrue(queue.busy)  # in use, inside the limit
        self.clock[0] += 6
        self.assertFalse(queue.busy)  # 11 seconds: stuck
        self.assertFalse(queue.busy)  # reading again changes nothing
        self.assertIsNotNone(queue._active)  # busy is read-only: it did not free the slot

    async def test_a_waiting_entry_still_means_busy_even_with_a_stuck_slot(self):
        queue = self.make_queue()
        await self.occupy(queue)
        self.clock[0] += 60
        waiting = queue.enqueue("u2", "hello", max_waiting=5)
        self.assertTrue(queue.busy)
        queue.release(waiting)
        self.assertFalse(queue.busy)

    async def test_no_limit_never_counts_as_stuck(self):
        queue = BridgeQueue(clock=lambda: self.clock[0], poll_interval=0.001)
        self.clock = [0.0]
        await self.occupy(queue)
        self.clock[0] += 10**6
        self.assertTrue(queue.busy)

    async def test_the_next_entry_frees_the_stuck_slot_and_is_dispatched(self):
        released = []
        queue = BridgeQueue(
            clock=lambda: self.clock[0], poll_interval=0.001, active_hold_limit=10.0,
            on_force_release=lambda entry, held: released.append(held),
        )
        self.clock = [100.0]
        await self.occupy(queue)
        self.clock[0] += 11
        entry = queue.enqueue(None, "x", max_waiting=5, proactive=True)
        result = await queue.wait_turn(entry, max_wait_seconds=1000, dispatch=lambda current: "go", available=lambda: True)
        self.assertEqual(result, "go")
        self.assertEqual(len(released), 1)
        self.assertTrue(queue.busy)  # the new entry now holds the slot
        queue.release(entry)
        self.assertFalse(queue.busy)

    async def test_a_released_slot_is_not_busy(self):
        queue = self.make_queue()
        entry = await self.occupy(queue)
        queue.release(entry)
        self.assertFalse(queue.busy)


class StuckSlotProactiveTests(base.PluginBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = [100.0]
        self.core._queue = BridgeQueue(clock=lambda: self.clock[0], poll_interval=0.01, active_hold_limit=10.0)
        entry = self.core._queue.enqueue("u1", "hi", max_waiting=5)
        await self.core._queue.wait_turn(entry, max_wait_seconds=1000, dispatch=lambda current: object(), available=lambda: True)

    async def test_busy_reason_follows_the_slot(self):
        self.clock[0] += 5
        self.assertEqual(self.core._proactive_busy_reason(base.OWNER), "Queue")
        self.clock[0] += 6
        self.assertIsNone(self.core._proactive_busy_reason(base.OWNER))

    async def test_a_plan_gets_through_a_stuck_slot(self):
        self.clock[0] += 11
        result, _ = await self.run_turn(self.core._run_proactive(base.plan(), False))
        self.assertEqual(result["result"], "sent")

    async def test_the_chat_message_gets_through_a_stuck_slot(self):
        self.clock[0] += 11
        result, _ = await self.run_turn(self.core._run_proactive_chat(False, 120, 0))
        self.assertEqual(result["result"], "sent")

    async def test_a_fresh_slot_still_holds_the_turn_back(self):
        self.clock[0] += 5
        task = asyncio.create_task(self.core._run_proactive(base.plan(), False))
        await asyncio.sleep(0.2)
        self.assertFalse(task.done())  # waits for the working slot
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class StuckSlotSchedulerTests(base.T1Base):
    async def test_the_plan_scheduler_defers_on_a_working_slot_and_goes_on_a_stuck_one(self):
        clock = [100.0]
        queue = BridgeQueue(clock=lambda: clock[0], poll_interval=0.001, active_hold_limit=10.0)
        entry = queue.enqueue("u1", "hi", max_waiting=5)
        await queue.wait_turn(entry, max_wait_seconds=1000, dispatch=lambda current: object(), available=lambda: True)
        self.scheduler._busy_reason = lambda conversation: "Queue" if queue.busy else None
        self.at(7, 30)
        clock[0] += 5
        self.assertEqual(await self.tick(), [])
        clock[0] += 6
        self.assertEqual(len(await self.tick()), 1)


if __name__ == "__main__":
    unittest.main()
