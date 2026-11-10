"""The connector protocol: how messages are written on the wire and the symmetric RPC layer on top of a transport.

A connector (the AstrBot plugin today, other chat bots later) talks to the core over one connection. Both
sides can call the other (``call``) and the connector can open streams (``stream``) whose items the core
produces one at a time: after the connector has *sent* an item it acknowledges it, and only then does the core
go on. That keeps "the queue slot is released after the reply went out" true across processes.

Frames are JSON objects with a kind ``k``:

    call   {"k":"call","id","m","p"}           ask the peer to run method ``m``; answered by ret / err
    ret    {"k":"ret","id","v"}                the result of a call
    err    {"k":"err","id","t","n","e"}        a call or stream failed: kind ``t`` ("runtime" | "os" | "other"), the
                                               original exception's class name ``n``, text ``e``
    open   {"k":"open","id","m","p"}           open a stream (connector -> core only in practice)
    item   {"k":"item","id","v"}               the next stream item
    ack    {"k":"ack","id"}                    the last item was sent; the producer may go on
    end    {"k":"end","id"}                    the stream is over
    cancel {"k":"cancel","id"}                 the consumer stopped early

Ids are strings: each side prefixes its own counter, so the two sides never clash.
Nothing here knows about any chat platform.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from .log import logger
from .platform_api import IncomingMessage, ReplyBatch, Segment


CANCEL_WAIT_SECONDS = 2.0


class ProtocolError(ValueError):
    """A frame or payload does not have the shape the protocol asks for."""


class RpcError(Exception):
    """The peer reported a failure (kind "other")."""


class RemoteRuntimeError(RuntimeError):
    """The peer reported a RuntimeError."""


class RemoteOSError(OSError):
    """The peer reported an OSError."""


class ConnectionLost(RemoteRuntimeError):
    """The connection ended while a call or stream was waiting."""


# ---- payloads ---------------------------------------------------------------------------------
_MESSAGE_STRINGS = (
    "platform", "platform_id", "chat_type", "session_id", "conversation_id",
    "sender_id", "sender_name", "message_str", "ref", "args",
)


def message_to_dict(msg: IncomingMessage) -> dict[str, Any]:
    return {
        "platform": msg.platform,
        "platform_id": msg.platform_id,
        "chat_type": msg.chat_type,
        "session_id": msg.session_id,
        "conversation_id": msg.conversation_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "group_id": msg.group_id,
        "text": msg.text,
        "message_str": msg.message_str,
        "ref": msg.ref,
        "identity_ok": msg.identity_ok,
        "command": msg.command,
        "args": msg.args,
        "wake_prefixes": list(msg.wake_prefixes) if msg.wake_prefixes is not None else None,
    }


def _optional_string(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ProtocolError(f"message field {key} must be a string or null")
    return value


def message_from_dict(data: Any) -> IncomingMessage:
    if not isinstance(data, dict):
        raise ProtocolError("message must be an object")
    for key in _MESSAGE_STRINGS:
        if key in data and not isinstance(data[key], str):
            raise ProtocolError(f"message field {key} must be a string")
    if not isinstance(data.get("identity_ok", True), bool):
        raise ProtocolError("message field identity_ok must be a boolean")
    values = {key: data.get(key, "") for key in _MESSAGE_STRINGS}
    prefixes = data.get("wake_prefixes")
    if prefixes is not None and not (isinstance(prefixes, list) and all(isinstance(item, str) for item in prefixes)):
        raise ProtocolError("message field wake_prefixes must be a list of strings")
    return IncomingMessage(
        **values,
        wake_prefixes=list(prefixes) if prefixes is not None else None,
        group_id=_optional_string(data, "group_id"),
        text=_optional_string(data, "text"),
        identity_ok=data.get("identity_ok", True),
        command=_optional_string(data, "command"),
    )


def segment_to_dict(segment: Segment) -> dict[str, Any]:
    return {
        "kind": segment.kind,
        "text": segment.text,
        "path": str(segment.path) if segment.path is not None else None,
    }


def segment_from_dict(data: Any) -> Segment:
    if not isinstance(data, dict):
        raise ProtocolError("segment must be an object")
    kind, text, path = data.get("kind"), data.get("text", ""), data.get("path")
    if kind not in ("text", "voice", "image"):
        raise ProtocolError("unknown segment kind")
    if not isinstance(text, str) or not (path is None or isinstance(path, str)):
        raise ProtocolError("segment text and path must be strings")
    return Segment(kind, text=text, path=Path(path) if path is not None else None)


def segments_to_list(segments: list[Segment] | tuple[Segment, ...]) -> list[dict[str, Any]]:
    return [segment_to_dict(segment) for segment in segments]


def segments_from_list(data: Any) -> list[Segment]:
    if not isinstance(data, list):
        raise ProtocolError("segments must be a list")
    return [segment_from_dict(item) for item in data]


def batch_to_dict(batch: ReplyBatch) -> dict[str, Any]:
    return {"segments": segments_to_list(batch.segments)}


def batch_from_dict(data: Any) -> ReplyBatch:
    if not isinstance(data, dict):
        raise ProtocolError("batch must be an object")
    return ReplyBatch(tuple(segments_from_list(data.get("segments"))))


# ---- transports -------------------------------------------------------------------------------
class Transport:
    """A connection that carries text frames; ``receive`` gives None once it is closed."""

    async def send(self, text: str) -> None:
        raise NotImplementedError

    async def receive(self) -> str | None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class MemoryTransport(Transport):
    """One end of an in-memory connection (frames still travel as text, so they are fully serialized)."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self._peer: "MemoryTransport | None" = None
        self._closed = False

    @classmethod
    def pair(cls) -> tuple["MemoryTransport", "MemoryTransport"]:
        first, second = cls(), cls()
        first._peer, second._peer = second, first
        return first, second

    async def send(self, text: str) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise ConnectionError("memory transport closed")
        self._peer._incoming.put_nowait(text)

    async def receive(self) -> str | None:
        return await self._incoming.get()

    async def close(self) -> None:
        for end in (self, self._peer):
            if end is not None and not end._closed:
                end._closed = True
                end._incoming.put_nowait(None)


# ---- the RPC peer -----------------------------------------------------------------------------
Handler = Callable[[Any], Awaitable[Any]]
StreamHandler = Callable[[Any], AsyncIterator[Any]]

_ERROR_KINDS = {"runtime": RemoteRuntimeError, "os": RemoteOSError, "other": RpcError}
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_named_errors: dict[tuple[str, type], type] = {}


def _error_kind(error: BaseException) -> str:
    # The core's logs name only these three kinds of failure, so they have to survive the trip.
    if isinstance(error, OSError):
        return "os"
    if isinstance(error, RuntimeError):
        return "runtime"
    return "other"


class RpcPeer:
    """One end of a connection: serves ``handlers`` / ``streams`` and can call the other end."""

    def __init__(
        self,
        transport: Transport,
        side: str,
        *,
        handlers: dict[str, Handler] | None = None,
        streams: dict[str, StreamHandler] | None = None,
        call_timeout: float = 30.0,
        ack_timeout: float = 600.0,
    ) -> None:
        self._transport = transport
        self._side = side
        self._handlers = handlers or {}
        self._streams = streams or {}
        self.call_timeout = call_timeout
        self.ack_timeout = ack_timeout
        self._counter = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._acks: dict[str, asyncio.Future] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stream_ids: set[str] = set()
        self._send_lock = asyncio.Lock()
        self.closed = False

    # -- plumbing
    def _new_id(self) -> str:
        self._counter += 1
        return f"{self._side}{self._counter}"

    async def _send(self, frame: dict[str, Any]) -> None:
        text = json.dumps(frame, ensure_ascii=False)
        async with self._send_lock:
            await self._transport.send(text)

    async def run(self) -> None:
        """Read frames until the connection ends, then fail everything that was still waiting."""
        try:
            while True:
                text = await self._transport.receive()
                if text is None:
                    break
                try:
                    frame = json.loads(text)
                    kind = frame["k"]
                except (ValueError, KeyError, TypeError):
                    logger.warning("[STBridge] Connector frame ignored (Malformed)")
                    continue
                self._dispatch(kind, frame)
        finally:
            await self._shutdown()
            try:
                await self._transport.close()  # also frees the socket when the other side closed first
            except Exception:
                pass

    async def _shutdown(self) -> None:
        self.closed = True
        lost = ConnectionLost("connection closed")
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(lost)
        for queue in list(self._queues.values()):
            queue.put_nowait(("err", lost))
        for future in list(self._acks.values()):
            if not future.done():
                future.set_exception(lost)
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await self._transport.close()

    async def drain(self, timeout: float) -> bool:
        """Wait (up to ``timeout``) until the streams this peer is producing have ended; True when none is left."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._stream_ids and not self.closed:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return not self._stream_ids

    def _dispatch(self, kind: str, frame: dict[str, Any]) -> None:
        ident = frame.get("id")
        if kind in ("call", "open"):
            if not isinstance(ident, str) or not isinstance(frame.get("m"), str):
                return
            runner = self._run_call if kind == "call" else self._run_stream
            task = asyncio.create_task(runner(ident, frame["m"], frame.get("p")))
            self._tasks[ident] = task
            if kind == "open":
                self._stream_ids.add(ident)
            task.add_done_callback(lambda _t, ident=ident: (self._tasks.pop(ident, None), self._stream_ids.discard(ident)))
        elif kind == "ret":
            future = self._pending.pop(ident, None)
            if future is not None and not future.done():
                future.set_result(frame.get("v"))
        elif kind == "err":
            error = self._remote_error(frame)
            future = self._pending.pop(ident, None)
            if future is not None and not future.done():
                future.set_exception(error)
            queue = self._queues.get(ident)
            if queue is not None:
                queue.put_nowait(("err", error))
        elif kind == "item":
            queue = self._queues.get(ident)
            if queue is not None:
                queue.put_nowait(("item", frame.get("v")))
        elif kind == "end":
            queue = self._queues.get(ident)
            if queue is not None:
                queue.put_nowait(("end", None))
        elif kind == "ack":
            future = self._acks.pop(ident, None)
            if future is not None and not future.done():
                future.set_result(None)
        elif kind == "cancel":
            task = self._tasks.get(ident)
            if task is not None:
                task.cancel()

    @staticmethod
    def _remote_error(frame: dict[str, Any]) -> Exception:
        base = _ERROR_KINDS.get(frame.get("t"), RpcError)
        text = frame.get("e")
        name = frame.get("n")
        if isinstance(name, str) and _NAME_PATTERN.fullmatch(name):
            # Keeps the original class name: the core's logs print it (type(error).__name__).
            cls = _named_errors.get((name, base))
            if cls is None:
                cls = _named_errors[(name, base)] = type(name, (base,), {})
            base = cls
        return base(text if isinstance(text, str) else "remote error")

    # -- serving
    async def _run_call(self, ident: str, method: str, params: Any) -> None:
        handler = self._handlers.get(method)
        try:
            if handler is None:
                raise RpcError(f"unknown method {method}")
            value = await handler(params)
            await self._send({"k": "ret", "id": ident, "v": value})
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            pass  # the connection is gone; nobody to answer
        except Exception as error:
            await self._send_error(ident, error)

    async def _send_error(self, ident: str, error: BaseException) -> None:
        try:
            await self._send({
                "k": "err", "id": ident, "t": _error_kind(error), "n": type(error).__name__,
                "e": f"{type(error).__name__}: {error}",
            })
        except (ConnectionError, OSError):
            pass

    async def _run_stream(self, ident: str, method: str, params: Any) -> None:
        producer = self._streams.get(method)
        if producer is None:
            await self._send_error(ident, RpcError(f"unknown method {method}"))
            return
        items = None
        cancelled = False
        try:
            items = producer(params)
            async for item in items:
                waiting = asyncio.get_running_loop().create_future()
                self._acks[ident] = waiting
                await self._send({"k": "item", "id": ident, "v": item})
                await asyncio.wait_for(waiting, self.ack_timeout)
            await self._send({"k": "end", "id": ident})
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ConnectionError:
            pass
        except Exception as error:
            await self._send_error(ident, error)
        finally:
            self._acks.pop(ident, None)
            if items is not None:
                # Runs the producer's own cleanup (the core's queue slot) right away.
                try:
                    await asyncio.shield(items.aclose())
                except Exception:
                    pass
            if cancelled and not self.closed:
                # Tells a consumer that cancelled the stream that the producer's cleanup is done.
                try:
                    await self._send({"k": "end", "id": ident})
                except Exception:
                    pass

    # -- calling
    async def call(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        if self.closed:
            raise ConnectionLost("connection closed")
        ident = self._new_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._send({"k": "call", "id": ident, "m": method, "p": params})
            return await asyncio.wait_for(future, self.call_timeout if timeout is None else timeout)
        except ConnectionError as error:
            raise ConnectionLost(str(error)) from error
        finally:
            self._pending.pop(ident, None)

    async def stream(self, method: str, params: Any = None) -> AsyncIterator[Any]:
        """Items of a stream the peer produces; each is acknowledged when the consumer asks for the next."""
        if self.closed:
            raise ConnectionLost("connection closed")
        ident = self._new_id()
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[ident] = queue
        finished = False
        try:
            try:
                await self._send({"k": "open", "id": ident, "m": method, "p": params})
            except ConnectionError as error:
                finished = True
                raise ConnectionLost(str(error)) from error
            while True:
                kind, value = await queue.get()
                if kind == "item":
                    yield value
                    await self._send({"k": "ack", "id": ident})
                elif kind == "end":
                    finished = True
                    return
                else:
                    finished = True
                    raise value
        finally:
            if not finished and not self.closed:
                # Stopped early: ask the producer to stop and wait (briefly) until its cleanup is done, so
                # what the producer holds (the core's queue slot) is free once this returns.
                try:
                    await self._send({"k": "cancel", "id": ident})
                    while True:
                        kind, _value = await asyncio.wait_for(queue.get(), CANCEL_WAIT_SECONDS)
                        if kind in ("end", "err"):
                            break
                except Exception:
                    pass
            self._queues.pop(ident, None)
