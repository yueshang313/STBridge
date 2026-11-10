"""T1a: /api/schedule* routes of the console."""

import json
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_config import MAX_PLANS, default_schedule_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import ScheduleTargets

OWNER_UMO = "qq_official:FriendMessage:owner-openid-secret"
GROUP_UMO = "qq_official:GroupMessage:group-openid-secret"


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeScheduler:
    def __init__(self):
        self.running = set()
        self.runs = []
        self.error = None
        self.result = {"result": "sent", "voice": "sent", "sticker": "skip:NoTag", "st_ms": 12, "total_ms": 34}

    def next_fire(self, plan):
        return datetime(2026, 11, 3, 7, 30)

    def is_running(self, plan_id):
        return plan_id in self.running

    async def run_now(self, plan):
        self.runs.append(plan["id"])
        if self.error is not None:
            raise self.error
        return self.result


def daily(**overrides):
    plan = {
        "name": "测试计划",
        "enabled": True,
        "kind": "daily",
        "time": "08:00",
        "weekdays": [1, 3],
        "jitter_minutes": 5,
        "target": {"type": "private"},
        "prompt": "说点什么",
        "fallback_texts": ["你好"],
    }
    plan.update(overrides)
    return plan


class ScheduleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.directory.name))
        self.targets = ScheduleTargets(Path(self.directory.name))
        self.targets.record_owner(OWNER_UMO)
        self.group_key = self.targets.record_group(GROUP_UMO)
        self.scheduler = FakeScheduler()
        self.port = _unused_port()
        self.service = ConsoleService(
            create_console_app(self.store, schedule_targets=self.targets, scheduler=self.scheduler),
            port=self.port,
        )
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

    async def test_get_returns_settings_plans_targets_and_templates(self):
        status, body = await self.call("/api/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], default_schedule_settings())
        self.assertEqual(len(body["plans"]), 6)
        self.assertTrue(all(plan["enabled"] is False for plan in body["plans"]))
        self.assertEqual(len(body["templates"]), 6)
        self.assertTrue(body["targets"]["owner_known"])
        self.assertEqual(body["targets"]["groups"][0]["key"], self.group_key)
        self.assertTrue(body["scheduler_available"])
        # disabled plans have no next fire time; every plan reports whether it is running
        self.assertTrue(all(plan["next_fire"] is None and plan["running"] is False for plan in body["plans"]))

    async def test_get_never_exposes_session_ids(self):
        status, body = await self.call("/api/schedule")
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("secret", text)
        self.assertNotIn("openid", text)

    async def test_enabled_plan_reports_next_fire_and_running(self):
        _, created = await self.call("/api/schedule/plans", "POST", daily())
        plan_id = created["plan"]["id"]
        self.scheduler.running.add(plan_id)
        _, body = await self.call("/api/schedule")
        plan = next(item for item in body["plans"] if item["id"] == plan_id)
        self.assertEqual(plan["next_fire"], "2026-11-03T07:30")
        self.assertTrue(plan["running"])

    async def test_without_targets_or_scheduler_get_still_works(self):
        port = _unused_port()
        service = ConsoleService(create_console_app(self.store), port=port)
        await service.start()
        try:
            async with self.client.get(f"http://127.0.0.1:{port}/api/schedule") as response:
                body = await response.json()
                self.assertEqual(response.status, 200)
                self.assertFalse(body["targets"]["owner_known"])
                self.assertFalse(body["scheduler_available"])
        finally:
            await service.stop()

    async def test_settings_roundtrip_and_validation(self):
        status, body = await self.call("/api/schedule/settings", "POST", {"enabled": False, "quiet_minutes": 5, "max_defer_minutes": 30})
        self.assertEqual(status, 200)
        self.assertEqual(
            self.store.config["schedule"]["settings"],
            {"enabled": False, "quiet_minutes": 5, "max_defer_minutes": 30, "min_gap_minutes": 30, "dedupe_ratio": 0.8},
        )
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 6)  # plans untouched
        for bad in ({"quiet_minutes": 121}, {"max_defer_minutes": 241}, {"enabled": "x"}):
            status, _ = await self.call("/api/schedule/settings", "POST", bad)
            self.assertEqual(status, 400)
        self.assertEqual(self.store.config["schedule"]["settings"]["quiet_minutes"], 5)

    async def test_create_update_delete_plan(self):
        status, body = await self.call("/api/schedule/plans", "POST", daily())
        self.assertEqual(status, 200)
        plan_id = body["plan"]["id"]
        self.assertRegex(plan_id, r"^[0-9a-f]{8}$")
        self.assertEqual(body["plan"]["weekdays"], [1, 3])
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 7)

        status, body = await self.call("/api/schedule/plans", "POST", daily(id=plan_id, name="改名", time="09:15"))
        self.assertEqual(status, 200)
        stored = next(plan for plan in self.store.config["schedule"]["plans"] if plan["id"] == plan_id)
        self.assertEqual((stored["name"], stored["time"]), ("改名", "09:15"))
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 7)

        status, _ = await self.call(f"/api/schedule/plans/{plan_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 6)
        status, _ = await self.call(f"/api/schedule/plans/{plan_id}", "DELETE")
        self.assertEqual(status, 404)

    async def test_presets_can_be_edited_and_deleted_like_any_plan(self):
        _, body = await self.call("/api/schedule")
        preset = body["plans"][0]
        status, _ = await self.call("/api/schedule/plans", "POST", {**preset, "enabled": True, "time": "06:45"})
        self.assertEqual(status, 200)
        status, _ = await self.call(f"/api/schedule/plans/{preset['id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 5)
        reloaded = ConfigStore(Path(self.directory.name))
        self.assertEqual(len(reloaded.config["schedule"]["plans"]), 5)  # not re-seeded

    async def test_add_from_template_by_posting_it_without_an_id(self):
        _, body = await self.call("/api/schedule")
        template = body["templates"][0]
        status, created = await self.call("/api/schedule/plans", "POST", {**template, "enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(created["plan"]["name"], template["name"])
        self.assertTrue(created["plan"]["enabled"])
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 7)

    async def test_interval_plan(self):
        plan = {
            "name": "喝水", "enabled": True, "kind": "interval", "start": "09:00", "end": "21:00",
            "every_minutes": 60, "target": {"type": "private"}, "prompt": "喝水", "fallback_texts": [],
        }
        status, body = await self.call("/api/schedule/plans", "POST", plan)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"]["every_minutes"], 60)

    async def test_invalid_plans_are_rejected_and_nothing_is_stored(self):
        for bad in (
            daily(time="25:00"),
            daily(name=""),
            daily(kind="weekly"),
            daily(prompt="x" * 301),
            daily(weekdays=[9]),
            daily(jitter_minutes=99),
            {"kind": "interval", "name": "x", "start": "20:00", "end": "10:00", "every_minutes": 30, "prompt": "x"},
        ):
            status, _ = await self.call("/api/schedule/plans", "POST", bad)
            self.assertEqual(status, 400, str(bad))
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 6)

    async def test_updating_an_unknown_plan_is_404(self):
        status, _ = await self.call("/api/schedule/plans", "POST", daily(id="ffffffff"))
        self.assertEqual(status, 404)

    async def test_group_target_must_be_a_known_group(self):
        status, body = await self.call("/api/schedule/plans", "POST", daily(target={"type": "group", "key": self.group_key}))
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"]["target"], {"type": "group", "key": self.group_key})
        status, _ = await self.call("/api/schedule/plans", "POST", daily(target={"type": "group", "key": "deadbeef"}))
        self.assertEqual(status, 400)

    async def test_plan_limit(self):
        for _ in range(MAX_PLANS - 6):
            status, _ = await self.call("/api/schedule/plans", "POST", daily())
            self.assertEqual(status, 200)
        status, _ = await self.call("/api/schedule/plans", "POST", daily())
        self.assertEqual(status, 400)

    async def test_test_endpoint_runs_the_plan_and_returns_the_result(self):
        _, body = await self.call("/api/schedule")
        plan_id = body["plans"][0]["id"]  # disabled presets can be tested too
        status, result = await self.call(f"/api/schedule/plans/{plan_id}/test", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(result["result"]["result"], "sent")
        self.assertEqual(result["result"]["st_ms"], 12)
        self.assertEqual(self.scheduler.runs, [plan_id])

    async def test_test_endpoint_errors(self):
        status, _ = await self.call("/api/schedule/plans/ffffffff/test", "POST")
        self.assertEqual(status, 404)
        _, body = await self.call("/api/schedule")
        plan_id = body["plans"][0]["id"]
        self.scheduler.error = RuntimeError("plan is already running")
        status, _ = await self.call(f"/api/schedule/plans/{plan_id}/test", "POST")
        self.assertEqual(status, 409)

    async def test_test_endpoint_without_scheduler_is_503(self):
        port = _unused_port()
        service = ConsoleService(create_console_app(self.store), port=port)
        await service.start()
        try:
            plan_id = self.store.config["schedule"]["plans"][0]["id"]
            async with self.client.post(f"http://127.0.0.1:{port}/api/schedule/plans/{plan_id}/test") as response:
                self.assertEqual(response.status, 503)
        finally:
            await service.stop()

    async def test_group_alias(self):
        status, body = await self.call(f"/api/schedule/targets/{self.group_key}", "POST", {"alias": " 制作人的群 "})
        self.assertEqual(status, 200)
        self.assertEqual(body["alias"], "制作人的群")
        _, listing = await self.call("/api/schedule")
        self.assertEqual(listing["targets"]["groups"][0]["alias"], "制作人的群")
        status, _ = await self.call("/api/schedule/targets/deadbeef", "POST", {"alias": "x"})
        self.assertEqual(status, 404)
        status, _ = await self.call(f"/api/schedule/targets/{self.group_key}", "POST", {"alias": "x" * 41})
        self.assertEqual(status, 400)
        status, _ = await self.call(f"/api/schedule/targets/{self.group_key}", "POST", {"alias": 5})
        self.assertEqual(status, 400)

    async def test_host_and_origin_are_checked_on_every_write(self):
        evil = {"Origin": "http://evil.example"}
        plan_id = self.store.config["schedule"]["plans"][0]["id"]
        for method, path, payload in (
            ("POST", "/api/schedule/settings", {"quiet_minutes": 1}),
            ("POST", "/api/schedule/plans", daily()),
            ("DELETE", f"/api/schedule/plans/{plan_id}", None),
            ("POST", f"/api/schedule/plans/{plan_id}/test", None),
            ("POST", f"/api/schedule/targets/{self.group_key}", {"alias": "x"}),
        ):
            status, _ = await self.call(path, method, payload, headers=evil)
            self.assertEqual(status, 403, f"{method} {path}")
        self.assertEqual(self.scheduler.runs, [])
        self.assertEqual(len(self.store.config["schedule"]["plans"]), 6)


if __name__ == "__main__":
    unittest.main()
