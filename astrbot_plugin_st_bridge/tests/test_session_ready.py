import asyncio
import random
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, select_tts_profile
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager
from test_plugin_lifecycle import submodule
from test_plugin_lifecycle import _bridge_event, _collect, _load_plugin_module


ORIGIN = "http://localhost:11451"
IDLE = {"generating": False, "active_request": False}


class SessionReadyTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, gate=None, callback=None):
        self.gate = gate or BridgeRecoveryGate()
        self.recoveries = []
        app = create_app(
            RequestStateManager(timeout_seconds=10),
            HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452),
            recovery_gate=self.gate,
            on_client_recovery=callback or (lambda: self.recoveries.append(1)),
        )
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def ready(self, payload, headers=None):
        response = await self.client.post(
            "/v1/session/ready", json=payload, headers={"Origin": ORIGIN} if headers is None else headers,
        )
        return response.status, await response.json()

    async def pending_status(self):
        response = await self.client.get("/v1/requests/pending", headers={"Origin": ORIGIN})
        return response.status

    async def test_idle_report_opens_a_fresh_gate_once(self):
        await self.start()
        self.assertEqual(await self.pending_status(), 503)
        self.assertEqual(await self.ready(IDLE), (200, {"state": "RECOVERED", "active": True}))
        self.assertEqual(self.gate.state, BridgeRecoveryGate.RECOVERED)
        self.assertEqual(await self.pending_status(), 204)
        self.assertEqual(await self.ready(IDLE), (200, {"state": "RECOVERED", "active": True}))
        self.assertEqual(self.recoveries, [1])

    async def test_busy_client_keeps_the_gate_closed(self):
        await self.start()
        for payload in (
            {"generating": True, "active_request": False},
            {"generating": False, "active_request": True},
            {"generating": True, "active_request": True},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    await self.ready(payload),
                    (200, {"state": "RECOVERY_REQUIRED", "reason": "client_busy", "active": True}),
                )
        self.assertEqual(self.gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
        self.assertEqual(await self.pending_status(), 503)
        self.assertEqual(self.recoveries, [])

    async def test_stopping_or_stopped_gate_returns_503(self):
        for final_state in ("stopping", "stopped"):
            with self.subTest(final_state=final_state):
                gate = BridgeRecoveryGate()
                gate.begin_shutdown()
                if final_state == "stopped":
                    gate.mark_stopped()
                await self.start(gate)
                try:
                    self.assertEqual(await self.ready(IDLE), (503, {"error": "bridge_stopping"}))
                    self.assertNotEqual(gate.state, BridgeRecoveryGate.RECOVERED)
                finally:
                    await self.client.close()
        await self.start()

    async def test_invalid_bodies_are_rejected(self):
        await self.start()
        cases = (
            ({"generating": False}, 400),
            ({"generating": False, "active_request": False, "extra": 1}, 400),
            ({"generating": "false", "active_request": False}, 422),
            ({"generating": 0, "active_request": False}, 422),
            ([False, False], 400),
        )
        for payload, status in cases:
            with self.subTest(payload=payload):
                self.assertEqual((await self.ready(payload))[0], status)
        response = await self.client.post(
            "/v1/session/ready", data="generating=false", headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status, 415)
        self.assertEqual(self.gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)

    async def test_origin_and_host_are_still_required(self):
        await self.start()
        for headers in ({}, {"Origin": "http://attacker.example"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                status, body = await self.ready(IDLE, headers=headers)
                self.assertEqual((status, body), (403, {"error": "origin_denied"}))
        status, body = await self.ready(IDLE, headers={"Origin": ORIGIN, "Host": "evil.example"})
        self.assertEqual((status, body), (403, {"error": "host_denied"}))
        self.assertEqual(self.gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)

    async def test_failing_callback_does_not_break_recovery(self):
        def broken():
            raise RuntimeError("logger failure")

        await self.start(callback=broken)
        self.assertEqual(await self.ready(IDLE), (200, {"state": "RECOVERED", "active": True}))


class PluginAutoRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_idle_report_replaces_manual_recover(self):
        module = _load_plugin_module()
        module.test_logger.infos.clear()
        apps = []

        class CapturingService:
            def __init__(self, app, config, gate):
                apps.append(app)
                self.gate = gate
                self.state = module.HttpServiceState.NEW

            async def start(self):
                self.state = module.HttpServiceState.RUNNING

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = module.HttpServiceState.STOPPED

        plugin = module.STBridgePlugin(
            context=None,
            config={"operator_platform_id": "qq-platform", "operator_sender_id": "qq-user"},
        )
        with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", CapturingService), \
                patch.object(module.star, "StarTools", None, create=True):
            await plugin.initialize()
        client = TestClient(TestServer(apps[0], host="127.0.0.1"))
        await client.start_server()
        try:
            event = _bridge_event(module, "before ready")
            results = await _collect(plugin.stbridge_private_message, event)
            self.assertEqual([result.text for result in results], ["琴音现在不在线哦，晚点再来找我～"])

            response = await client.post("/v1/session/ready", json=IDLE, headers={"Origin": ORIGIN})
            self.assertEqual(await response.json(), {"state": "RECOVERED", "active": True})
            self.assertEqual(module.test_logger.infos, ["[STBridge] Recovery confirmed by ST client"])

            processing = asyncio.create_task(
                _collect(plugin.stbridge_private_message, _bridge_event(module, "after ready")),
            )
            for _ in range(100):
                request = plugin._core._request_state_manager.peek_pending_request()
                if request is not None:
                    break
                await asyncio.sleep(0)
            self.assertEqual(request.content, "after ready")
            plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
            await processing
        finally:
            await client.close()
            await plugin.terminate()


class PickNumberTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-pick-")
        self.root = Path(self._directory.name)
        self.ktn = self.root / "ktn"
        for name, seconds in (("c三", 5), ("a一", 5), ("b長すぎ", 12), ("b二", 5)):
            path = self.ktn / "开心" / f"{name}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(1000)
                wav.writeframes(b"\x00\x00" * 1000 * seconds)
        self.config = {
            "version": 1,
            "default_character": "琴音",
            "default_emotion": "开心",
            "models": {"ktn": {"name": "ktn", "reference_audio_root": str(self.ktn)}},
            "characters": {"琴音": {"name": "琴音", "model_name": "ktn", "emotions": {}}},
        }

    def tearDown(self):
        self._directory.cleanup()

    def test_pick_is_the_stable_position_among_usable_files(self):
        expected = {1: "a一", 2: "b二", 3: "c三"}
        rng = random.Random(3)
        seen = set()
        for _ in range(40):
            selection = select_tts_profile(self.config, "开心", rng=rng)
            self.assertEqual(selection.candidates, 3)
            self.assertEqual(selection.profile["prompt_text"], expected[selection.pick])
            seen.add(selection.pick)
        self.assertEqual(seen, {1, 2, 3})
        repeat = [select_tts_profile(self.config, "开心", rng=random.Random(9)).pick for _ in range(3)]
        self.assertEqual(len(set(repeat)), 1)

    def test_console_listing_uses_the_same_numbers(self):
        (self.root / "data").mkdir()
        store = ConfigStore(self.root / "data")

        async def bind():
            def update(config):
                config.update({key: self.config[key] for key in ("models", "characters", "default_character")})

            await store.update(update)

        asyncio.run(bind())
        happy, = store.discovered("ktn")
        numbered = {audio["prompt_text"]: audio["pick"] for audio in happy["audios"]}
        self.assertEqual(numbered, {"a一": 1, "b二": 2, "b長すぎ": None, "c三": 3})
        self.assertEqual(happy["available"], 3)

    def test_manual_profile_reports_pick_one(self):
        manual = self.root / "manual.wav"
        manual.write_bytes(b"RIFF")
        self.config["characters"]["琴音"]["emotions"]["开心"] = {"ref_audio_path": str(manual)}
        selection = select_tts_profile(self.config, "开心")
        self.assertEqual((selection.source, selection.candidates, selection.pick), ("manual", 1, 1))


if __name__ == "__main__":
    unittest.main()
