import unittest
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import BridgeRecoveryGate
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from test_plugin_lifecycle import _load_plugin_module, submodule


async def _stop_like_astrbot(star_cls, star_instance) -> str:
    """Mirror star_manager._terminate_plugin: only the class's own __dict__ counts."""
    if "__del__" in star_cls.__dict__:
        star_instance.__del__()
        return "__del__"
    elif "terminate" in star_cls.__dict__:
        await star_instance.terminate()
        return "terminate"
    return "none"


class AstrBotLifecycleLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_terminate_is_defined_on_the_plugin_class_itself(self):
        module = _load_plugin_module()
        cls = module.STBridgePlugin
        self.assertIn("terminate", cls.__dict__)
        self.assertNotIn("__del__", cls.__dict__)
        # initialize is found by hasattr, so inheriting it is fine.
        self.assertTrue(hasattr(cls, "initialize"))

    async def test_astrbot_style_stop_shuts_listener_and_queue_down(self):
        module = _load_plugin_module()

        class FakeService:
            def __init__(self, app, config, gate):
                self.gate = gate
                self.state = HttpServiceState.NEW

            async def start(self):
                self.state = HttpServiceState.RUNNING

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = HttpServiceState.STOPPED

        with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FakeService):
            plugin = module.STBridgePlugin(context=None)
            await plugin.initialize()
            service = plugin._core._http_service
            gate = plugin._core._recovery_gate
            self.assertEqual(service.state, HttpServiceState.RUNNING)
            self.assertFalse(plugin._core._queue.closed)

            called = await _stop_like_astrbot(module.STBridgePlugin, plugin)

            self.assertEqual(called, "terminate")
            self.assertEqual(service.state, HttpServiceState.STOPPED)
            self.assertEqual(gate.state, BridgeRecoveryGate.STOPPED)
            self.assertIsNone(plugin._core._http_service)
            self.assertIsNone(plugin._core._console_service)
            self.assertTrue(plugin._core._queue.closed)


if __name__ == "__main__":
    unittest.main()
