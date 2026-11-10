"""R2c: the whole chain without a host, QQ, SillyTavern or a TTS engine.

    a real core process  <-  a fake chat connector (the real ConnectorClient over a real WebSocket + token)
                         <-> a fake ST extension (the bridge_client.mjs protocol, in Python)
                         ->  a fake Irodori TTS (a local HTTP server that answers with a 48 kHz WAV)

A fake NapCat (an aiohttp WebSocket client with NapCat's handshake headers and OneBot events) takes the place of a
chat platform in the last scenario.

One core process serves all scenarios (``setUpModule`` starts it on free ports in a temporary data directory,
``tearDownModule`` stops it and removes everything). The scenarios are numbered because the last two restart
the core. Run it alone with ``python tests/run_e2e.py``.
"""

import asyncio
import io
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_client import ConnectorClient
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_protocol import ConnectionLost, batch_from_dict, message_to_dict
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_ws import connect_websocket, read_token
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_library import SongLibrary
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import GROUP, PRIVATE, IncomingMessage, PlatformAPI
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.sticker_store import StickerStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION

OWNER = ("e2e-platform", "owner-1")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
STARTED = "[STBridge] Core started"
BILINGUAL = '☆‹「{jp}」›\n"{zh}"'
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wav_48k(seconds=0.25):
    """A short 48 kHz mono 16-bit sine wave, as the Irodori server would send."""
    frames = int(48000 * seconds)
    samples = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * index / 48000))) for index in range(frames))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(samples)
    return buffer.getvalue()


# ---- the fake TTS ------------------------------------------------------------------------------------
class FakeTts:
    """Irodori's OpenAI-style speech endpoint on a local port; remembers what it was asked."""

    def __init__(self, port):
        self.requests = []
        audio = wav_48k()
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                requests.append(json.loads(self.rfile.read(length) or b"{}"))
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


# ---- the core process --------------------------------------------------------------------------------
class Stack:
    """The temporary data directory, the fake TTS and the core process."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="st-bridge-e2e-"))
        self.data_dir = self.root / "data"
        names = ("st_port", "bridge_port", "console_port", "connector_port", "onebot_port")
        *ports, tts_port = free_ports(len(names) + 1)
        self.ports = dict(zip(names, ports))
        self.tts_port = tts_port
        self.process = None
        self.output = []
        self._reader = None
        self.tts = FakeTts(tts_port)

    def prepare(self):
        self.data_dir.mkdir(parents=True)
        ConfigStore(self.data_dir)
        path = self.data_dir / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["network"] = dict(self.ports)
        config["tts"]["engine"] = "irodori"
        config["tts"]["irodori"].update(host="127.0.0.1", port=self.tts_port)
        config["tts"]["common"].update(auto_start=False, prob=100.0, cooldown=0, send_text_with_audio=True)
        config["stickers"].update(enabled=True, prob=100, cooldown=0)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        StickerStore(self.data_dir).add("开心", "happy.png", PNG)
        SongLibrary(self.data_dir).add("晴天.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64, {"title": "晴天", "artist": "周杰伦"})

    def start_core(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PLUGIN_ROOT), env.get("PYTHONPATH", "")]))
        env["PYTHONUTF8"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "st_bridge_core", "--data-dir", str(self.data_dir), "--launched-by", "e2e"],
            cwd=str(PLUGIN_ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        process = self.process

        def read():
            for raw in process.stdout:
                self.output.append(raw.decode("utf-8", errors="replace").rstrip())

        self._reader = threading.Thread(target=read, daemon=True)
        self._reader.start()
        deadline = time.monotonic() + 90
        while not port_open(self.ports["connector_port"]):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the core did not start:\n" + "\n".join(self.output[-20:]))
            time.sleep(0.1)

    def wait_exit(self, timeout=40):
        self.process.wait(timeout)
        self._reader.join(5)
        self.process.stdout.close()
        return self.process.returncode

    def logged(self, text):
        return any(text in line for line in self.output)

    def log_file_text(self):
        return (self.data_dir / "logs" / "bridge.log").read_text(encoding="utf-8")

    def ports_free(self):
        return not any(port_open(port) for port in self.ports.values())

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)
        if self.process is not None and not self.process.stdout.closed:
            self._reader.join(5)
            self.process.stdout.close()
        self.tts.close()
        shutil.rmtree(self.root, ignore_errors=True)


STACK = None


def setUpModule():
    global STACK
    STACK = Stack()
    STACK.prepare()
    STACK.start_core()


def tearDownModule():
    process = STACK.process
    ports_free = None
    if process is not None and process.poll() is None:
        # A clean stop through the connector protocol, like the plugin does it.
        async def stop():
            client = make_client(PlatformAPI())
            if await client.connect_once():
                try:
                    await client.call("shutdown", {})
                finally:
                    await client.stop()

        asyncio.run(stop())
        try:
            STACK.wait_exit()
        except subprocess.TimeoutExpired:
            pass
    stopped = process is not None and process.poll() is not None
    if stopped:
        ports_free = STACK.ports_free()
    root = STACK.root
    STACK.close()
    if not stopped:
        raise AssertionError("the core did not stop")
    if not ports_free:
        raise AssertionError("a core port is still open")
    if root.exists():
        raise AssertionError("the temporary directory is still there")


# ---- the fake ST extension ---------------------------------------------------------------------------
class FakeSt:
    """The ST extension's side of the bridge protocol (st_bridge_client/bridge_client.mjs)."""

    def __init__(self):
        self.reply = lambda claim: BILINGUAL.format(jp="はい", zh="好的")
        self.hold = asyncio.Event()
        self.hold.set()
        self.seen = []
        self._base = f"http://127.0.0.1:{STACK.ports['bridge_port']}"
        self._headers = {"Origin": f"http://localhost:{STACK.ports['st_port']}"}
        self._session = None
        self._task = None
        self.online = asyncio.Event()  # set once the bridge answered a poll normally (the gate is open)

    async def start(self):
        self._session = aiohttp.ClientSession(headers=self._headers)
        self._task = asyncio.create_task(self._loop())
        await asyncio.wait_for(self.online.wait(), 30)

    async def stop(self):
        self.hold.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session is not None:
            await self._session.close()

    async def _call(self, method, path, body=None):
        async with self._session.request(method, self._base + path, json=body) as response:
            if response.status == 204:
                return 204, None
            try:
                return response.status, await response.json(content_type=None)
            except Exception:
                return response.status, None

    async def _loop(self):
        while True:
            try:
                status, data = await self._call("GET", "/v1/requests/pending")
                if status == 503:  # the core was (re)started: report that ST is idle again
                    await self._call("POST", "/v1/session/ready", {"generating": False, "active_request": False})
                    continue
                self.online.set()
                if status != 200 or not data:
                    await asyncio.sleep(0.03)
                    continue
                request_id = data["request_id"]
                status, claim = await self._call("POST", f"/v1/requests/{request_id}/claim", {"claim_attempt_id": uuid.uuid4().hex})
                if status != 200:
                    continue
                self.seen.append(claim)
                await self._call("POST", f"/v1/requests/{request_id}/generation", {"state": "RUNNING"})
                await self.hold.wait()
                reply = self.reply(claim)
                await self._call("POST", f"/v1/requests/{request_id}/generation", {"state": "FINISHED"})
                if reply is None:
                    await self._call("POST", f"/v1/requests/{request_id}/failure", {"error_code": "extension_error"})
                else:
                    await self._call("POST", f"/v1/requests/{request_id}/result", {"reply": reply})
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.05)

    async def wait_seen(self, count, timeout=30):
        await wait_until(lambda: len(self.seen) >= count, timeout)


# ---- the fake chat connector --------------------------------------------------------------------------
class ChatPlatform(PlatformAPI):
    """What the chat platform would be asked to do; everything is recorded."""

    def __init__(self):
        self.sent = []  # (ref, segments)
        self.sent_to = []  # (conversation, segments)
        self.calls = []  # ("can_preupload" | "preupload" | "send_uploaded", ...)

    async def send(self, ref, segments):
        self.sent.append((ref, list(segments)))

    async def send_to(self, conversation_id, segments):
        self.sent_to.append((conversation_id, list(segments)))
        return True

    def wake_prefixes(self):
        return ["/"]

    async def can_preupload_image(self, ref):
        self.calls.append(("can_preupload", ref))
        return True

    async def preupload_image(self, ref, path):
        self.calls.append(("preupload", ref, Path(path).name))
        return f"upload-{len(self.calls)}"

    async def send_uploaded_image(self, ref, handle):
        self.calls.append(("send_uploaded", ref, handle))


def make_client(platform, **kwargs):
    return ConnectorClient(
        name="e2e-connector",
        version=CORE_VERSION,
        platform=platform,
        connect=lambda: connect_websocket(STACK.ports["connector_port"]),
        token=lambda: read_token(STACK.data_dir),
        capabilities=lambda: {"wake_prefixes": ["/"], "extra_temp_dir": None},
        import_owner=lambda: {"platform_id": OWNER[0], "sender_id": OWNER[1]},
        retry_delays=(0.1, 0.3),
        **kwargs,
    )


async def wait_until(condition, timeout=30.0, step=0.03):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("not reached in time")
        await asyncio.sleep(step)


class Chat:
    """One chat connection: sends messages the way the AstrBot plugin would and collects the reply batches."""

    def __init__(self):
        self.platform = ChatPlatform()
        self.client = make_client(self.platform)
        self._refs = 0

    async def start(self):
        self.client.start()
        if not await self.client.wait_connected(60):
            raise AssertionError("the connector did not connect")

    async def stop(self):
        await self.client.stop()

    def message(self, kind, text, *, sender=OWNER[1], name="主人", conversation=None, command=None):
        self._refs += 1
        group = kind == GROUP
        return IncomingMessage(
            platform="e2e", platform_id=OWNER[0], chat_type=GROUP if group else PRIVATE,
            session_id=f"session-{conversation or kind}", conversation_id=conversation or ("conv-group" if group else "conv-owner"),
            sender_id=sender, sender_name=name, group_id="group-1" if group else None,
            text=None if command else text, message_str=text, ref=f"ref-{id(self)}-{self._refs}",
            command=command, wake_prefixes=["/"],
        )

    async def talk(self, msg):
        """The reply batches (each a list of segments) the core streams for one message; None when it is not accepted."""
        body = {"message": message_to_dict(msg)}
        if msg.command:
            method = "handle_command"
        elif msg.chat_type == GROUP:
            await self.client.call("accept_group", body)
            method = "handle_group"
        else:
            if not await self.client.call("accept_private", body):
                return None
            method = "handle_private"
        return [list(batch_from_dict(item).segments) async for item in self.client.stream(method, body)]


# ---- the fake NapCat ----------------------------------------------------------------------------------
class FakeNapCat:
    """NapCat's side of the reverse WebSocket: connects with its headers, sends events, answers actions."""

    SELF_ID = "10000"

    def __init__(self):
        self.url = f"http://127.0.0.1:{STACK.ports['onebot_port']}/onebot"
        self.actions = []  # the action frames, in order
        self.voice_files = []  # (path, existed when the action arrived, wav info)
        self.answer = True
        self._session = None
        self._ws = None
        self._reader = None

    @staticmethod
    def token():
        return (STACK.data_dir / "onebot_token").read_text(encoding="utf-8").strip()

    def headers(self, token=None, **extra):
        values = {
            "Authorization": f"Bearer {self.token() if token is None else token}", "X-Self-ID": self.SELF_ID,
            "x-client-role": "Universal", "User-Agent": "OneBot/11",
        }
        values.update(extra)
        return values

    async def connect(self, **kwargs):
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self.url, headers=self.headers(**kwargs))
        except BaseException:
            await self._session.close()
            raise
        self._reader = asyncio.create_task(self._read())
        await self.send({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": int(self.SELF_ID), "time": int(time.time())})
        await self.send({"post_type": "meta_event", "meta_event_type": "heartbeat", "self_id": int(self.SELF_ID), "status": {"online": True, "good": True}, "interval": 30000})

    async def close(self):
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def send(self, event):
        await self._ws.send_str(json.dumps(event))

    async def _read(self):
        async for message in self._ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if "action" not in frame:
                continue
            self.actions.append(frame)
            for item in frame["params"]["message"]:
                if item["type"] == "record":
                    file = Path(item["data"]["file"])
                    self.voice_files.append((file, file.is_file(), wav_info(file)[:3] if file.is_file() else None))
            if self.answer:
                await self._ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": len(self.actions)}, "echo": frame["echo"]}))

    @staticmethod
    def private(text, user="20001"):
        return {"post_type": "message", "message_type": "private", "sub_type": "friend", "self_id": 10000, "user_id": int(user), "message_id": 1,
                "message": [{"type": "text", "data": {"text": text}}], "raw_message": text, "sender": {"user_id": int(user), "nickname": "主人"}}

    @staticmethod
    def group(segments, group="30001", user="20002", card="小明"):
        return {"post_type": "message", "message_type": "group", "sub_type": "normal", "self_id": 10000, "group_id": int(group), "user_id": int(user),
                "message_id": 2, "message": segments, "raw_message": "", "sender": {"user_id": int(user), "nickname": "昵称", "card": card, "role": "member"}}


class Console:
    """The console's HTTP API of the core under test."""

    def __init__(self):
        self.base = f"http://127.0.0.1:{STACK.ports['console_port']}"

    async def call(self, path, method="GET", payload=None):
        async with aiohttp.ClientSession() as session:
            async with session.request(method, self.base + path, json=payload) as response:
                return response.status, await response.json()


def kinds(batch):
    return [segment.kind for segment in batch]


def wav_info(path):
    with wave.open(str(path), "rb") as reader:
        return reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chat = Chat()
        self.st = FakeSt()
        self.console = Console()
        self.addAsyncCleanup(self.chat.stop)
        await self.chat.start()
        if getattr(getattr(self, self._testMethodName), "st_online", True):  # see the end of the file
            await self.st.start()
            self.addAsyncCleanup(self.st.stop)

    async def wait_log(self, text, timeout=20):
        await wait_until(lambda: STACK.logged(text), timeout)

    # 1 ---------------------------------------------------------------------------------------------------
    async def test_01_owner_private_chat_gives_text_voice_and_a_sticker(self):
        self.st.reply = lambda claim: "[情绪:开心]" + BILINGUAL.format(jp="やったね", zh="太好了")
        batches = await self.chat.talk(self.chat.message(PRIVATE, "你好"))
        self.assertEqual(self.st.seen[-1]["content"], "你好")  # a private message goes to ST as it is
        self.assertEqual(len(batches), 1)
        text, voice = batches[0]
        self.assertEqual((text.kind, text.text, voice.kind), ("text", "太好了", "voice"))
        # The fake Irodori answered with 48 kHz; what the connector gets is 24 kHz mono 16-bit.
        self.assertEqual(wav_info(voice.path)[:3], (24000, 1, 2))
        self.assertIn("やったね", STACK.tts.requests[-1]["input"])
        # The sticker follows the text: pre-uploaded, then sent as its own reply.
        names = [call[0] for call in self.chat.platform.calls]
        self.assertEqual(names, ["can_preupload", "preupload", "send_uploaded"])
        await self.wait_log("[STBridge] TTS emotion requested=开心")
        await self.wait_log("[STBridge] Sticker emotion=开心 candidates=1 result=sent preupload=ok")

    # 2 ---------------------------------------------------------------------------------------------------
    async def test_02_an_awakened_group_message_carries_the_group_prefix(self):
        self.st.reply = lambda claim: BILINGUAL.format(jp="みなさん", zh="大家好呀")
        msg = self.chat.message(GROUP, "你好大家", sender="member-1", name="小明")
        batches = await self.chat.talk(msg)
        content = self.st.seen[-1]["content"]
        self.assertRegex(content, r"^【群友】小明（\d+）\[好感度:-?\d+\]：你好大家$")
        text, voice = batches[0]
        self.assertEqual((text.text, voice.kind), ("大家好呀", "voice"))
        self.assertFalse(text.text.startswith("@"))  # nobody waited: no mention

    # 3 ---------------------------------------------------------------------------------------------------
    async def test_03_two_people_at_once_are_answered_in_turn_and_the_second_is_mentioned(self):
        self.st.reply = lambda claim: BILINGUAL.format(jp="はい", zh="回复:" + claim["content"].rsplit("：", 1)[-1])
        self.st.hold.clear()
        done = []

        async def talk(msg, label):
            result = await self.chat.talk(msg)
            done.append(label)
            return result

        first = asyncio.create_task(talk(self.chat.message(PRIVATE, "第一条"), "first"))
        await self.st.wait_seen(len(self.st.seen) + 1)  # ST holds the first request
        second = asyncio.create_task(talk(self.chat.message(GROUP, "第二条", sender="member-2", name="小红"), "second"))
        await wait_until(lambda: STACK.logged("Queue enqueued"))
        status, queue = await self.console.call("/api/queue")
        self.assertEqual((status, queue["active"], queue["waiting_count"]), (200, True, 1))
        self.st.hold.set()
        r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), 40)
        self.assertEqual(done, ["first", "second"])
        self.assertEqual(r1[0][0].text, "回复:第一条")
        self.assertEqual(r2[0][0].text, "@小红 回复:第二条")  # it had to wait: the reply names the sender
        await self.wait_log("[STBridge] Queue dispatched")

    # 4 ---------------------------------------------------------------------------------------------------
    async def test_04_an_affinity_update_block_changes_the_stored_value(self):
        async def owner_value():
            status, body = await self.console.call("/api/affinity")
            self.assertEqual(status, 200)
            return next(user["value"] for user in body["users"] if user["track"] == "owner")

        self.st.reply = lambda claim: "旁白<UpdateVariable>_.add('藤田琴音.好感度', 1);</UpdateVariable>" + BILINGUAL.format(jp="ありがとう", zh="谢谢")
        await self.chat.talk(self.chat.message(PRIVATE, "先说一句"))
        before = await owner_value()
        self.st.reply = lambda claim: BILINGUAL.format(jp="嬉しい", zh="好开心") + "<UpdateVariable><Analysis>夸奖</Analysis>_.add('藤田琴音.好感度', 3);//开心\n</UpdateVariable>"
        batches = await self.chat.talk(self.chat.message(PRIVATE, "你真可爱"))
        self.assertEqual(batches[0][0].text, "好开心")  # the block never reaches the chat
        self.assertEqual(await owner_value(), before + 3)
        await self.wait_log("[STBridge] Affinity user=")
        # ST was told the owner's value in the request.
        self.assertTrue(any(write["value"] == before for write in (self.st.seen[-1].get("affinity_writes") or [])))

    # 5 ---------------------------------------------------------------------------------------------------
    async def test_05_a_song_request_lists_the_versions_and_goes_no_further(self):
        # /唱 歌名 from the owner: the numbered list (nothing is downloaded, sung or sent yet).
        batches = await self.chat.talk(self.chat.message(PRIVATE, "唱 晴天", command="sing"))
        listing = batches[0][0].text
        self.assertIn("晴天", listing)
        self.assertIn("[歌库]", listing)
        self.assertEqual(self.st.seen, [])  # the command never goes to ST
        # A [唱歌:歌名] tag in ST's reply: the reply first, then the list as its own message.
        self.st.reply = lambda claim: "[唱歌:晴天]" + BILINGUAL.format(jp="いいよ", zh="好呀")
        before = len(self.chat.platform.sent)
        batches = await self.chat.talk(self.chat.message(PRIVATE, "唱首歌吧"))
        self.assertEqual(batches[0][0].text, "好呀")
        (ref, segments), = self.chat.platform.sent[before:]
        self.assertEqual(segments[0].kind, "text")
        self.assertIn("找到这些版本", segments[0].text)
        self.assertIn("[歌库]", segments[0].text)
        self.assertNotIn("cover", [name for name, *_ in self.chat.platform.calls])

    # 6 ---------------------------------------------------------------------------------------------------
    async def test_06_a_scheduled_message_is_sent_to_the_remembered_conversation(self):
        self.assertTrue(await self.chat.client.call("accept_private", {"message": message_to_dict(self.chat.message(PRIVATE, "在吗", conversation="conv-owner-schedule"))}))
        self.st.reply = lambda claim: "[情绪:开心]" + BILINGUAL.format(jp="おはよう", zh="早安")
        status, body = await self.console.call("/api/schedule")
        self.assertEqual(status, 200)
        plan = next(item for item in body["plans"] if item["target"]["type"] == "private")
        status, body = await asyncio.wait_for(self.console.call(f"/api/schedule/plans/{plan['id']}/test", "POST", {}), 60)
        self.assertEqual(status, 200)
        self.assertEqual((body["result"]["result"], body["result"]["voice"]), ("sent", "sent"))
        self.assertEqual(self.st.seen[-1]["kind"], "proactive")
        self.assertIn("系统提示", self.st.seen[-1]["content"])
        sent = [(conversation, kinds(segments)) for conversation, segments in self.chat.platform.sent_to]
        self.assertEqual(sent, [("conv-owner-schedule", ["voice"]), ("conv-owner-schedule", ["text"]), ("conv-owner-schedule", ["image"])])
        self.assertEqual(self.chat.platform.sent_to[1][1][0].text, "早安")
        await self.wait_log("[STBridge] Schedule plan=")

    # 7 ---------------------------------------------------------------------------------------------------
    async def test_07_a_dropped_connector_does_not_block_the_queue_and_comes_back(self):
        self.st.hold.clear()
        turn = asyncio.create_task(self.chat.talk(self.chat.message(PRIVATE, "会被打断")))
        await self.st.wait_seen(len(self.st.seen) + 1)
        status, queue = await self.console.call("/api/queue")
        self.assertTrue(queue["active"])
        await self.chat.client._peer.close()  # the connector's connection drops mid-turn
        with self.assertRaises(ConnectionLost):
            await asyncio.wait_for(turn, 30)
        # The turn that was being handled no longer holds the queue.
        async def idle():
            return not (await self.console.call("/api/queue"))[1]["active"]

        for _ in range(100):
            if await idle():
                break
            await asyncio.sleep(0.1)
        self.assertTrue(await idle())
        # ST finishes the abandoned request; the connector reconnects by itself; the chat works again.
        self.st.hold.set()
        await wait_until(lambda: self.chat.client.connected, 30)
        self.st.reply = lambda claim: BILINGUAL.format(jp="おかえり", zh="欢迎回来")
        batches = await asyncio.wait_for(self.chat.talk(self.chat.message(PRIVATE, "我回来了")), 40)
        self.assertEqual(batches[0][0].text, "欢迎回来")

    # 8 ---------------------------------------------------------------------------------------------------
    async def test_08_a_restarted_core_is_found_again_and_keeps_the_owner(self):
        self.assertEqual(STACK.log_file_text().count(STARTED), 1)
        await self.chat.client.call("shutdown", {})
        await asyncio.get_running_loop().run_in_executor(None, STACK.wait_exit)
        self.assertEqual(STACK.process.returncode, 0)
        self.assertTrue(STACK.ports_free())
        await wait_until(lambda: not self.chat.client.connected, 30)
        await asyncio.get_running_loop().run_in_executor(None, STACK.start_core)
        await wait_until(lambda: self.chat.client.connected, 60)
        # The owner is the same: it lives in the config, so the owner's private message is accepted again,
        # and a command that needs no ST answers.
        config = json.loads((STACK.data_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["owner"], {"platform_id": OWNER[0], "sender_id": OWNER[1], "imported": True})
        batches = await self.chat.talk(self.chat.message(PRIVATE, "/好感度", command="affinity"))
        self.assertIn("好感度", batches[0][0].text)
        self.assertEqual(STACK.log_file_text().count(STARTED), 2)

    # 9 ---------------------------------------------------------------------------------------------------
    async def test_09_without_st_the_chat_is_told_the_bridge_is_unavailable_and_nothing_hangs(self):
        started = time.monotonic()
        batches = await asyncio.wait_for(self.chat.talk(self.chat.message(PRIVATE, "有人吗")), 30)
        self.assertEqual(len(batches), 1)
        self.assertIn("不在线", batches[0][0].text)
        self.assertLess(time.monotonic() - started, 10)
        status, queue = await self.console.call("/api/queue")
        self.assertEqual((queue["active"], queue["waiting_count"]), (False, 0))
        # As soon as the extension is there, the chat works again.
        st = FakeSt()
        await st.start()
        self.addAsyncCleanup(st.stop)
        st.reply = lambda claim: BILINGUAL.format(jp="こんにちは", zh="你好呀")
        batches = await asyncio.wait_for(self.chat.talk(self.chat.message(PRIVATE, "现在呢")), 40)
        self.assertEqual(batches[0][0].text, "你好呀")

    # 10 --------------------------------------------------------------------------------------------------
    async def test_10_napcat_is_a_platform_of_its_own_and_the_switch_is_immediate(self):
        napcat = FakeNapCat()
        self.addAsyncCleanup(napcat.close)

        async def platform(**settings):
            status, body = await self.console.call("/api/chat-platform", "POST", settings)
            self.assertEqual(status, 200)
            return body

        async def restore():
            await platform(active="astrbot")

        self.addAsyncCleanup(restore)

        # The handshake: no token, a wrong token and a browser's Origin are all refused.
        for headers in ({"token": "wrong"}, {"Origin": "http://evil.example"}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await FakeNapCat().connect(**headers)
            self.assertIn(caught.exception.status, (401, 403))
        await napcat.connect()
        await wait_until(lambda: STACK.logged("NapCat connected"))
        status, info = await self.console.call("/api/chat-platform")
        self.assertEqual(info["onebot"]["status"]["connected"], True)
        self.assertEqual(info["onebot"]["status"]["self_id"], "10000")
        self.assertEqual(info["onebot"]["token"], napcat.token())
        self.assertEqual(info["settings"]["active"], "astrbot")  # the default: nothing changes until the switch

        # AstrBot is active: NapCat's messages get no answer.
        seen = len(self.st.seen)
        await napcat.send(napcat.private("你好"))
        await asyncio.sleep(0.5)
        self.assertEqual((len(self.st.seen), napcat.actions), (seen, []))

        # Switch to NapCat: one group on the whitelist, the owner's QQ number.
        await platform(active="napcat", group_whitelist=["30001"], napcat_owner="20001")
        self.assertIsNone(await self.chat.talk(self.chat.message(PRIVATE, "AstrBot 来的")))  # the connector is silent now
        self.assertEqual(len(self.st.seen), seen)

        # The owner's private message: text, then the voice as its own message (a local wav file).
        self.st.reply = lambda claim: "[情绪:开心]" + BILINGUAL.format(jp="やったね", zh="太好了")
        await napcat.send(napcat.private("/琴音 你好"))
        await wait_until(lambda: len(napcat.actions) >= 3)
        self.assertEqual(self.st.seen[-1]["content"], "你好")  # the nickname prefix is removed
        shapes = [(action["action"], [item["type"] for item in action["params"]["message"]]) for action in napcat.actions[:3]]
        self.assertEqual(shapes, [("send_private_msg", ["text"]), ("send_private_msg", ["record"]), ("send_private_msg", ["image"])])
        self.assertEqual(napcat.actions[0]["params"], {"user_id": 20001, "message": [{"type": "text", "data": {"text": "太好了"}}]})
        (voice, existed, wav), = napcat.voice_files
        self.assertTrue(existed)
        self.assertEqual(wav, (24000, 1, 2))
        self.assertNotIn("\\", napcat.actions[1]["params"]["message"][0]["data"]["file"])
        self.assertEqual(self.chat.platform.calls, [])  # nothing went through the AstrBot connector

        # A whitelisted group answers to an @; another group is not even looked at.
        before = len(napcat.actions)
        self.st.reply = lambda claim: BILINGUAL.format(jp="みなさん", zh="大家好")
        await napcat.send(napcat.group([{"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": " 大家好"}}]))
        await wait_until(lambda: any(a["action"] == "send_group_msg" for a in napcat.actions[before:]))
        self.assertRegex(self.st.seen[-1]["content"], r"^【群友】小明（\d+）\[好感度:-?\d+\]：大家好$")
        seen = len(self.st.seen)
        await napcat.send(napcat.group([{"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": "你好"}}], group="30009"))
        await napcat.send(napcat.group([{"type": "text", "data": {"text": "路过"}}]))
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.st.seen), seen)

        # A scheduled message goes to the remembered NapCat conversation: voice, text, sticker.
        before = len(napcat.actions)
        self.st.reply = lambda claim: "[情绪:开心]" + BILINGUAL.format(jp="おはよう", zh="早安")
        status, body = await self.console.call("/api/schedule")
        plan = next(item for item in body["plans"] if item["target"]["type"] == "private")
        status, body = await asyncio.wait_for(self.console.call(f"/api/schedule/plans/{plan['id']}/test", "POST", {}), 60)
        self.assertEqual((status, body["result"]["result"]), (200, "sent"))
        shapes = [(a["action"], [item["type"] for item in a["params"]["message"]], a["params"].get("user_id")) for a in napcat.actions[before:]]
        self.assertEqual(shapes, [("send_private_msg", ["record"], 20001), ("send_private_msg", ["text"], 20001), ("send_private_msg", ["image"], 20001)])
        self.assertTrue(napcat.voice_files[-1][1])

        # The command works without ST, and the connector's owner is not the NapCat owner.
        before = len(napcat.actions)
        await napcat.send(napcat.private("stbridge_ping"))
        await wait_until(lambda: len(napcat.actions) > before)
        self.assertIn("NapCat", napcat.actions[before]["params"]["message"][0]["data"]["text"])

        # Back to AstrBot: NapCat is ignored again, the connector answers again.
        await platform(active="astrbot")
        seen, before = len(self.st.seen), len(napcat.actions)
        await napcat.send(napcat.private("还在吗"))
        await asyncio.sleep(0.5)
        self.assertEqual((len(self.st.seen), len(napcat.actions)), (seen, before))
        self.st.reply = lambda claim: BILINGUAL.format(jp="いるよ", zh="在的")
        batches = await self.chat.talk(self.chat.message(PRIVATE, "回来了"))
        self.assertEqual(batches[0][0].text, "在的")

        # The token never reaches the log.
        self.assertFalse(STACK.logged(napcat.token()))
        self.assertNotIn(napcat.token(), STACK.log_file_text())


# The scenarios that restart the core and leave ST out set this before asyncSetUp runs.
EndToEnd.test_08_a_restarted_core_is_found_again_and_keeps_the_owner.st_online = False
EndToEnd.test_09_without_st_the_chat_is_told_the_bridge_is_unavailable_and_nothing_hangs.st_online = False

if __name__ == "__main__":
    unittest.main()
