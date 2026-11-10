"""A core running inside the test process, wired to the plugin through an in-memory transport.

Everything the plugin and the core say to each other still goes through the connector protocol (every frame is
serialized to JSON text and back); only the socket and the core process are left out. The plugin tests use it
as the plugin's runtime (``STBridgePlugin._create_runtime``).
"""

import asyncio
import unittest

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_client import ConnectorClient
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_hub import ConnectorHub, RemotePlatform
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import MemoryTransport
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION

TOKEN = "embedded-test-token"


_LIVE = []


async def _close_live_runtimes():
    """End every connection an embedded runtime still has open (and wait until its tasks are done)."""
    runtimes, _LIVE[:] = list(_LIVE), []
    for runtime in runtimes:
        try:
            await runtime.close_connections()
        except Exception:
            pass


def _install_teardown_hook():
    """Close the open connections of embedded runtimes when a test's event loop is shut down.

    The plugin tests never stop the plugin they build, so their connection tasks were still pending when the
    loop was closed ("Task was destroyed but it is pending"). Done once, for every IsolatedAsyncioTestCase.
    """
    original = unittest.IsolatedAsyncioTestCase._tearDownAsyncioRunner
    if getattr(original, "_closes_embedded_runtimes", False):
        return

    def teardown(self):
        runner = getattr(self, "_asyncioRunner", None)
        if runner is not None and _LIVE:
            try:
                runner.run(_close_live_runtimes())
            except Exception:
                pass
        original(self)

    teardown._closes_embedded_runtimes = True
    unittest.IsolatedAsyncioTestCase._tearDownAsyncioRunner = teardown


_install_teardown_hook()


class EmbeddedRuntime:
    def __init__(self, plugin, data_dir=None):
        _LIVE.append(self)
        hint = plugin._owner_hint or {}
        self.platform = RemotePlatform()
        self.core = BridgeCore(
            self.platform,
            data_dir if data_dir is not None else plugin._data_dir_provider,
            operator_platform_id=hint.get("platform_id", ""),
            operator_sender_id=hint.get("sender_id", ""),
        )
        # Not logged: the plugin tests compare the exact log lines of what they exercise.
        self.hub = ConnectorHub(self.core, self.platform, token=TOKEN, launched_by="test", log_connections=False)
        self.core.connector_status = self.hub.status
        self._serving = []
        self.platform.ensure_connected = self._ensure
        self.client = ConnectorClient(
            name="astrbot",
            version=CORE_VERSION,
            platform=plugin._platform,
            connect=self._connect,
            token=lambda: TOKEN,
            capabilities=lambda: {"wake_prefixes": plugin._platform.wake_prefixes(), "extra_temp_dir": None, "image_preupload": True},
            # The owner is seeded into the core directly; the one-time import has its own tests.
            import_owner=lambda: None,
            on_demand=True,
        )

    async def _ensure(self):
        await self.client.connect_once()

    async def _connect(self):
        ours, theirs = MemoryTransport.pair()
        self._serving.append(asyncio.create_task(self.hub.serve(theirs)))
        return ours

    async def start(self):
        await self.core.initialize()

    async def close_connections(self):
        await self.client.stop()
        for task in self._serving:
            task.cancel()
        await asyncio.gather(*self._serving, return_exceptions=True)
        self._serving.clear()

    async def stop(self):
        # The core stops first, over the still-open connection (a waiting message gets its last reply),
        # then the connection goes: the same order as stopping a core process.
        await self.core.terminate()
        await self.hub.drain()
        await self.client.stop()
        for task in self._serving:
            task.cancel()
        await asyncio.gather(*self._serving, return_exceptions=True)
        self._serving.clear()
