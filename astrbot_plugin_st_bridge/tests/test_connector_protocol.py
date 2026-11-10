"""The connector protocol: payload serialization and the RPC peer (over the in-memory transport)."""

import asyncio
import json
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import (
    ConnectionLost,
    MemoryTransport,
    ProtocolError,
    RemoteOSError,
    RemoteRuntimeError,
    RpcError,
    RpcPeer,
    batch_from_dict,
    batch_to_dict,
    message_from_dict,
    message_to_dict,
    segment_from_dict,
    segment_to_dict,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import IncomingMessage, ReplyBatch, Segment


def sample_message(**changes):
    values = dict(
        platform="qq_official", platform_id="qq-1", chat_type="group", session_id="s1", conversation_id="c1",
        sender_id="u1", sender_name="小明", group_id="g1", text="你好", message_str="/琴音 你好", ref="r1",
        identity_ok=True, command=None, args="", wake_prefixes=["/", "!"],
    )
    values.update(changes)
    return IncomingMessage(**values)


class PayloadTests(unittest.TestCase):
    def test_a_message_survives_json(self):
        for msg in (
            sample_message(),
            sample_message(group_id=None, text=None, wake_prefixes=None, command="sing", args="晴天"),
            sample_message(identity_ok=False, sender_name="", chat_type="other"),
        ):
            wire = json.loads(json.dumps(message_to_dict(msg), ensure_ascii=False))
            self.assertEqual(message_from_dict(wire), msg)

    def test_a_message_with_the_wrong_shape_is_refused(self):
        good = message_to_dict(sample_message())
        for bad in (
            "text", None, [],
            {**good, "sender_id": 5},
            {**good, "text": 5},
            {**good, "group_id": ["g"]},
            {**good, "identity_ok": "yes"},
            {**good, "wake_prefixes": "/"},
            {**good, "wake_prefixes": ["/", 1]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ProtocolError):
                    message_from_dict(bad)

    def test_unknown_message_fields_are_ignored_and_missing_ones_default(self):
        msg = message_from_dict({"platform": "x", "future_field": 1})
        self.assertEqual((msg.platform, msg.sender_id, msg.text, msg.identity_ok), ("x", "", None, True))

    def test_segments_and_batches_survive_json(self):
        segments = [Segment.of_text("文字"), Segment.voice(Path("C:/tmp/a.wav")), Segment.image(Path("/tmp/b.png"))]
        for segment in segments:
            self.assertEqual(segment_from_dict(json.loads(json.dumps(segment_to_dict(segment)))), segment)
        batch = ReplyBatch(tuple(segments))
        self.assertEqual(batch_from_dict(json.loads(json.dumps(batch_to_dict(batch), ensure_ascii=False))), batch)

    def test_a_segment_or_batch_with_the_wrong_shape_is_refused(self):
        for bad in ({"kind": "video"}, {"kind": "text", "text": 5}, {"kind": "voice", "path": 5}, "x", None):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                segment_from_dict(bad)
        for bad in ({"segments": "x"}, {"segments": [{"kind": "nope"}]}, [], None):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                batch_from_dict(bad)


class PeerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.near, self.far = MemoryTransport.pair()
        self.runners = []

    async def asyncTearDown(self):
        await self.near.close()
        await asyncio.gather(*self.runners, return_exceptions=True)

    def start(self, side, transport, **kwargs):
        peer = RpcPeer(transport, side, **kwargs)
        self.runners.append(asyncio.create_task(peer.run()))
        return peer


class CallTests(PeerTestCase):
    async def test_calls_return_values_and_errors_keep_their_kind_and_name(self):
        async def echo(params):
            return {"got": params}

        async def runtime(_):
            raise RuntimeError("nope")

        async def disk(_):
            raise OSError("disk")

        async def other(_):
            raise ValueError("odd")

        server = self.start("s", self.far, handlers={"echo": echo, "runtime": runtime, "disk": disk, "other": other})
        client = self.start("c", self.near)
        self.assertEqual(await client.call("echo", {"a": [1, "二"]}), {"got": {"a": [1, "二"]}})
        # What the core's logs print is the class name of the failure (type(error).__name__) and its kind.
        for method, base, name in (("runtime", RuntimeError, "RuntimeError"), ("disk", OSError, "OSError"), ("other", RpcError, "ValueError")):
            with self.subTest(method=method):
                with self.assertRaises(base) as caught:
                    await client.call(method)
                self.assertEqual(type(caught.exception).__name__, name)
        with self.assertRaises(RpcError):
            await client.call("missing")
        self.assertIsInstance(RemoteRuntimeError("x"), RuntimeError)
        self.assertIsInstance(RemoteOSError("x"), OSError)
        server.closed = client.closed = True

    async def test_calls_run_in_parallel_and_time_out(self):
        gate = asyncio.Event()

        async def slow(_):
            await gate.wait()
            return "late"

        async def quick(_):
            return "now"

        self.start("s", self.far, handlers={"slow": slow, "quick": quick})
        client = self.start("c", self.near)
        waiting = asyncio.create_task(client.call("slow"))
        self.assertEqual(await client.call("quick"), "now")
        with self.assertRaises(asyncio.TimeoutError):
            await client.call("slow", timeout=0.05)
        gate.set()
        self.assertEqual(await waiting, "late")

    async def test_the_peer_can_call_back(self):
        async def ask(_):
            return await server.call("ping")

        async def ping(_):
            return "pong"

        server = self.start("s", self.far, handlers={"ask": ask})
        client = self.start("c", self.near, handlers={"ping": ping})
        self.assertEqual(await client.call("ask"), "pong")

    async def test_garbage_frames_are_ignored(self):
        async def echo(params):
            return params

        self.start("s", self.far, handlers={"echo": echo})
        client = self.start("c", self.near)
        await self.near.send("not json")
        await self.near.send(json.dumps([1, 2]))
        await self.near.send(json.dumps({"no_kind": 1}))
        self.assertEqual(await client.call("echo", 7), 7)

    async def test_a_closing_connection_fails_waiting_calls(self):
        async def hang(_):
            await asyncio.sleep(30)

        self.start("s", self.far, handlers={"hang": hang})
        client = self.start("c", self.near)
        waiting = asyncio.create_task(client.call("hang", timeout=10))
        await asyncio.sleep(0.05)
        await self.near.close()
        with self.assertRaises(ConnectionLost):
            await waiting
        with self.assertRaises(ConnectionLost):
            await client.call("hang")


class StreamTests(PeerTestCase):
    async def test_the_producer_goes_on_only_after_the_consumer_asked_for_the_next_item(self):
        order = []

        async def numbers(params):
            try:
                for index in range(1, 4):
                    order.append(f"produce {index}")
                    yield {"n": index}
                    order.append(f"resumed {index}")
            finally:
                order.append("closed")

        self.start("s", self.far, streams={"numbers": numbers})
        client = self.start("c", self.near)
        seen = []
        async for item in client.stream("numbers", {}):
            seen.append(item["n"])
            order.append(f"consumed {item['n']}")
            await asyncio.sleep(0.05)  # the producer must not run ahead meanwhile
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(order, [
            "produce 1", "consumed 1", "resumed 1",
            "produce 2", "consumed 2", "resumed 2",
            "produce 3", "consumed 3", "resumed 3", "closed",
        ])

    async def test_a_consumer_that_stops_early_waits_until_the_producer_has_cleaned_up(self):
        cleaned = []

        async def endless(params):
            try:
                while True:
                    yield {"n": 1}
            finally:
                cleaned.append(True)

        self.start("s", self.far, streams={"endless": endless})
        client = self.start("c", self.near)
        stream = client.stream("endless", {})
        self.assertEqual(await stream.__anext__(), {"n": 1})
        await stream.aclose()
        self.assertEqual(cleaned, [True])  # already done when aclose() returned

    async def test_a_stream_error_reaches_the_consumer_with_its_kind(self):
        async def broken(params):
            yield {"n": 1}
            raise RuntimeError("mid-stream")

        self.start("s", self.far, streams={"broken": broken})
        client = self.start("c", self.near)
        stream = client.stream("broken", {})
        self.assertEqual(await stream.__anext__(), {"n": 1})
        with self.assertRaises(RuntimeError):
            await stream.__anext__()

    async def test_a_lost_connection_ends_the_consumer_and_cancels_the_producer(self):
        cleaned = []

        async def waiting(params):
            try:
                yield {"n": 1}
                await asyncio.sleep(30)
            finally:
                cleaned.append(True)

        self.start("s", self.far, streams={"waiting": waiting})
        client = self.start("c", self.near)
        stream = client.stream("waiting", {})
        self.assertEqual(await stream.__anext__(), {"n": 1})
        task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.05)
        await self.far.close()
        with self.assertRaises(ConnectionLost):
            await task
        await asyncio.sleep(0.05)
        self.assertEqual(cleaned, [True])

    async def test_a_lost_connection_releases_a_producer_waiting_for_the_acknowledgement(self):
        cleaned = []

        async def one(params):
            try:
                yield {"n": 1}
            finally:
                cleaned.append(True)

        self.start("s", self.far, streams={"one": one})
        client = self.start("c", self.near)
        stream = client.stream("one", {})
        await stream.__anext__()  # the producer now waits for the acknowledgement
        await self.near.close()
        await asyncio.sleep(0.1)
        self.assertEqual(cleaned, [True])


if __name__ == "__main__":
    unittest.main()
