"""R3 review: a NapCat port that cannot be opened must not take the core down."""

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

import aiohttp

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import runner
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import RpcPeer
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_ws import connect_websocket, read_token
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.onebot_link import OneBotLink


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class TakenOneBotPortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        names = ("st_port", "bridge_port", "console_port", "connector_port", "onebot_port")
        self.ports = dict(zip(names, free_ports(len(names))))
        ConfigStore(self.data_dir)
        path = self.data_dir / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["network"] = dict(self.ports)
        config["tts"]["common"]["auto_start"] = False
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    async def test_the_core_runs_without_napcat_when_its_port_is_taken(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.ports["onebot_port"]))
        blocker.listen()
        self.addCleanup(blocker.close)
        stop = asyncio.Event()
        core_task = asyncio.create_task(runner.run(self.data_dir, "unit-test", stop))
        try:
            # The connector port opens although the NapCat port is taken.
            peer = None
            for _ in range(300):
                try:
                    transport = await connect_websocket(self.ports["connector_port"])
                except OSError:
                    await asyncio.sleep(0.1)
                    continue
                peer = RpcPeer(transport, "c", handlers={}, streams={})
                break
            self.assertIsNotNone(peer, "the core did not start")
            running = asyncio.create_task(peer.run())
            hello = await peer.call("hello", {"token": read_token(self.data_dir), "name": "unit", "version": "1", "capabilities": {}})
            self.assertEqual(hello["launched_by"], "unit-test")
            # The console says so, and the token is still shown for later.
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{self.ports['console_port']}/api/chat-platform") as response:
                    body = await response.json()
            status = body["onebot"]["status"]
            self.assertEqual((status["connected"], status["port_unavailable"], status["port"]), (False, True, self.ports["onebot_port"]))
            self.assertEqual(body["settings"]["active"], "astrbot")
            await peer.close()
            await asyncio.gather(running, return_exceptions=True)
        finally:
            stop.set()
        self.assertEqual(await asyncio.wait_for(core_task, 60), 0)  # a clean stop, nothing to close for NapCat

    async def test_the_link_reports_a_free_port_as_available(self):
        link = OneBotLink()
        self.assertEqual((link.status()["port_unavailable"], link.status()["port"]), (False, None))
        link.unavailable_port = 11455
        self.assertEqual((link.status()["port_unavailable"], link.status()["port"]), (True, 11455))

    async def test_start_onebot_returns_none_and_marks_the_link_on_failure(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.ports["onebot_port"]))
        blocker.listen()
        self.addCleanup(blocker.close)
        link = OneBotLink()
        self.assertIsNone(await runner._start_onebot(link, "tok", self.ports["onebot_port"]))
        self.assertEqual(link.unavailable_port, self.ports["onebot_port"])
        free = free_ports(1)[0]
        server = await runner._start_onebot(link, "tok", free)
        self.assertIsNotNone(server)
        await server.stop()
        # Stopping the same server again (or one that never started) is safe.
        await server.stop()


if __name__ == "__main__":
    unittest.main()
