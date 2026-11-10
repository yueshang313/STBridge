import asyncio
import socket
import unittest

from aiohttp import ClientConnectorError, ClientSession, TCPConnector, web

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import (
    HttpServiceState,
    LocalHttpService,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import RequestStateManager


ORIGIN = "http://localhost:11451"


def _unused_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _service(port, *, gate=None, runner_factory=None, site_factory=None):
    gate = gate or BridgeRecoveryGate()
    config = HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=port)
    app = create_app(
        RequestStateManager(),
        config,
        recovery_gate=gate,
    )
    kwargs = {}
    if runner_factory is not None:
        kwargs["runner_factory"] = runner_factory
    if site_factory is not None:
        kwargs["site_factory"] = site_factory
    return LocalHttpService(app, config, gate, **kwargs), gate


class HttpServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_start_is_idempotent_and_stop_closes_listener_and_tasks(self):
        port = _unused_loopback_port()
        service, gate = _service(port)
        client = ClientSession(connector=TCPConnector(force_close=True))
        try:
            await service.start()
            runner = service.runner
            site = service.site
            self.assertEqual(service.state, HttpServiceState.RUNNING)
            self.assertIsNotNone(site)
            sockets = site._server.sockets
            self.assertTrue(sockets)
            self.assertEqual(sockets[0].getsockname()[0], "127.0.0.1")
            self.assertEqual(sockets[0].getsockname()[1], port)

            await service.start()
            self.assertIs(service.runner, runner)
            self.assertIs(service.site, site)

            response = await client.get(
                f"http://127.0.0.1:{port}/v1/requests/pending",
                headers={
                    "Origin": ORIGIN,
                },
            )
            self.assertEqual(response.status, 503)
            self.assertEqual(await response.json(), {"error": "recovery_required"})

            started = asyncio.Event()

            async def owned_work():
                started.set()
                await asyncio.Future()

            owned_task = service.create_owned_task(owned_work())
            await started.wait()
            await service.stop()
            await service.stop()

            self.assertTrue(owned_task.cancelled())
            self.assertEqual(service.owned_task_count, 0)
            self.assertEqual(service.state, HttpServiceState.STOPPED)
            self.assertIsNone(service.runner)
            self.assertIsNone(service.site)
            self.assertEqual(gate.state, BridgeRecoveryGate.STOPPED)
            with self.assertRaises(ClientConnectorError):
                await client.get(
                    f"http://127.0.0.1:{port}/v1/requests/pending",
                    headers={
                        "Origin": ORIGIN,
                    },
                )
        finally:
            await service.stop()
            await client.close()

    async def test_occupied_port_fails_without_fallback_or_leaked_runner(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            service, gate = _service(port)

            with self.assertRaises(OSError):
                await service.start()

            self.assertEqual(service.state, HttpServiceState.NEW)
            self.assertIsNone(service.runner)
            self.assertIsNone(service.site)
            self.assertEqual(service.owned_task_count, 0)
            self.assertEqual(holder.getsockname()[1], port)
            await service.stop()
            self.assertEqual(gate.state, BridgeRecoveryGate.STOPPED)

    async def test_runner_setup_failure_rolls_back_runner(self):
        class SetupFailureRunner:
            def __init__(self):
                self.cleanup_count = 0

            async def setup(self):
                raise RuntimeError("setup failed")

            async def cleanup(self):
                self.cleanup_count += 1

        runner = SetupFailureRunner()
        service, _ = _service(
            _unused_loopback_port(),
            runner_factory=lambda *_args, **_kwargs: runner,
        )
        with self.assertRaisesRegex(RuntimeError, "setup failed"):
            await service.start()

        self.assertEqual(runner.cleanup_count, 1)
        self.assertEqual(service.state, HttpServiceState.NEW)
        self.assertIsNone(service.runner)
        self.assertIsNone(service.site)
        await service.stop()

    async def test_site_start_failure_rolls_back_runner(self):
        class FakeRunner:
            def __init__(self):
                self.cleanup_count = 0

            async def setup(self):
                return None

            async def cleanup(self):
                self.cleanup_count += 1

        class StartFailureSite:
            async def start(self):
                raise OSError("site start failed")

        runner = FakeRunner()
        service, _ = _service(
            _unused_loopback_port(),
            runner_factory=lambda *_args, **_kwargs: runner,
            site_factory=lambda *_args, **_kwargs: StartFailureSite(),
        )
        with self.assertRaisesRegex(OSError, "site start failed"):
            await service.start()

        self.assertEqual(runner.cleanup_count, 1)
        self.assertEqual(service.state, HttpServiceState.NEW)
        self.assertIsNone(service.runner)
        self.assertIsNone(service.site)
        await service.stop()

    async def test_cancelled_start_cleans_runner_and_releases_lock(self):
        class FakeRunner:
            def __init__(self):
                self.cleanup_count = 0

            async def setup(self):
                return None

            async def cleanup(self):
                self.cleanup_count += 1

        started = asyncio.Event()
        start_count = 0

        class BlockingFirstSite:
            async def start(self):
                nonlocal start_count
                start_count += 1
                if start_count == 1:
                    started.set()
                    await asyncio.Future()

            async def stop(self):
                return None

        runner = FakeRunner()
        service, _ = _service(
            _unused_loopback_port(),
            runner_factory=lambda *_args, **_kwargs: runner,
            site_factory=lambda *_args, **_kwargs: BlockingFirstSite(),
        )
        start_task = asyncio.create_task(service.start())
        await started.wait()
        start_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await start_task

        self.assertEqual(runner.cleanup_count, 1)
        self.assertFalse(service._lock.locked())
        self.assertEqual(service.state, HttpServiceState.NEW)
        self.assertIsNone(service.runner)
        self.assertIsNone(service.site)

        await service.start()
        self.assertEqual(service.state, HttpServiceState.RUNNING)
        await service.stop()

    async def test_stop_enters_fail_closed_state_before_stopping_site(self):
        gate = BridgeRecoveryGate()
        observed = []

        class FakeRunner:
            async def setup(self):
                return None

            async def cleanup(self):
                return None

        class ObservingSite:
            async def start(self):
                return None

            async def stop(self):
                observed.append(gate.state)

        service, _ = _service(
            _unused_loopback_port(),
            gate=gate,
            runner_factory=lambda *_args, **_kwargs: FakeRunner(),
            site_factory=lambda *_args, **_kwargs: ObservingSite(),
        )
        await service.start()
        await service.stop()

        self.assertEqual(observed, [BridgeRecoveryGate.STOPPING])
        self.assertEqual(gate.state, BridgeRecoveryGate.STOPPED)

    async def test_cancelled_close_still_waits_for_resource_cleanup(self):
        gate = BridgeRecoveryGate()
        stop_started = asyncio.Event()
        release_stop = asyncio.Event()
        cleanup_count = 0

        class FakeRunner:
            async def setup(self):
                return None

            async def cleanup(self):
                nonlocal cleanup_count
                cleanup_count += 1

        class BlockingStopSite:
            async def start(self):
                return None

            async def stop(self):
                stop_started.set()
                await release_stop.wait()

        service, _ = _service(
            _unused_loopback_port(),
            gate=gate,
            runner_factory=lambda *_args, **_kwargs: FakeRunner(),
            site_factory=lambda *_args, **_kwargs: BlockingStopSite(),
        )
        await service.start()
        stop_task = asyncio.create_task(service.stop())
        await stop_started.wait()
        stop_task.cancel()
        release_stop.set()

        with self.assertRaises(asyncio.CancelledError):
            await stop_task

        self.assertEqual(cleanup_count, 1)
        self.assertFalse(service._lock.locked())
        self.assertEqual(service.state, HttpServiceState.STOPPED)
        self.assertEqual(gate.state, BridgeRecoveryGate.STOPPED)


if __name__ == "__main__":
    unittest.main()
