import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore, group_key, user_hash, user_key
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    default_queue_settings,
    queue_settings,
    validate_queue_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_queue import BridgeQueue
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import GenerationStatus
from test_plugin_lifecycle import (
    _bridge_event,
    _collect,
    _group_bridge_event,
    _load_plugin_module,
    _ready_plugin,
)


CHARACTER = "藤田琴音"


def bilingual(japanese, chinese, delta=None, variable=f"{CHARACTER}.好感度"):
    reply = f'☆‹{japanese}›"{chinese}"'
    if delta is not None:
        reply += f"<UpdateVariable>\n_.add('{variable}', {delta});\n</UpdateVariable>"
    return reply


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PluginQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.logger = self.module.test_logger
        for messages in (self.logger.warnings, self.logger.errors, self.logger.infos):
            messages.clear()
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-queue-")
        self.plugin = _ready_plugin(self.module, voice=True)
        self.plugin._core._affinity_store = AffinityStore(Path(self._directory.name))
        self.manager = self.plugin._core._request_state_manager
        self.enterContext(patch.dict(sys.modules, self.module.TestBotpyModules))
        self.seen = set()

    async def asyncTearDown(self):
        self._directory.cleanup()

    def group(self, text, sender_id, name):
        event = _group_bridge_event(self.module, text, sender_id=sender_id, sender_name=name)
        return asyncio.create_task(_collect(self.plugin.stbridge_group_message, event)), event

    def private(self, text):
        event = _bridge_event(self.module, text)
        return asyncio.create_task(_collect(self.plugin.stbridge_private_message, event)), event

    async def settle(self):
        # A message now goes through the connector protocol first, which takes more loop turns.
        for _ in range(200):
            await asyncio.sleep(0)

    async def next_request(self):
        for _ in range(6000):  # up to a minute: the clock only matters when something is really broken
            request = self.manager.peek_pending_request()
            if request is not None and request.request_id not in self.seen:
                self.seen.add(request.request_id)
                return request
            await asyncio.sleep(0.01)
        raise AssertionError("no new ST request")

    def complete(self, request, reply):
        self.manager.claim_request(request.request_id, f"attempt-{request.request_id}")
        self.manager.mark_generation_started(request.request_id)
        self.manager.confirm_generation_finished(request.request_id)
        self.manager.complete_request(request.request_id, reply)

    async def test_three_concurrent_users_are_answered_in_arrival_order(self):
        finished = []

        def track(name, task):
            task.add_done_callback(lambda _: finished.append(name))
            return task

        a, event_a = self.group("一", "member-a", "小明")
        track("a", a)
        await self.settle()
        b, event_b = self.group("二", "member-b", "小红")
        track("b", b)
        await self.settle()
        c, event_c = self.group("三", "member-c", "")
        track("c", c)
        await self.settle()
        self.assertEqual(self.plugin._core._queue.waiting_count, 2)

        contents = []
        for name in ("a", "b", "c"):
            request = await self.next_request()
            contents.append(request.content)
            self.complete(request, bilingual(f"日本語{name}", f"中文{name}"))
            await asyncio.sleep(0.01)

        results = await asyncio.gather(a, b, c)
        self.assertEqual(finished, ["a", "b", "c"])
        self.assertEqual(contents, [
            "【群友】小明（1）[好感度:0]：一",
            "【群友】小红（1）[好感度:0]：二",
            "【群友】用户A[好感度:0]：三",
        ])
        # Only the replies that had to wait mention the sender; TTS text never does.
        self.assertEqual(
            [[item.text for item in result] for result in results],
            [["中文a"], ["@小红 中文b"], ["@用户A 中文c"]],
        )
        self.assertEqual([text for text, _ in self.plugin._core._tts.calls], ["日本語a", "日本語b", "日本語c"])
        self.assertEqual(self.plugin._core._queue.waiting_count, 0)
        hashed_b = user_hash(user_key("qq-platform", "member-b"))
        self.assertIn(f"[STBridge] Queue enqueued user={hashed_b} pos=1", self.logger.infos)
        self.assertTrue(any(
            line.startswith(f"[STBridge] Queue dispatched user={hashed_b} waited=")
            for line in self.logger.infos
        ))
        logs = "".join(self.logger.infos + self.logger.warnings)
        for secret in ("小明", "小红", "member-", "中文", "二"):
            self.assertNotIn(secret, logs)

    async def test_the_rejection_line_comes_from_the_texts_config_with_the_nickname(self):
        store = ConfigStore(Path(self._directory.name) / "console")
        self.plugin._core._emotion_store = store

        def edit(config):
            config["characters"]["小夜"] = {
                "name": "小夜", "model_name": "", "default_emotion": "neutral", "emotions": {},
                "nickname": "夜夜", "st_name": "XiaoYe",
            }
            config["default_character"] = "小夜"

        await store.update(edit)
        tasks = [self.group("先", "member-0", "甲")[0]]
        await self.settle()
        for index in range(1, 6):
            tasks.append(self.group("等", f"member-{index}", f"乙{index}")[0])
            await self.settle()
        rejected, _ = self.group("满", "member-6", "丙")
        self.assertEqual([item.text for item in await rejected], ["现在找夜夜的人太多啦，请稍后再来～"])
        await store.update(lambda config: config["texts"].update(queue_full="{昵称}正忙，等等再来"))
        rejected, _ = self.group("满", "member-7", "丁")
        self.assertEqual([item.text for item in await rejected], ["夜夜正忙，等等再来"])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_sixth_waiter_is_rejected_without_request(self):
        tasks = [self.group("先", "member-0", "甲")[0]]
        await self.settle()
        for index in range(1, 6):
            tasks.append(self.group("等", f"member-{index}", f"乙{index}")[0])
            await self.settle()
        self.assertEqual(self.plugin._core._queue.waiting_count, 5)

        rejected, _ = self.group("满", "member-6", "丙")
        self.assertEqual([item.text for item in await rejected], [self.module.QUEUE_FULL_TEXT])
        self.assertEqual(self.plugin._core._queue.waiting_count, 5)
        self.assertTrue(any(line.startswith("[STBridge] Queue full") for line in self.logger.warnings))

        self.plugin._core._http_service.state = HttpServiceState.STOPPED
        await asyncio.gather(*tasks)
        self.assertEqual(self.plugin._core._queue.waiting_count, 0)

    async def test_queue_limit_comes_from_console_settings(self):
        store = ConfigStore(Path(self._directory.name) / "console")
        await store.update(lambda config: config.update(queue={"max_waiting": 1, "max_wait_seconds": 30}))
        self.plugin._core._emotion_store = store
        first, _ = self.group("先", "member-a", "甲")
        await self.settle()
        waiting, _ = self.group("等", "member-b", "乙")
        await self.settle()
        rejected, _ = self.group("满", "member-c", "丙")
        self.assertEqual([item.text for item in await rejected], [self.module.QUEUE_FULL_TEXT])
        self.plugin._core._http_service.state = HttpServiceState.STOPPED
        await asyncio.gather(first, waiting)

    async def test_wait_timeout_removes_entry_with_plain_notice(self):
        clock = FakeClock()
        self.plugin._core._queue = BridgeQueue(clock=clock, poll_interval=0.01)
        first, _ = self.group("先", "member-a", "小明")
        await self.settle()
        late, event = self.group("后", "member-b", "小红")
        await self.settle()
        clock.now += 181
        results = await late
        self.assertEqual([item.text for item in results], [f"@小红 {self.module.QUEUE_TIMEOUT_TEXT}"])
        self.assertIsNone(self.plugin._core._tts.eligible)
        self.assertEqual(self.plugin._core._queue.waiting_count, 0)
        self.assertTrue(any(line.startswith("[STBridge] Queue wait timed out") for line in self.logger.warnings))

        request = await self.next_request()
        self.complete(request, bilingual("日本語", "中文"))
        self.assertEqual([item.text for item in await first], ["中文"])
        self.assertIsNone(self.manager.peek_pending_request())

    async def test_private_messages_of_a_waiting_user_are_merged(self):
        blocker = self.manager.create_request("someone else")
        first, _ = self.private("第一句")
        await self.settle()
        second, second_event = self.private("第二句")
        self.assertEqual(await second, [])
        self.assertTrue(second_event.call_llm)
        self.assertFalse(second_event.stopped)

        self.manager.fail_request(blocker.request_id, "test_cleanup")
        self.seen.add(blocker.request_id)
        request = await self.next_request()
        self.assertEqual(request.content, "第一句\n第二句")
        self.complete(request, bilingual("日本語", "中文"))
        self.assertEqual([item.text for item in await first], ["中文"])
        self.assertIsNone(self.manager.peek_pending_request())
        self.assertTrue(any(line.startswith("[STBridge] Queue merged") for line in self.logger.infos))

    async def test_group_messages_of_a_waiting_user_are_merged(self):
        first, _ = self.group("先", "member-a", "小明")
        await self.settle()
        b1, _ = self.group("一", "member-b", "小红")
        await self.settle()
        b2, _ = self.group("二", "member-b", "小红")
        self.assertEqual(await b2, [])
        self.assertEqual(self.plugin._core._queue.waiting_count, 1)

        self.complete(await self.next_request(), bilingual("日本語", "中文a"))
        await first
        request = await self.next_request()
        self.assertEqual(request.content, "【群友】小红（1）[好感度:0]：一\n【群友】小红（1）[好感度:0]：二")
        self.complete(request, bilingual("日本語", "中文b"))
        self.assertEqual([item.text for item in await b1], ["@小红 中文b"])

    async def test_merge_that_would_exceed_size_limit_is_rejected(self):
        blocker = self.manager.create_request("someone else")
        limit = self.module.DEFAULT_MAX_REQUEST_TEXT_BYTES
        first, _ = self.private("x" * (limit - 10))
        await self.settle()
        second, _ = self.private("y" * 20)
        self.assertEqual([item.text for item in await second], [self.module.TOO_LONG_TEXT])
        entry = self.plugin._core._queue.waiting_entry(user_key("qq-platform", "qq-user"))
        self.assertEqual(len(entry.texts), 1)

        self.manager.fail_request(blocker.request_id, "test_cleanup")
        self.seen.add(blocker.request_id)
        request = await self.next_request()
        self.assertEqual(request.content, "x" * (limit - 10))
        self.manager.fail_request(request.request_id, "test_cleanup")
        await first

    async def test_dispatched_entry_is_not_merged_and_affinity_is_read_at_dispatch(self):
        key = group_key(user_key("qq-platform", "member-a"))
        self.plugin._core._affinity_store.set_value(key, 45)
        first, _ = self.group("你好", "member-a", "小明")
        first_request = await self.next_request()
        second, _ = self.group("再来", "member-a", "小明")
        await self.settle()
        self.assertEqual(self.plugin._core._queue.waiting_count, 1)

        self.complete(first_request, bilingual("日本語", "中文", delta=3, variable="群友好感度"))
        self.assertEqual([item.text for item in await first], ["中文"])
        self.assertEqual(self.plugin._core._affinity_store.get(key, 0, -50, 100), 48)

        second_request = await self.next_request()
        self.assertEqual(first_request.content, "【群友】小明（1）[好感度:45]：你好")
        self.assertEqual(second_request.content, "【群友】小明（1）[好感度:48]：再来")
        self.assertEqual(second_request.affinity_writes, (
            {"path": [CHARACTER, "好感度"], "value": 0}, {"path": ["群友好感度"], "value": 48}, {"path": ["点歌许可"], "value": 0},
        ))
        self.complete(second_request, bilingual("日本語", "中文2"))
        self.assertEqual([item.text for item in await second], ["@小明 中文2"])

    async def test_private_replies_never_mention_even_after_waiting(self):
        first, _ = self.private("一")
        first_request = await self.next_request()
        second, _ = self.private("二")
        await self.settle()
        self.complete(first_request, bilingual("日本語", "中文1"))
        await first
        second_request = await self.next_request()
        self.assertEqual(second_request.content, "二")
        self.manager.fail_request(second_request.request_id, "generation_failed")
        self.assertEqual(
            [item.text for item in await second],
            ["ST 未能完成本次请求；请确认 ST 状态后再试。"],
        )

    async def test_waited_group_failure_notice_mentions_sender(self):
        first, _ = self.group("先", "member-a", "小明")
        first_request = await self.next_request()
        second, _ = self.group("后", "member-b", "小红")
        await self.settle()
        self.complete(first_request, bilingual("日本語", "中文"))
        await first
        self.complete(await self.next_request(), "不是双语格式")
        self.assertEqual([item.text for item in await second], [f"@小红 {self.module.INVALID_REPLY_TEXT}"])

    async def test_unconfirmed_generation_makes_head_wait_instead_of_busy(self):
        earlier = self.manager.create_request("earlier")
        self.manager.claim_request(earlier.request_id, "attempt-earlier")
        self.manager.mark_generation_started(earlier.request_id)
        self.manager.fail_request(earlier.request_id, "generation_failed")
        self.seen.add(earlier.request_id)

        waiting, _ = self.private("等一下")
        await asyncio.sleep(0.25)
        self.assertFalse(waiting.done())
        self.assertIsNone(self.manager.peek_pending_request())

        self.manager.confirm_generation_finished(earlier.request_id)
        request = await self.next_request()
        self.assertEqual(request.content, "等一下")
        self.complete(request, bilingual("日本語", "中文"))
        self.assertEqual([item.text for item in await waiting], ["中文"])
        self.assertTrue(any(line.startswith("[STBridge] Queue dispatched") for line in self.logger.infos))

    async def test_closed_gate_or_stopped_service_ends_waiters_immediately(self):
        for close in (
            lambda: self.plugin._core._recovery_gate.begin_shutdown(),
            lambda: setattr(self.plugin._core._http_service, "state", HttpServiceState.STOPPED),
        ):
            with self.subTest(close=close):
                self.plugin = _ready_plugin(self.module)
                self.manager = self.plugin._core._request_state_manager
                first, _ = self.group("先", "member-a", "小明")
                first_request = await self.next_request()
                waiting, _ = self.group("后", "member-b", "小红")
                await self.settle()
                close()
                results = await asyncio.wait_for(waiting, 1)
                self.assertEqual([item.text for item in results], [f"@小红 {self.module.UNAVAILABLE_TEXT}"])
                await first
                self.assertEqual(self.plugin._core._queue.waiting_count, 0)
                self.assertEqual(self.manager.get_request(first_request.request_id).status.value, "FAILED")
                self.assertEqual(
                    [request for request in self.manager._requests if request not in self.seen],
                    [],
                )

    async def test_terminate_releases_waiters_and_cancellation_leaves_nothing(self):
        async def stop():
            self.plugin._core._recovery_gate.begin_shutdown()
            self.plugin._core._recovery_gate.mark_stopped()
            service.state = HttpServiceState.STOPPED

        service = SimpleNamespace(state=HttpServiceState.RUNNING, stop=stop)
        self.plugin._core._http_service = service
        first, _ = self.group("先", "member-a", "小明")
        await self.next_request()
        waiting, _ = self.group("后", "member-b", "小红")
        await self.settle()
        await self.plugin.terminate()
        self.assertEqual(
            [item.text for item in await asyncio.wait_for(waiting, 1)],
            [f"@小红 {self.module.UNAVAILABLE_TEXT}"],
        )
        await first
        self.assertEqual(self.plugin._core._queue.waiting_count, 0)
        self.assertTrue(self.plugin._core._queue.closed)

        self.plugin = _ready_plugin(self.module)
        self.manager = self.plugin._core._request_state_manager
        first, _ = self.group("先", "member-a", "小明")
        first_request = await self.next_request()
        waiting, _ = self.group("后", "member-b", "小红")
        await self.settle()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(self.plugin._core._queue.waiting_count, 0)
        self.manager.fail_request(first_request.request_id, "test_cleanup")
        await first
        self.assertEqual(self.manager.get_request(first_request.request_id).generation_status,
                         GenerationStatus.NOT_STARTED)
        self.assertEqual(self.plugin._core._queue.snapshot(), {"active": False, "waiting": []})


    async def test_slot_of_never_resumed_handler_is_force_released(self):
        clock = FakeClock()
        self.plugin._core._queue = BridgeQueue(
            clock=clock,
            poll_interval=0.01,
            active_hold_limit=self.module.QUEUE_ACTIVE_HOLD_LIMIT_SECONDS,
            on_force_release=self.plugin._core._log_queue_force_release,
        )
        # Like AstrBot after a stopped event: the handler is never resumed after its yield.
        event_a = _group_bridge_event(self.module, "先", sender_id="member-a", sender_name="小明")
        stuck = self.plugin.stbridge_group_message(event_a)
        first = asyncio.ensure_future(stuck.__anext__())
        self.complete(await self.next_request(), bilingual("日本語", "中文a"))
        self.assertEqual((await first).text, "中文a")

        # B arrives later so that its own 180 s wait limit is not what ends the wait.
        clock.now += 100
        waiting, _ = self.group("后", "member-b", "小红")
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        clock.now += self.module.QUEUE_ACTIVE_HOLD_LIMIT_SECONDS + 1 - 100
        request = await self.next_request()
        self.assertEqual(request.content, "【群友】小红（1）[好感度:0]：后")
        hashed_a = user_hash(user_key("qq-platform", "member-a"))
        self.assertTrue(any(
            line.startswith(f"[STBridge] Queue slot force-released user={hashed_a} held=241")
            for line in self.logger.warnings
        ))

        await stuck.aclose()
        self.assertTrue(self.plugin._core._queue.snapshot()["active"])
        self.complete(request, bilingual("日本語", "中文b"))
        self.assertEqual([item.text for item in await waiting], ["@小红 中文b"])
        self.assertFalse(self.plugin._core._queue.snapshot()["active"])


class BridgeQueueWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_slot_is_freed_but_busy_bridge_still_blocks(self):
        clock = FakeClock()
        released = []
        queue = BridgeQueue(
            clock=clock,
            poll_interval=0.01,
            active_hold_limit=240,
            on_force_release=lambda entry, held: released.append((entry, held)),
        )
        first = queue.enqueue("a", "一", max_waiting=5)
        self.assertEqual(
            await queue.wait_turn(first, max_wait_seconds=180, dispatch=lambda _: "a", available=lambda: True),
            "a",
        )
        busy = True
        second = queue.enqueue("b", "二", max_waiting=5)
        waiting = asyncio.create_task(queue.wait_turn(
            second,
            max_wait_seconds=600,
            dispatch=lambda _: None if busy else "b",
            available=lambda: True,
        ))
        clock.now += 239
        await asyncio.sleep(0.05)
        self.assertEqual(released, [])

        clock.now += 2
        await asyncio.sleep(0.05)
        self.assertEqual(released, [(first, 241)])
        # The slot is free, but the bridge itself is still busy: keep waiting.
        self.assertFalse(waiting.done())
        self.assertFalse(queue.snapshot()["active"])

        busy = False
        self.assertEqual(await asyncio.wait_for(waiting, 1), "b")
        queue.release(first)
        self.assertTrue(queue.snapshot()["active"])
        queue.release(second)
        self.assertFalse(queue.snapshot()["active"])

    async def test_without_limit_slot_is_never_force_released(self):
        clock = FakeClock()
        queue = BridgeQueue(clock=clock, poll_interval=0.01)
        first = queue.enqueue("a", "一", max_waiting=5)
        await queue.wait_turn(first, max_wait_seconds=180, dispatch=lambda _: "a", available=lambda: True)
        second = queue.enqueue("b", "二", max_waiting=5)
        waiting = asyncio.create_task(queue.wait_turn(
            second, max_wait_seconds=10_000, dispatch=lambda _: "b", available=lambda: True,
        ))
        clock.now += 5_000
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        queue.release(first)
        self.assertEqual(await asyncio.wait_for(waiting, 1), "b")


class QueueSettingsTests(unittest.TestCase):
    def test_defaults_bounds_and_fallback(self):
        self.assertEqual(default_queue_settings(), {"max_waiting": 5, "max_wait_seconds": 180})
        self.assertEqual(validate_queue_settings({}), default_queue_settings())
        self.assertEqual(
            validate_queue_settings({"max_waiting": 20, "max_wait_seconds": 30}),
            {"max_waiting": 20, "max_wait_seconds": 30},
        )
        for bad in (
            {"max_waiting": 0}, {"max_waiting": 21}, {"max_waiting": 2.5}, {"max_waiting": True},
            {"max_wait_seconds": 29}, {"max_wait_seconds": 601}, {"max_wait_seconds": "60"}, [],
        ):
            with self.subTest(bad=bad), self.assertRaises(EmotionConfigError):
                validate_queue_settings(bad)
        self.assertEqual(queue_settings({"queue": {"max_waiting": 99}}), default_queue_settings())
        self.assertEqual(queue_settings({}), default_queue_settings())

    def test_store_adds_defaults_and_rejects_invalid_saved_section(self):
        with tempfile.TemporaryDirectory(prefix="st-bridge-queue-config-") as directory:
            root = Path(directory)
            legacy = {"version": 1, "models": {}, "characters": {}}
            (root / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(ConfigStore(root).config["queue"], default_queue_settings())
            broken = dict(legacy, queue={"max_waiting": 0})
            (root / "config.json").write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ConfigStore(root)


class QueueConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-queue-console-")
        self.store = ConfigStore(Path(self._directory.name))
        self.status = {
            "active": True,
            "waiting": [{"user": user_hash("qq:openid-1"), "waited_seconds": 3.5, "messages": 2}],
        }
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.service = ConsoleService(
            create_console_app(self.store, queue_status=lambda: self.status),
            port=self.port,
        )
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

    async def test_read_and_update_settings(self):
        status, body = await self.call("GET", "/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "settings": {"max_waiting": 5, "max_wait_seconds": 180},
            "active": True,
            "waiting_count": 1,
            "waiting": self.status["waiting"],
        })
        self.assertNotIn("openid-1", json.dumps(body))

        status, body = await self.call("POST", "/api/queue/settings", {"max_waiting": 8, "max_wait_seconds": 240})
        self.assertEqual((status, body), (200, {"settings": {"max_waiting": 8, "max_wait_seconds": 240}}))
        self.assertEqual(self.store.config["queue"], {"max_waiting": 8, "max_wait_seconds": 240})
        for payload in ({"max_waiting": 21}, {"max_wait_seconds": 10}, {"max_waiting": "8"}):
            status, _ = await self.call("POST", "/api/queue/settings", payload)
            self.assertEqual(status, 400)
        self.assertEqual(self.store.config["queue"]["max_waiting"], 8)

    async def test_writes_need_same_origin_and_host(self):
        status, body = await self.call(
            "POST", "/api/queue/settings", {"max_waiting": 2}, {"Origin": "http://evil.example"},
        )
        self.assertEqual((status, body), (403, {"error": "forbidden_origin"}))
        status, body = await self.call("GET", "/api/queue", headers={"Host": "evil.example"})
        self.assertEqual((status, body), (403, {"error": "forbidden_host"}))
        self.assertEqual(self.store.config["queue"], default_queue_settings())

    async def test_without_queue_provider_reports_empty_queue(self):
        await self.service.stop()
        self.service = ConsoleService(create_console_app(self.store), port=self.port)
        await self.service.start()
        status, body = await self.call("GET", "/api/queue")
        self.assertEqual((status, body["waiting_count"], body["active"]), (200, 0, False))


if __name__ == "__main__":
    unittest.main()
