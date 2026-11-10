"""P7c.2: the daily song limit (count store, permission, pick / link / command behaviour, refunds, logs)."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from package_context import install_plugin_namespace
from test_plugin_lifecycle import submodule

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import CoverError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import EmotionConfigError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_quota import SongQuota
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_request import (
    default_song_request_settings,
    song_request_settings,
    validate_song_request_settings,
)
from test_song_flow import (
    ACCEPT,
    FAIL,
    GROUP_UMO,
    MEMBER_KEY,
    OWNER_KEY,
    OWNER_UMO,
    REFUSE,
    SongFlowBase,
    user_hash,
)

LIMIT = default_song_request_settings()["limit_text"]
LIMIT_5 = LIMIT.replace("{次数}", "5")
DAY1 = datetime(2026, 11, 2, 23, 59, 0)


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


class QuotaStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.now = DAY1
        self.logger = FakeLogger()
        self.quota = self.make()

    def make(self):
        return SongQuota(self.path, clock=lambda: self.now, logger=self.logger)

    def test_consume_up_to_the_limit_then_refuse(self):
        self.assertEqual([self.quota.try_consume("a", 3) for _ in range(5)], [True, True, True, False, False])
        self.assertEqual(self.quota.used("a"), 3)
        self.assertEqual(self.quota.used("b"), 0)

    def test_zero_limit_never_counts(self):
        self.assertTrue(self.quota.try_consume("a", 0))
        self.assertEqual(self.quota.used("a"), 0)

    def test_refund_stops_at_zero(self):
        self.quota.try_consume("a", 5)
        self.quota.try_consume("a", 5)
        self.quota.refund("a")
        self.assertEqual(self.quota.used("a"), 1)
        self.quota.refund("a")
        self.quota.refund("a")
        self.quota.refund("never")
        self.assertEqual(self.quota.used("a"), 0)
        self.assertTrue(self.quota.try_consume("a", 1))

    def test_the_count_resets_at_local_midnight(self):
        self.quota.try_consume("a", 1)
        self.assertFalse(self.quota.try_consume("a", 1))
        self.now = DAY1 + timedelta(minutes=2)  # 00:01 the next day
        self.assertEqual(self.quota.used("a"), 0)
        self.assertTrue(self.quota.try_consume("a", 1))

    def test_a_refund_after_midnight_is_void(self):
        date = self.quota.today()
        self.quota.try_consume("a", 5)
        self.now = DAY1 + timedelta(minutes=2)
        self.quota.try_consume("a", 5)  # today's first song
        self.quota.refund("a", date)  # yesterday's song failing now
        self.assertEqual(self.quota.used("a"), 1)
        self.quota.refund("a", self.quota.today())
        self.assertEqual(self.quota.used("a"), 0)

    def test_counts_survive_a_reload_but_not_a_new_day(self):
        self.quota.try_consume("a", 5)
        self.quota.try_consume("group:b", 5)
        self.assertEqual(json.loads((self.path / "song_quota.json").read_text(encoding="utf-8")),
                         {"version": 1, "date": "2026-11-02", "counts": {"a": 1, "group:b": 1}})
        again = self.make()
        self.assertEqual((again.used("a"), again.used("group:b")), (1, 1))
        self.now = DAY1 + timedelta(hours=1)
        self.assertEqual(self.make().used("a"), 0)

    def test_a_broken_file_means_empty_counts_and_a_log_line(self):
        for content in ("{not json", "[]", '{"version": 2, "date": "2026-11-02", "counts": {}}',
                        '{"version": 1, "date": 5, "counts": {}}', '{"version": 1, "date": "2026-11-02", "counts": 3}'):
            with self.subTest(content=content):
                self.logger.warnings.clear()
                (self.path / "song_quota.json").write_text(content, encoding="utf-8")
                quota = self.make()
                self.assertEqual(quota.used("a"), 0)
                self.assertTrue(quota.try_consume("a", 1))
                self.assertEqual(len(self.logger.warnings), 1)

    def test_bad_entries_are_dropped(self):
        (self.path / "song_quota.json").write_text(
            json.dumps({"version": 1, "date": "2026-11-02",
                        "counts": {"a": 2, "b": -1, "c": "x", "d": True, "e": 0}}), encoding="utf-8")
        quota = self.make()
        self.assertEqual([quota.used(key) for key in "abcde"], [2, 0, 0, 0, 0])

    def test_an_unwritable_file_does_not_stop_counting(self):
        with patch("data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_quota._write_json", side_effect=OSError("disk")):
            self.assertTrue(self.quota.try_consume("a", 2))
            self.quota.refund("a")
        self.assertEqual(len(self.logger.warnings), 2)
        self.assertEqual(self.quota.used("a"), 0)


class QuotaSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = default_song_request_settings()
        self.assertEqual((settings["daily_limit"], settings["limit_owner"]), (5, False))
        self.assertEqual(settings["limit_text"], "今天已经唱了{次数}首啦，嗓子要休息一下，明天再来吧～")
        self.assertEqual(validate_song_request_settings({}), settings)

    def test_an_older_config_gets_the_defaults(self):
        old = default_song_request_settings()
        for field in ("daily_limit", "limit_owner", "limit_text"):
            del old[field]
        result = song_request_settings({"song_request": old})
        self.assertEqual((result["daily_limit"], result["limit_owner"]), (5, False))
        self.assertEqual(result["limit_text"], LIMIT)

    def test_bounds(self):
        for value in (0, 1, 100, 7.0):
            self.assertEqual(validate_song_request_settings({"daily_limit": value})["daily_limit"], int(value))
        for value in (-1, 101, 2.5, True, "5", None, float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_song_request_settings({"daily_limit": value})
                self.assertEqual(caught.exception.status, 400)

    def test_limit_owner_and_text(self):
        self.assertTrue(validate_song_request_settings({"limit_owner": True})["limit_owner"])
        for value in (1, "true", None):
            with self.assertRaises(EmotionConfigError):
                validate_song_request_settings({"limit_owner": value})
        self.assertEqual(validate_song_request_settings({"limit_text": "  满了  "})["limit_text"], "满了")
        self.assertEqual(len(validate_song_request_settings({"limit_text": "x" * 100})["limit_text"]), 100)
        for value in ("", "   ", "x" * 101, None, 5, ["a"]):
            with self.subTest(value=value):
                with self.assertRaises(EmotionConfigError):
                    validate_song_request_settings({"limit_text": value})


class QuotaFlowTests(SongFlowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now_dt = DAY1
        self.plugin._core._song_now = lambda: self.now_dt
        self.quota = SongQuota(Path(self.directory.name), clock=lambda: self.now_dt)
        self.plugin._core._song_quota = self.quota

    def speaker(self, group=True):
        return self.plugin._core._speaker("qq-platform", "member-a" if group else "qq-user", "n", group=group)

    # ---- permission

    async def test_the_fifth_song_is_the_last_one_a_member_may_order(self):
        for used in range(5):
            with self.subTest(used=used):
                self.assertEqual(self.quota.used(MEMBER_KEY), used)
                speaker = self.speaker()
                self.assertEqual(speaker.writes[2], {"path": ["点歌许可"], "value": 1})
                self.quota.try_consume(MEMBER_KEY, 5)
        speaker = self.speaker()
        self.assertEqual(speaker.writes[2], {"path": ["点歌许可"], "value": 0})
        self.assertEqual((speaker.sing_allowed, speaker.sing_reason), (False, "quota"))

    async def test_the_owner_is_not_limited_unless_switched_on(self):
        for _ in range(20):
            self.quota.try_consume(OWNER_KEY, 100)
        self.assertEqual(self.speaker(group=False).writes[2]["value"], 1)
        await self.set_song_settings(limit_owner=True)
        speaker = self.speaker(group=False)
        self.assertEqual((speaker.writes[2]["value"], speaker.sing_reason), (0, "quota"))
        await self.set_song_settings(daily_limit=21)
        self.assertEqual(self.speaker(group=False).writes[2]["value"], 1)

    async def test_zero_means_unlimited(self):
        await self.set_song_settings(daily_limit=0, limit_owner=True)
        for _ in range(50):
            self.quota.try_consume(MEMBER_KEY, 100)
        self.assertEqual(self.speaker().writes[2]["value"], 1)
        self.assertEqual(self.quota.used(MEMBER_KEY), 50)
        counted, date = self.plugin._core._song_consume(MEMBER_KEY, self.module.GROUP_TRACK)
        self.assertEqual((counted, date), (True, None))

    async def test_the_quota_is_checked_before_the_refusal_roll_and_after_the_threshold(self):
        rolls = []
        self.plugin._core._song_random = lambda: rolls.append(1) or 0.99
        for _ in range(5):
            self.quota.try_consume(MEMBER_KEY, 5)
        self.assertFalse(self.speaker().sing_allowed)
        self.assertEqual(rolls, [])  # refusal rate is 0 and the quota already said no: no roll
        self.plugin._core._affinity_store.set_value(MEMBER_KEY, 10, low=-50, high=100)
        self.assertEqual(self.speaker().sing_reason, "other")  # the threshold comes first

    async def test_the_limit_resets_at_midnight(self):
        for _ in range(5):
            self.quota.try_consume(MEMBER_KEY, 5)
        self.assertEqual(self.speaker().writes[2]["value"], 0)
        self.now_dt = DAY1 + timedelta(minutes=2)
        self.assertEqual(self.speaker().writes[2]["value"], 1)

    async def test_without_a_quota_store_nothing_is_limited(self):
        self.plugin._core._song_quota = None
        self.assertEqual(self.speaker().writes[2]["value"], 1)
        self.assertEqual(self.plugin._core._song_consume(MEMBER_KEY, self.module.GROUP_TRACK), (True, None))

    # ---- counting

    async def test_picking_counts_one_and_success_keeps_it(self):
        self.seed(group=True)
        await self.say("2", group=True)
        self.assertEqual(self.quota.used(MEMBER_KEY), 1)
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(self.quota.used(MEMBER_KEY), 1)
        line = self.song_logs()[0]
        self.assertIn("result=sent", line)
        self.assertTrue(line.endswith("quota=1/5"), line)

    async def test_a_failed_delivery_gives_the_song_back(self):
        for error in (CoverError("preview_only"), RuntimeError("boom")):
            with self.subTest(error=error):
                self.cover.resolve_error = error
                self.module.test_logger.infos.clear()
                before = self.quota.used(MEMBER_KEY)
                self.seed(group=True)
                await self.say("2", group=True)
                self.assertEqual(self.quota.used(MEMBER_KEY), before + 1)
                await self.finish_delivery(MEMBER_KEY)
                await asyncio.sleep(0)
                self.assertEqual(self.quota.used(MEMBER_KEY), before)
                self.assertTrue(self.song_logs()[0].endswith(f"quota={before}/5"), self.song_logs()[0])

    async def test_a_failed_send_gives_the_song_back_too(self):
        self.send_error = RuntimeError("platform")
        self.seed(group=True)
        await self.say("2", group=True)
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(self.quota.used(MEMBER_KEY), 0)

    async def test_a_cancelled_delivery_gives_the_song_back(self):
        self.cover.gate = asyncio.Event()
        self.seed(group=True)
        await self.say("2", group=True)
        await asyncio.sleep(0)
        self.assertEqual(self.quota.used(MEMBER_KEY), 1)
        self.plugin._core._http_service = None
        await self.plugin.terminate()
        self.assertEqual(self.quota.used(MEMBER_KEY), 0)
        self.assertIn("failed:Cancelled", self.song_logs()[0])

    async def test_a_refund_after_midnight_does_not_touch_the_new_day(self):
        self.cover.gate = asyncio.Event()
        self.cover.sing_error = CoverError("Timeout")
        self.seed(group=True)
        await self.say("2", group=True)
        await asyncio.sleep(0)
        self.now_dt = DAY1 + timedelta(minutes=5)
        self.quota.try_consume(MEMBER_KEY, 5)  # the first song of the new day
        self.cover.gate.set()
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(self.quota.used(MEMBER_KEY), 1)

    async def test_looking_cancelling_and_timing_out_cost_nothing(self):
        self.seed(group=True)
        await self.say("0 周杰伦", group=True)
        await self.say("9", group=True)
        await self.say("取消", group=True)
        self.seed(group=True)
        self.plugin._core._song_clock = lambda: 10 ** 6
        self.assertIsNone(await self.say("2", group=True))
        self.assertEqual(self.quota.used(MEMBER_KEY), 0)
        replies = await self.sing_command("唱 晴天", group=True)
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.quota.used(MEMBER_KEY), 0)

    async def sing_command(self, text, *, group=False):
        event = self.make_event(text, group=group)
        return [item.text for item in await self.run_handler(self.plugin.stbridge_sing, event)]

    async def test_a_cached_cover_still_counts(self):
        async def cached(song_id, **kwargs):
            return {"cached": True, "cover": {"key": "abc123"}}

        self.cover.sing = cached
        self.seed(group=True)
        await self.say("2", group=True)
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(self.quota.used(MEMBER_KEY), 1)

    async def test_the_owner_is_not_counted_and_logs_quota_off(self):
        self.seed()
        await self.say("2")
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.quota.used(OWNER_KEY), 0)
        self.assertTrue(self.song_logs()[0].endswith("quota=off"), self.song_logs()[0])

    async def test_the_owner_counts_and_refunds_when_limited(self):
        await self.set_song_settings(limit_owner=True)
        self.cover.resolve_error = CoverError("Timeout")
        self.seed()
        await self.say("2")
        self.assertEqual(self.quota.used(OWNER_KEY), 1)
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.quota.used(OWNER_KEY), 0)

    # ---- being full

    async def fill(self, key=MEMBER_KEY):
        for _ in range(5):
            self.quota.try_consume(key, 5)

    async def test_picking_when_full_says_so_and_drops_the_list(self):
        await self.fill()
        self.seed(group=True)
        self.assertEqual(await self.say("2", group=True), [LIMIT_5])
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertEqual(self.plugin._core._song_tasks, {})
        self.assertEqual(self.quota.used(MEMBER_KEY), 5)
        self.assertIn(f"user={user_hash(MEMBER_KEY)} via=tag pick=none result=skip:QuotaExceeded used=5/5",
                      self.song_logs()[0])

    async def test_a_busy_pick_does_not_consume(self):
        self.cover.gate = asyncio.Event()
        self.seed(group=True)
        await self.say("2", group=True)
        await asyncio.sleep(0)
        self.seed(group=True)
        self.assertEqual(await self.say("3", group=True), [self.module.SONG_BUSY_TEXT])
        self.assertEqual(self.quota.used(MEMBER_KEY), 1)
        self.cover.gate.set()
        await self.finish_delivery(MEMBER_KEY)

    async def test_the_command_says_the_limit_line_only_for_the_quota(self):
        await self.fill()
        self.assertEqual(await self.sing_command("唱 晴天", group=True), [LIMIT_5])
        self.assertEqual(self.cover.calls, [])
        await self.set_texts(limit_text="今天{次数}首满了")
        self.assertEqual(await self.sing_command("唱 晴天", group=True), ["今天5首满了"])
        # not allowed for another reason: the ordinary refusal
        self.quota.refund(MEMBER_KEY)
        self.plugin._core._affinity_store.set_value(MEMBER_KEY, 10, low=-50, high=100)
        self.assertEqual(await self.sing_command("唱 晴天", group=True), [REFUSE])
        self.assertIn("result=skip:QuotaExceeded used=5/5", self.song_logs()[0])
        self.assertIn("result=skip:NotAllowed", self.song_logs()[-1])

    async def test_a_link_when_full_says_the_limit_line(self):
        detail_calls = []

        async def detail(song_id, **kwargs):
            detail_calls.append(song_id)
            return {"name": "向云端", "artist": "", "album": ""}

        await self.fill()
        with patch.object(submodule(self.module, "mixin_song"), "fetch_netease_song", detail):
            self.assertEqual(await self.say("https://music.163.com/song?id=5", group=True), [LIMIT_5])
            self.assertEqual(detail_calls, [])
            self.assertEqual(self.plugin._core._song_tasks, {})
            self.assertIn("via=link pick=none result=skip:QuotaExceeded used=5/5", self.song_logs()[0])
            # other reasons keep the ordinary refusal
            self.quota.refund(MEMBER_KEY)
            self.plugin._core._affinity_store.set_value(MEMBER_KEY, 10, low=-50, high=100)
            self.assertEqual(await self.say("https://music.163.com/song?id=5", group=True), [REFUSE])

    async def test_a_link_counts_and_refunds(self):
        async def detail(song_id, **kwargs):
            return {"name": "向云端", "artist": "", "album": ""}

        self.cover.resolve_error = CoverError("Timeout")
        with patch.object(submodule(self.module, "mixin_song"), "fetch_netease_song", detail):
            self.assertEqual(await self.say("https://music.163.com/song?id=5", group=True),
                             [ACCEPT.replace("{歌名}", "向云端")])
            self.assertEqual(self.quota.used(MEMBER_KEY), 1)
            await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(self.quota.used(MEMBER_KEY), 0)

    async def test_the_tag_path_is_refused_when_full(self):
        await self.fill()
        speaker = self.speaker()
        self.assertIsNone(self.plugin._core._start_song_tag_search(speaker, "晴天"))
        self.assertEqual(self.cover.calls, [])
        self.assertIn("result=skip:QuotaExceeded used=5/5", self.song_logs()[0])

    async def test_members_share_one_count_across_groups(self):
        self.quota.try_consume(MEMBER_KEY, 5)
        other_group = self.plugin._core._speaker("qq-platform", "member-a", "n", group=True)
        self.assertEqual(other_group.key, MEMBER_KEY)
        self.assertEqual(self.quota.used(other_group.key), 1)


if __name__ == "__main__":
    unittest.main()
