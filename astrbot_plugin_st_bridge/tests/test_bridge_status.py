import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    bridge_runtime_status,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from test_console_server import _unused_loopback_port
from test_plugin_lifecycle import _load_plugin_module, _ready_plugin

ORIGIN = "http://localhost:11451"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _protocol_app(clock, gate=None, manager=None):
    return create_app(
        manager or RequestStateManager(timeout_seconds=10),
        HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452),
        recovery_gate=gate or BridgeRecoveryGate(),
        clock=clock,
    )


class BridgeRuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_polled_then_polled_then_time_passes(self):
        clock = FakeClock()
        gate = BridgeRecoveryGate()
        app = _protocol_app(clock, gate)
        self.assertEqual(bridge_runtime_status(app), {"state": "RECOVERY_REQUIRED", "last_poll_age": None})

        gate.confirm_recovery()
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        try:
            response = await client.get("/v1/requests/pending", headers={"Origin": ORIGIN})
            self.assertEqual(response.status, 204)
        finally:
            await client.close()
        self.assertEqual(bridge_runtime_status(app), {"state": "RECOVERED", "last_poll_age": 0.0})

        clock.now += 12.34
        self.assertEqual(bridge_runtime_status(app), {"state": "RECOVERED", "last_poll_age": 12.3})
        gate.begin_shutdown()
        self.assertEqual(bridge_runtime_status(app)["state"], "STOPPING")

    async def test_the_reader_does_not_count_as_a_poll(self):
        clock = FakeClock()
        app = _protocol_app(clock)
        bridge_runtime_status(app)
        bridge_runtime_status(app)
        self.assertIsNone(bridge_runtime_status(app)["last_poll_age"])


class GenerationInProgressTests(unittest.TestCase):
    def test_pending_is_not_generating_claimed_is(self):
        manager = RequestStateManager(timeout_seconds=10)
        self.assertFalse(manager.generation_in_progress())
        request = manager.create_request("hello")
        self.assertFalse(manager.generation_in_progress())
        manager.claim_request(request.request_id, "attempt-1")
        self.assertTrue(manager.generation_in_progress())
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, "reply")
        self.assertFalse(manager.generation_in_progress())

    def test_an_unconfirmed_generation_after_a_timeout_still_counts(self):
        now = [0.0]
        manager = RequestStateManager(timeout_seconds=5, clock=lambda: now[0])
        request = manager.create_request("slow")
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        now[0] = 100.0
        self.assertTrue(manager.generation_in_progress())


class LifecycleProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_service_not_started_returns_the_offline_shape(self):
        module = _load_plugin_module()
        plugin = module.STBridgePlugin(context=None, config={})
        self.assertEqual(
            plugin._core._bridge_status(),
            {"gate": None, "last_poll_age": None, "active": False, "http_running": False},
        )

    def test_a_service_without_an_app_does_not_raise(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        self.assertFalse(plugin._core._bridge_status()["http_running"])

    def test_running_service_with_an_active_request(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        clock = FakeClock()
        app = _protocol_app(clock, plugin._core._recovery_gate, plugin._core._request_state_manager)
        plugin._core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING, app=app)
        status = plugin._core._bridge_status()
        self.assertEqual(status, {"gate": "RECOVERED", "last_poll_age": None, "active": False, "http_running": True})
        request = plugin._core._request_state_manager.create_request("hi")
        plugin._core._request_state_manager.claim_request(request.request_id, "attempt-1")
        self.assertTrue(plugin._core._bridge_status()["active"])

    def test_stopped_service_reports_not_running(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        plugin._core._http_service = SimpleNamespace(state=HttpServiceState.STOPPED, app=object())
        self.assertFalse(plugin._core._bridge_status()["http_running"])


class ConsoleStatusRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.directory.name))

    async def asyncTearDown(self):
        self.directory.cleanup()

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

    async def test_status_with_a_provider(self):
        payload = {"gate": "RECOVERED", "last_poll_age": 1.2, "active": False, "http_running": True}
        status, body = await self.get_status(bridge_status=lambda: payload)
        self.assertEqual((status, body), (200, {"bridge": payload, "replies": None, "today": None, "wake_prefix": None, "connector": None, "platform": "astrbot", "onebot": None}))

    async def test_status_without_a_provider(self):
        status, body = await self.get_status()
        self.assertEqual((status, body), (200, {"bridge": None, "replies": None, "today": None, "wake_prefix": None, "connector": None, "platform": "astrbot", "onebot": None}))


if __name__ == "__main__":
    unittest.main()
