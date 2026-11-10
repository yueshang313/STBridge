"""H1: the record of how the last requests to ST ended (shown by the console health chain)."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import BridgeRecoveryGate
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mixin_bridge import BridgeMixin
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.reply_stats import FAILURE_CODES, MAX_RECORDS, ReplyStats
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from test_console_server import _unused_loopback_port
from test_l1d_proactive import PluginBase, plan
from test_a1_testchat import Base, drive, finish, real_message
from test_plugin_lifecycle import _wait_for_request

GOOD = '[情绪:开心]☆‹「日」›"中"'


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 9, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stats = ReplyStats(clock=self.clock)

    def test_an_empty_record_is_a_normal_snapshot(self):
        snapshot = self.stats.snapshot()
        self.assertEqual(snapshot["count"], 0)
        self.assertEqual(snapshot["ok"], 0)
        self.assertEqual(snapshot["recent"], [])
        for key in ("last_ok_at", "last_ok_age", "last_fail_at", "last_fail_age", "last_fail_code", "avg_ms"):
            self.assertIsNone(snapshot[key], key)

    def test_counts_times_and_codes(self):
        self.stats.record("ok", 1000)
        self.clock.advance(60)
        self.stats.record("StFailed", 3000)
        self.clock.advance(30)
        self.stats.record("ok", 2000)
        self.clock.advance(10)
        snapshot = self.stats.snapshot()
        self.assertEqual((snapshot["count"], snapshot["ok"]), (3, 2))
        self.assertEqual(snapshot["recent"], ["ok", "StFailed", "ok"])
        self.assertEqual(snapshot["last_ok_at"], "2026-11-09T12:01:30")
        self.assertEqual(snapshot["last_ok_age"], 10)
        self.assertEqual(snapshot["last_fail_at"], "2026-11-09T12:01:00")
        self.assertEqual(snapshot["last_fail_age"], 40)
        self.assertEqual(snapshot["last_fail_code"], "StFailed")
        self.assertEqual(snapshot["avg_ms"], 2000)

    def test_only_the_last_twenty_are_kept(self):
        for index in range(MAX_RECORDS + 5):
            self.stats.record("ok" if index >= 10 else "Timeout", 100)
        snapshot = self.stats.snapshot()
        self.assertEqual(snapshot["count"], 20)
        self.assertEqual(snapshot["ok"], 15)
        self.assertEqual(snapshot["recent"].count("Timeout"), 5)
        self.assertEqual(len(snapshot["recent"]), 20)

    def test_the_average_covers_the_last_ten_with_a_time(self):
        for _ in range(5):
            self.stats.record("ok", 10_000)
        for _ in range(9):
            self.stats.record("ok", 1000)
        self.stats.record("InvalidReply", 0)  # no time known: not part of the average
        self.assertEqual(self.stats.snapshot()["avg_ms"], 1000)

    def test_unknown_codes_and_bad_times_are_handled(self):
        self.stats.record("Nonsense", 5)
        self.stats.record("QueueFull", 5)
        self.assertEqual(self.stats.snapshot()["count"], 0)
        self.stats.record("ok", None)
        self.stats.record("ok", float("inf"))
        self.stats.record("ok", -5)
        snapshot = self.stats.snapshot()
        self.assertEqual(snapshot["count"], 3)
        self.assertIsNone(snapshot["avg_ms"])

    def test_the_failure_code_table(self):
        self.assertEqual(FAILURE_CODES, ("StFailed", "Timeout", "EmptyReply", "InvalidReply"))


class StatusEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConfigStore(Path(self.directory.name))

    async def get_status(self, **kwargs):
        port = _unused_loopback_port()
        service = ConsoleService(create_console_app(self.store, **kwargs), port=port)
        await service.start()
        try:
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/api/status") as response:
                    return response.status, await response.json()
        finally:
            await service.stop()

    async def test_the_replies_field_comes_from_the_provider(self):
        stats = ReplyStats()
        stats.record("ok", 1500)
        stats.record("Timeout", 0)
        status, body = await self.get_status(reply_stats=stats.snapshot)
        self.assertEqual(status, 200)
        self.assertEqual(body["replies"]["count"], 2)
        self.assertEqual(body["replies"]["recent"], ["ok", "Timeout"])
        self.assertEqual(body["replies"]["last_fail_code"], "Timeout")

    async def test_an_empty_record_is_served_as_is(self):
        status, body = await self.get_status(reply_stats=ReplyStats().snapshot)
        self.assertEqual(status, 200)
        self.assertEqual(body["replies"]["count"], 0)
        self.assertEqual(body["replies"]["recent"], [])

    async def test_without_a_provider_or_with_a_broken_one_the_field_is_null(self):
        self.assertIsNone((await self.get_status())[1]["replies"])

        def broken():
            raise RuntimeError("boom")

        status, body = await self.get_status(reply_stats=broken)
        self.assertEqual(status, 200)
        self.assertIsNone(body["replies"])


class FailureCodeTests(unittest.TestCase):
    """Which ends of a request count as the model's failure."""

    def setUp(self):
        self.clock = Clock()
        self.now = 0.0
        self.manager = RequestStateManager(timeout_seconds=10, clock=lambda: self.now)
        self.service = SimpleNamespace(state=HttpServiceState.RUNNING)
        self.gate = BridgeRecoveryGate()
        self.gate.confirm_recovery()

    def code(self):
        return BridgeMixin._reply_failure_code(self.manager, self.request, self.service, self.gate)

    def create(self):
        self.request = self.manager.create_request("hi", timeout_seconds=10)
        return self.request

    def test_a_claimed_request_that_failed_is_stfailed(self):
        request = self.create()
        self.manager.claim_request(request.request_id, "attempt-1")
        self.manager.fail_request(request.request_id, "generation_failed")
        self.assertEqual(self.code(), "StFailed")

    def test_a_request_st_never_took_is_nobodys_model_failure(self):
        request = self.create()
        self.manager.fail_request(request.request_id, "extension_error")
        self.assertIsNone(self.code())

    def test_a_claimed_request_that_ran_out_of_time_is_a_timeout(self):
        request = self.create()
        self.manager.claim_request(request.request_id, "attempt-1")
        self.now += 11
        self.manager.expire_due_request()
        self.assertEqual(self.code(), "Timeout")

    def test_a_request_nobody_claimed_that_ran_out_of_time_is_not_counted(self):
        self.create()
        self.now += 11
        self.manager.expire_due_request()
        self.assertIsNone(self.code())

    def test_a_stopped_bridge_or_protection_is_not_counted(self):
        request = self.create()
        self.manager.claim_request(request.request_id, "attempt-1")
        self.manager.fail_request(request.request_id, "generation_failed")
        self.service.state = HttpServiceState.STOPPED
        self.assertIsNone(self.code())
        self.service.state = HttpServiceState.RUNNING
        self.assertEqual(self.code(), "StFailed")

    def test_an_unknown_request_is_not_counted(self):
        self.request = SimpleNamespace(request_id="nope")
        self.assertIsNone(self.code())


class ChatTurnTests(Base):
    """A private owner turn records how it ended; a test chat never does."""

    def snapshot(self):
        return self.core._reply_stats.snapshot()

    async def run_real(self, reply, *, claim=True):
        task = asyncio.create_task(drive(self.core.handle_private(real_message("你好"))))
        request = await _wait_for_request(self.manager)
        if reply == "fail":
            if claim:
                self.manager.claim_request(request.request_id, "h1-fail")
            self.manager.fail_request(request.request_id, "generation_failed")
        else:
            finish(self.manager, request, reply)
        await asyncio.wait_for(task, 20)

    async def test_a_good_reply_is_ok(self):
        await self.run_real(GOOD)
        snapshot = self.snapshot()
        self.assertEqual((snapshot["count"], snapshot["ok"], snapshot["recent"]), (1, 1, ["ok"]))
        self.assertGreaterEqual(snapshot["avg_ms"], 0)

    async def test_st_failing_after_taking_the_request_is_stfailed(self):
        await self.run_real("fail")
        self.assertEqual(self.snapshot()["recent"], ["StFailed"])
        self.assertEqual(self.snapshot()["last_fail_code"], "StFailed")

    async def test_a_reply_that_cannot_be_read_is_invalidreply(self):
        await self.run_real("这不是约定的格式")
        self.assertEqual(self.snapshot()["recent"], ["InvalidReply"])

    async def test_a_request_st_never_took_is_not_recorded(self):
        await self.run_real("fail", claim=False)
        self.assertEqual(self.snapshot()["count"], 0)

    async def test_a_group_turn_counts_too(self):
        task = asyncio.create_task(drive(self.core.handle_group(real_message("群里", chat="group", sender="g-user"))))
        request = await _wait_for_request(self.manager)
        finish(self.manager, request, GOOD)
        await asyncio.wait_for(task, 20)
        self.assertEqual(self.snapshot()["recent"], ["ok"])

    async def test_a_test_chat_is_never_recorded(self):
        await self.run_chat({"chat": "private", "text": "测试"}, GOOD)
        await self.run_chat({"chat": "private", "text": "测试"}, "格式不对")
        await self.run_chat({"chat": "private", "text": "测试"}, None)
        self.assertEqual(self.snapshot()["count"], 0)

    async def test_the_results_are_kept_in_order(self):
        await self.run_real(GOOD)
        await self.run_real("这不是约定的格式")
        await self.run_real(GOOD)
        self.assertEqual(self.snapshot()["recent"], ["ok", "InvalidReply", "ok"])


class ProactiveTests(PluginBase):
    def recent(self):
        return self.core._reply_stats.snapshot()["recent"]

    async def test_a_plan_with_a_good_reply_is_ok(self):
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False))
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.recent(), ["ok"])

    async def test_a_chat_message_is_recorded_too(self):
        await self.run_turn(self.core._run_proactive_chat(False, 200, 0))
        self.assertEqual(self.recent(), ["ok"])

    async def test_st_failing_is_stfailed_even_when_the_fallback_text_goes_out(self):
        task = asyncio.create_task(self.core._run_proactive(plan(), False))
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        manager.claim_request(request.request_id, "h1-proactive")
        manager.fail_request(request.request_id, "generation_failed")
        result = await asyncio.wait_for(task, 10)
        self.assertEqual(result["result"], "fallback")
        self.assertEqual(self.recent(), ["StFailed"])

    async def test_a_failure_before_st_took_the_request_is_not_recorded(self):
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False), st="fail")
        self.assertEqual(result["result"], "fallback")
        self.assertEqual(self.recent(), [])

    async def test_an_unreadable_reply_is_invalidreply(self):
        await self.run_turn(self.core._run_proactive(plan(), False), reply="随便说说")
        self.assertEqual(self.recent(), ["InvalidReply"])

    async def test_a_repeated_message_is_still_a_working_model(self):
        self.core._schedule_targets.set_last_text(self.core._schedule_targets.owner_conversation, "早安，制作人。")
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False, chat=True))
        self.assertEqual(self.recent(), ["ok"])

    async def test_nothing_asked_of_st_is_nothing_recorded(self):
        self.core._http_service = SimpleNamespace(state=HttpServiceState.STOPPED)
        result, _ = await self.run_turn(self.core._run_proactive(plan(), False), st=None)
        self.assertEqual(self.recent(), [])



# ---- H1.1: an "unavailable" text nobody edited follows the new default ----

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import reply_texts  # noqa: E402
from test_plugin_lifecycle import _load_plugin_module, _ready_plugin  # noqa: E402

OLD_UNAVAILABLE = "桥接当前不可用，请检查服务状态与恢复保护。"
NEW_UNAVAILABLE = "{昵称}现在不在线哦，晚点再来找我～"


class OldUnavailableDefaultTests(unittest.IsolatedAsyncioTestCase):
    def test_the_old_default_is_what_the_code_says_it_is(self):
        self.assertEqual(reply_texts.OLD_UNAVAILABLE_DEFAULT, OLD_UNAVAILABLE)
        self.assertEqual(reply_texts.default_texts()["unavailable"], NEW_UNAVAILABLE)

    def test_a_stored_old_default_reads_as_the_new_default(self):
        texts, replaced = reply_texts.lenient_texts({"unavailable": OLD_UNAVAILABLE})
        self.assertEqual(texts["unavailable"], NEW_UNAVAILABLE)
        self.assertEqual(replaced, [])
        self.assertEqual(reply_texts.texts_settings({"texts": {"unavailable": f"  {OLD_UNAVAILABLE} "}})["unavailable"], NEW_UNAVAILABLE)

    def test_an_edited_text_stays(self):
        for edited in ("稍等一下哦", "桥接当前不可用，请检查服务状态与恢复保护。。", NEW_UNAVAILABLE, "{昵称}睡着了"):
            with self.subTest(edited):
                self.assertEqual(reply_texts.texts_settings({"texts": {"unavailable": edited}})["unavailable"], edited)

    def test_other_texts_that_equal_it_are_not_touched(self):
        texts = reply_texts.texts_settings({"texts": {"too_long": OLD_UNAVAILABLE, "unavailable": OLD_UNAVAILABLE}})
        self.assertEqual(texts["too_long"], OLD_UNAVAILABLE)
        self.assertEqual(texts["unavailable"], NEW_UNAVAILABLE)

    def test_a_broken_unavailable_still_falls_back_and_is_reported(self):
        texts, replaced = reply_texts.lenient_texts({"unavailable": "x" * 500})
        self.assertEqual(texts["unavailable"], NEW_UNAVAILABLE)
        self.assertEqual(replaced, [("unavailable", "length")])

    async def test_the_reply_the_bridge_sends_uses_the_new_default_and_the_nickname(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        core = plugin._core
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        core._emotion_store = ConfigStore(Path(directory.name))
        core._emotion_store.config["texts"]["unavailable"] = OLD_UNAVAILABLE
        self.assertEqual(core._reply_text("unavailable"), "琴音现在不在线哦，晚点再来找我～")
        core._emotion_store.config["texts"]["unavailable"] = "我去睡了"
        self.assertEqual(core._reply_text("unavailable"), "我去睡了")

    async def test_the_console_shows_the_new_default_and_the_file_is_untouched(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ConfigStore(Path(directory.name))
        store.config["texts"]["unavailable"] = OLD_UNAVAILABLE
        port = _unused_loopback_port()
        service = ConsoleService(create_console_app(store), port=port)
        await service.start()
        try:
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/api/texts") as response:
                    body = await response.json()
        finally:
            await service.stop()
        self.assertEqual(body["texts"]["unavailable"], NEW_UNAVAILABLE)
        self.assertEqual(store.config["texts"]["unavailable"], OLD_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
