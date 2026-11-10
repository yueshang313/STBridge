"""H2: today's numbers for the console overview (replies, songs), the mood summary and the status field."""

import asyncio
import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import daily_counts as daily_module
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import mixin_song
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.daily_counts import COUNTS_FILE, DailyCounts
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, default_config, mood_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mood_store import MoodStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import GROUP, PRIVATE
from test_a1_testchat import Base, drive, finish, real_message
from test_console_server import _unused_loopback_port
from test_l1d_proactive import PluginBase, plan
from test_plugin_lifecycle import _wait_for_request

GOOD = '[情绪:开心]☆‹「日」›"中"'


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 9, 20, 0, 0)

    def __call__(self):
        return self.now


class CountsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.clock = Clock()
        self.counts = DailyCounts(self.data, clock=self.clock)

    def reload(self):
        return DailyCounts(self.data, clock=self.clock)

    def test_it_starts_at_zero(self):
        snapshot = self.counts.snapshot()
        self.assertEqual(snapshot["date"], "2026-11-09")
        self.assertEqual(snapshot["replies"], {"private": 0, "group": 0, "total": 0})
        self.assertEqual(snapshot["songs"], {"owner": 0, "group": 0, "total": 0})

    def test_private_and_group_replies_are_counted_apart(self):
        for kind in ("private", "private", "group"):
            self.counts.add_reply(kind)
        self.assertEqual(self.counts.snapshot()["replies"], {"private": 2, "group": 1, "total": 3})

    def test_songs_are_counted_by_owner_and_group(self):
        self.counts.add_song("owner")
        self.counts.add_song("group")
        self.counts.add_song("group")
        self.assertEqual(self.counts.snapshot()["songs"], {"owner": 1, "group": 2, "total": 3})

    def test_unknown_kinds_are_ignored(self):
        self.counts.add_reply("proactive")
        self.counts.add_song("nobody")
        self.assertEqual(self.counts.snapshot()["replies"]["total"], 0)
        self.assertEqual(self.counts.snapshot()["songs"]["total"], 0)

    def test_a_new_date_starts_from_zero(self):
        self.counts.add_reply("private")
        self.counts.add_song("owner")
        self.clock.now += timedelta(hours=5)  # 01:00 the next day
        snapshot = self.counts.snapshot()
        self.assertEqual(snapshot["date"], "2026-11-10")
        self.assertEqual(snapshot["replies"]["total"], 0)
        self.assertEqual(snapshot["songs"]["total"], 0)
        self.counts.add_reply("group")
        self.assertEqual(self.counts.snapshot()["replies"], {"private": 0, "group": 1, "total": 1})

    def test_the_counts_survive_a_restart(self):
        self.counts.add_reply("private")
        self.counts.add_reply("group")
        self.counts.add_song("owner")
        again = self.reload().snapshot()
        self.assertEqual(again["replies"], {"private": 1, "group": 1, "total": 2})
        self.assertEqual(again["songs"], {"owner": 1, "group": 0, "total": 1})

    def test_the_file_of_another_day_is_ignored(self):
        self.counts.add_reply("private")
        self.clock.now += timedelta(days=1)
        self.assertEqual(self.reload().snapshot()["replies"]["total"], 0)

    def test_broken_files_read_as_empty_without_raising(self):
        path = self.data / COUNTS_FILE
        for content in ("{not json", "[]", "null", json.dumps({"version": 9, "date": "2026-11-09", "replies": {}, "songs": {}}),
                        json.dumps({"version": 1, "date": 5, "replies": {}, "songs": {}}),
                        json.dumps({"version": 1, "date": "2026-11-09", "replies": "x", "songs": {}})):
            with self.subTest(content):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(self.reload().snapshot()["replies"]["total"], 0)

    def test_bad_numbers_in_a_good_file_count_as_zero(self):
        (self.data / COUNTS_FILE).write_text(json.dumps({
            "version": 1, "date": "2026-11-09",
            "replies": {"private": -3, "group": True}, "songs": {"owner": "7", "group": 4},
        }), encoding="utf-8")
        snapshot = self.reload().snapshot()
        self.assertEqual(snapshot["replies"], {"private": 0, "group": 0, "total": 0})
        self.assertEqual(snapshot["songs"], {"owner": 0, "group": 4, "total": 4})

    def test_a_failed_write_keeps_counting_in_memory(self):
        with mock.patch.object(daily_module, "_write_json", side_effect=OSError("disk")):
            self.counts.add_reply("private")
            self.counts.add_reply("private")
        self.assertEqual(self.counts.snapshot()["replies"]["private"], 2)


class MoodSummaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1_000_000.0
        self.store = MoodStore(Path(self.directory.name), clock=lambda: self.now)
        self.settings = mood_settings({})

    def test_nothing_recorded_is_none(self):
        self.assertIsNone(self.store.summary("global", self.settings))
        self.assertIsNone(self.store.summary("owner", self.settings))

    def test_the_latest_emotion_with_its_strength(self):
        self.store.record("生气", ("global",), at=self.now - 3 * 3600)
        self.store.record("开心", ("global", "owner"), at=self.now - 60)
        summary = self.store.summary("global", self.settings)
        self.assertEqual(summary["emotion"], "开心")
        self.assertEqual(summary["age"], "刚才")
        self.assertEqual(summary["degree"], "还很明显")
        self.assertFalse(summary["calm"])
        self.assertEqual(self.store.summary("owner", self.settings)["emotion"], "开心")

    def test_an_old_mood_has_passed(self):
        self.store.record("害羞", ("owner",), at=self.now - 10 * 3600)
        summary = self.store.summary("owner", self.settings)
        self.assertEqual((summary["emotion"], summary["degree"], summary["calm"]), ("害羞", "已经平复", True))
        self.assertIsNone(self.store.summary("global", self.settings))

    def test_an_unknown_track_is_none(self):
        self.assertIsNone(self.store.summary("nobody", self.settings))


class ReplyCountingTests(Base):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.core._daily_counts = DailyCounts(Path(self.directory.name))

    def replies(self):
        return self.core._daily_counts.snapshot()["replies"]

    async def run_real(self, reply, *, chat=PRIVATE, fail=False):
        handler = self.core.handle_private if chat == PRIVATE else self.core.handle_group
        task = asyncio.create_task(drive(handler(real_message("你好", chat=chat, sender="u1"))))
        request = await _wait_for_request(self.manager)
        if fail:
            self.manager.claim_request(request.request_id, "h2-fail")
            self.manager.fail_request(request.request_id, "generation_failed")
        else:
            finish(self.manager, request, reply)
        await asyncio.wait_for(task, 20)

    async def test_a_private_reply_counts_as_private(self):
        await self.run_real(GOOD)
        self.assertEqual(self.replies(), {"private": 1, "group": 0, "total": 1})

    async def test_a_group_reply_counts_as_group(self):
        await self.run_real(GOOD, chat=GROUP)
        self.assertEqual(self.replies(), {"private": 0, "group": 1, "total": 1})

    async def test_a_reply_that_could_not_be_read_does_not_count(self):
        await self.run_real("格式不对")
        self.assertEqual(self.replies()["total"], 0)

    async def test_a_failed_request_does_not_count(self):
        await self.run_real(None, fail=True)
        self.assertEqual(self.replies()["total"], 0)

    async def test_a_test_chat_never_counts(self):
        task = asyncio.create_task(self.core.run_test_chat({"chat": "private", "text": "测试"}))
        request = await _wait_for_request(self.manager)
        finish(self.manager, request, GOOD)
        await asyncio.wait_for(task, 20)
        self.assertEqual(self.replies()["total"], 0)

    async def test_without_a_counter_nothing_breaks(self):
        self.core._daily_counts = None
        await self.run_real(GOOD)


class ProactiveNotCountedTests(PluginBase):
    async def test_a_plan_and_a_chat_message_are_not_replies(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.core._daily_counts = DailyCounts(Path(directory.name))
        await self.run_turn(self.core._run_proactive(plan(), False))
        await self.run_turn(self.core._run_proactive_chat(False, 200, 0))
        self.assertEqual(self.core._daily_counts.snapshot()["replies"]["total"], 0)


class SongCountingTests(Base):
    async def test_songs_count_by_track(self):
        with tempfile.TemporaryDirectory() as directory:
            self.core._daily_counts = DailyCounts(Path(directory))
            self.core._count_song("owner")
            self.core._count_song("group")
            self.assertEqual(self.core._daily_counts.snapshot()["songs"], {"owner": 1, "group": 1, "total": 2})
            self.core._daily_counts = None
            self.core._count_song("owner")  # no counter: nothing happens

    def test_a_delivered_song_is_counted_where_it_is_logged_as_sent(self):
        source = inspect.getsource(mixin_song)
        self.assertIn('result = "sent"\n            self._count_song(track)', source)


class OverviewTests(Base):
    async def test_every_part_is_looked_up_on_its_own(self):
        core = self.core
        core._daily_counts = None
        core._chat_scheduler = None
        core._mood_store = None
        view = core._today_overview()
        self.assertEqual((view["date"], view["replies"], view["songs"], view["chat"], view["mood"]), (None, None, None, None, None))
        self.assertEqual(view["queue"], {"active": False, "waiting": 0})

    async def test_the_parts_when_everything_is_there(self):
        core = self.core
        with tempfile.TemporaryDirectory() as directory:
            core._emotion_store = ConfigStore(Path(directory))
            core._daily_counts = DailyCounts(Path(directory))
            core._daily_counts.add_reply("private")
            core._mood_store = MoodStore(Path(directory))
            core._mood_store.record("开心", ("global", "owner"))
            core._chat_scheduler = type("S", (), {"snapshot": lambda self: {"reason": "Waiting", "next_at": "2026-11-09T21:00", "today": 1, "daily_limit": 3}})()
            view = core._today_overview()
            self.assertEqual(view["replies"]["private"], 1)
            self.assertEqual(view["chat"]["reason"], "Waiting")
            self.assertEqual(view["mood"]["owner"]["emotion"], "开心")
            self.assertEqual(view["mood"]["global"]["degree"], "还很明显")
            json.dumps(view)

    async def test_a_part_that_raises_leaves_only_itself_empty(self):
        core = self.core

        class Broken:
            def snapshot(self):
                raise RuntimeError("boom")

        core._chat_scheduler = Broken()
        core._daily_counts = Broken()
        view = core._today_overview()
        self.assertIsNone(view["chat"])
        self.assertIsNone(view["replies"])
        self.assertIsNotNone(view["queue"])


class StatusFieldTests(unittest.IsolatedAsyncioTestCase):
    async def get_status(self, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        port = _unused_loopback_port()
        service = ConsoleService(create_console_app(ConfigStore(Path(directory.name)), **kwargs), port=port)
        await service.start()
        try:
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/api/status") as response:
                    return response.status, await response.json()
        finally:
            await service.stop()

    async def test_the_today_field_comes_from_the_provider(self):
        payload = {"date": "2026-11-09", "replies": {"private": 2, "group": 1, "total": 3}, "songs": None, "chat": None, "mood": None, "queue": {"active": False, "waiting": 0}}
        status, body = await self.get_status(today_stats=lambda: payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["today"], payload)

    async def test_without_a_provider_or_with_a_broken_one_it_is_null(self):
        self.assertIsNone((await self.get_status())[1]["today"])

        def broken():
            raise RuntimeError("boom")

        status, body = await self.get_status(today_stats=broken)
        self.assertEqual(status, 200)
        self.assertIsNone(body["today"])


if __name__ == "__main__":
    unittest.main()
