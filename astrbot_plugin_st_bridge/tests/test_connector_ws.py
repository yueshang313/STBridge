"""The connector over a real local WebSocket: handshake, Origin and token checks, streaming with
acknowledgements, disconnects, replacement and reconnecting."""

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

import aiohttp

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_client import ConnectorClient, ConnectorUnavailable
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_hub import ConnectorHub, RemotePlatform
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import (
    ConnectionLost,
    RemoteOSError,
    RpcPeer,
    message_to_dict,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_ws import (
    ConnectorServer,
    connect_websocket,
    ensure_token,
    read_token,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import PlatformAPI, ReplyBatch, Segment
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION
from test_connector_protocol import sample_message


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StubCore:
    """Just enough of BridgeCore for the hub: streams two batches and records how far it got."""

    def __init__(self):
        self.log = []
        self.imported = []
        self.stop_after_first = None  # an Event: the stream waits on it after its first batch
        self.platform = "astrbot"

    def active_platform(self):
        return self.platform

    def accept_private(self, msg):
        return msg.sender_id == "owner"

    def accept_group(self, msg):
        self.log.append(("group", msg.sender_id))

    async def _stream(self, name, msg):
        self.log.append((name, "start"))
        try:
            yield ReplyBatch.of_text("one")
            self.log.append((name, "after-one"))
            if self.stop_after_first is not None:
                await self.stop_after_first.wait()
            yield ReplyBatch((Segment.of_text("two"), Segment.voice(Path("/tmp/v.wav"))))
            self.log.append((name, "done"))
        finally:
            self.log.append((name, "closed"))

    def handle_private(self, msg):
        return self._stream("private", msg)

    def handle_group(self, msg):
        return self._stream("group", msg)

    def handle_command(self, msg):
        return self._stream("command", msg)

    async def import_operator(self, platform_id, sender_id):
        self.imported.append((platform_id, sender_id))

    def connector_cache(self):
        return {"unavailable_text": "离线啦", "operator": {"platform_id": "p", "sender_id": "owner"}}


class RecordingPlatform(PlatformAPI):
    """The connector's own platform: records what the core asks it to do."""

    def __init__(self):
        self.calls = []

    async def send(self, ref, segments):
        self.calls.append(("send", ref, segments))

    async def send_to(self, conversation_id, segments):
        self.calls.append(("send_to", conversation_id, segments))
        return conversation_id != "refused"

    async def can_preupload_image(self, ref):
        return ref == "uploadable"

    async def preupload_image(self, ref, path):
        self.calls.append(("preupload", ref, path))
        return "handle-1"

    async def send_uploaded_image(self, ref, handle):
        self.calls.append(("send_uploaded", ref, handle))
        if handle == "bad":
            raise RuntimeError("expired")


class WebSocketCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        self.token = ensure_token(self.data_dir)
        self.core = StubCore()
        self.remote = RemotePlatform()
        self.hub = ConnectorHub(self.core, self.remote, token=self.token, launched_by="test-launcher")
        self.port = free_port()
        self.server = ConnectorServer(self.hub, port=self.port)
        await self.server.start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.stop()
        await self.server.stop()

    def make_client(self, *, name="astrbot", token="default", platform=None, **kwargs):
        self.platform = platform or RecordingPlatform()
        client = ConnectorClient(
            name=name,
            version=CORE_VERSION,
            platform=self.platform,
            connect=lambda: connect_websocket(self.port),
            token=lambda: self.token if token == "default" else token,
            capabilities=lambda: {"wake_prefixes": ["/", "!"], "extra_temp_dir": "C:/temp/astrbot"},
            import_owner=lambda: {"platform_id": "p", "sender_id": "owner"},
            **kwargs,
        )
        self.clients.append(client)
        return client

    async def connected_client(self, **kwargs):
        client = self.make_client(**kwargs)
        self.assertTrue(await client.connect_once())
        return client

    async def wait_until(self, condition, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("condition not reached in time")
            await asyncio.sleep(0.02)


class HandshakeTests(WebSocketCase):
    async def test_hello_answers_with_versions_cache_and_who_launched_the_core(self):
        client = await self.connected_client()
        hello = client.last_hello
        self.assertEqual((hello["core_version"], hello["launched_by"], hello["protocol"]), (CORE_VERSION, "test-launcher", 1))
        self.assertEqual(hello["cache"], self.core.connector_cache())
        self.assertEqual(self.core.imported, [("p", "owner")])
        status = self.hub.status()
        self.assertEqual((status["connected"], status["name"], status["version"]), (True, "astrbot", CORE_VERSION))
        self.assertNotIn(self.token, str(status))
        # What the connector told about its platform is kept for the core.
        self.assertEqual(self.remote.wake_prefixes(), ["/", "!"])
        self.assertEqual(self.remote.extra_temp_dir(), Path("C:/temp/astrbot"))

    async def test_a_request_with_an_origin_header_is_refused(self):
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await session.ws_connect(f"http://127.0.0.1:{self.port}/connector", headers={"Origin": "http://evil.example"})
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.hub.status()["connected"])

    async def test_a_wrong_or_missing_token_is_refused_and_never_logged(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import log as core_log

        logged = []

        class Capture:
            def info(self, message):
                logged.append(message)

            warning = error = info

        core_log.set_logger(Capture())
        self.addCleanup(core_log.set_logger, __import__("logging").getLogger("st_bridge_core"))
        for token in ("wrong-token", "", None):
            with self.subTest(token=token):
                client = self.make_client(token=token)
                self.assertFalse(await client.connect_once())
                self.assertFalse(self.hub.status()["connected"])
        self.assertTrue(any("rejected" in line for line in logged))
        self.assertFalse(any("wrong-token" in line or self.token in line for line in logged))

    async def test_nothing_but_hello_works_before_hello(self):
        transport = await connect_websocket(self.port)
        peer = RpcPeer(transport, "c")
        runner = asyncio.create_task(peer.run())
        with self.assertRaises(RemoteOSError):  # PermissionError
            await peer.call("accept_private", {"message": message_to_dict(sample_message())})
        with self.assertRaises(RemoteOSError):
            await peer.call("shutdown", {})
        await peer.close()
        await asyncio.gather(runner, return_exceptions=True)

    async def test_the_token_file_is_created_once_and_read_back(self):
        other = Path(self.directory.name) / "fresh"
        self.assertIsNone(read_token(other))
        first = ensure_token(other)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual((ensure_token(other), read_token(other)), (first, first))


class StreamingTests(WebSocketCase):
    async def test_accept_calls_and_streamed_batches(self):
        client = await self.connected_client()
        self.assertIs(await client.call("accept_private", {"message": message_to_dict(sample_message(sender_id="owner"))}), True)
        self.assertIs(await client.call("accept_private", {"message": message_to_dict(sample_message(sender_id="x"))}), False)
        await client.call("accept_group", {"message": message_to_dict(sample_message(sender_id="member"))})
        self.assertIn(("group", "member"), self.core.log)
        batches = [item async for item in client.stream("handle_private", {"message": message_to_dict(sample_message())})]
        self.assertEqual(batches[0]["segments"], [{"kind": "text", "text": "one", "path": None}])
        self.assertEqual([segment["kind"] for segment in batches[1]["segments"]], ["text", "voice"])
        self.assertEqual(self.core.log[-4:], [("private", "start"), ("private", "after-one"), ("private", "done"), ("private", "closed")])

    async def test_the_core_waits_for_the_sent_acknowledgement_before_it_goes_on(self):
        client = await self.connected_client()
        stream = client.stream("handle_group", {"message": message_to_dict(sample_message())})
        await stream.__anext__()  # the first batch is here, but not acknowledged yet
        await asyncio.sleep(0.2)
        self.assertNotIn(("group", "after-one"), self.core.log)
        second = await stream.__anext__()  # asking for the next one acknowledges the first
        self.assertEqual(second["segments"][0]["text"], "two")
        self.assertIn(("group", "after-one"), self.core.log)
        with self.assertRaises(StopAsyncIteration):
            await stream.__anext__()

    async def test_closing_the_stream_early_releases_the_core_side(self):
        client = await self.connected_client()
        stream = client.stream("handle_command", {"message": message_to_dict(sample_message(command="sing"))})
        await stream.__anext__()
        await stream.aclose()
        self.assertIn(("command", "closed"), self.core.log)
        self.assertNotIn(("command", "done"), self.core.log)

    async def test_a_dropped_connection_ends_the_stream_and_releases_the_core_side(self):
        self.core.stop_after_first = asyncio.Event()  # the core is "waiting for ST" after the first batch
        client = await self.connected_client()
        stream = client.stream("handle_private", {"message": message_to_dict(sample_message())})
        await stream.__anext__()
        pending = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.1)
        await client._peer.close()
        with self.assertRaises(ConnectionLost):
            await pending
        await self.wait_until(lambda: ("private", "closed") in self.core.log)
        await self.wait_until(lambda: not self.hub.status()["connected"])
        with self.assertRaises(ConnectorUnavailable):
            await client.call("accept_group", {"message": message_to_dict(sample_message())})


class CoreCallsConnectorTests(WebSocketCase):
    async def test_the_core_reaches_the_platform_of_the_connector(self):
        await self.connected_client()
        segments = [Segment.of_text("你好"), Segment.image(Path("/tmp/a.png"))]
        await self.remote.send("ref-1", segments)
        self.assertTrue(await self.remote.send_to("conv-1", segments))
        self.assertFalse(await self.remote.send_to("refused", segments))
        self.assertTrue(await self.remote.can_preupload_image("uploadable"))
        self.assertFalse(await self.remote.can_preupload_image("other"))
        self.assertEqual(await self.remote.preupload_image("ref-1", Path("/tmp/a.png")), "handle-1")
        await self.remote.send_uploaded_image("ref-1", "h")
        self.assertEqual(
            [call[0] for call in self.platform.calls],
            ["send", "send_to", "send_to", "preupload", "send_uploaded"],
        )
        self.assertEqual(self.platform.calls[0][2], segments)
        with self.assertRaises(RuntimeError):  # the connector's failure comes back as a RuntimeError
            await self.remote.send_uploaded_image("ref-1", "bad")

    async def test_without_a_connector_the_core_gets_a_runtime_error(self):
        with self.assertRaises(RuntimeError):
            await self.remote.send("ref", [Segment.of_text("x")])

    async def test_a_new_connector_replaces_the_old_one(self):
        first = await self.connected_client(name="first")
        second = await self.connected_client(name="second")
        await self.wait_until(lambda: not first.connected)
        self.assertEqual(self.hub.status()["name"], "second")
        await self.remote.send("ref", [Segment.of_text("x")])
        self.assertEqual([call[0] for call in self.platform.calls], ["send"])  # the second client's platform
        self.assertTrue(second.connected)

    async def test_shutdown_request_reaches_the_owner_of_the_core(self):
        stopped = []
        self.hub._on_shutdown = lambda: stopped.append(True)
        client = await self.connected_client()
        await client.call("shutdown", {})
        self.assertEqual(stopped, [True])


class ReconnectTests(WebSocketCase):
    async def test_the_client_reconnects_after_the_core_went_away_and_comes_back(self):
        hellos = []

        async def on_connected(hello):
            hellos.append(hello["core_version"])

        client = self.make_client(retry_delays=(0.05, 0.1), on_connected=on_connected)
        client.start()
        self.assertTrue(await client.wait_connected(5))
        self.assertEqual(hellos, [CORE_VERSION])
        await self.server.stop()
        await self.wait_until(lambda: not client.connected)
        with self.assertRaises(ConnectorUnavailable):
            await client.call("accept_group", {"message": message_to_dict(sample_message())})
        # While the core is away the last hello (with the cached "unavailable" line) stays available.
        self.assertEqual(client.last_hello["cache"]["unavailable_text"], "离线啦")
        self.server = ConnectorServer(self.hub, port=self.port)
        await self.server.start()
        await self.wait_until(lambda: client.connected, timeout=10)
        self.assertEqual(hellos, [CORE_VERSION, CORE_VERSION])


if __name__ == "__main__":
    unittest.main()

