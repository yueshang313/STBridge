"""R4a: the core as a program of its own (``python -m st_bridge_core``) and the plugin that only connects to it."""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge import core_link
from data.plugins.astrbot_plugin_st_bridge.core_link import CoreLink, CoreNotFound
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import runner
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_client import ConnectorClient
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_ws import connect_websocket, read_token
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.log import LOG_DIR, LOG_FILE
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import PlatformAPI
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import tcp_probe
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION, compute_build_id


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def prepare_data_dir(root: Path) -> dict:
    """A data directory whose config.json puts the ports on free ones."""
    root.mkdir(parents=True, exist_ok=True)
    ConfigStore(root)
    names = ("st_port", "bridge_port", "console_port", "connector_port", "onebot_port")
    ports = dict(zip(names, free_ports(len(names))))
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["network"] = ports
    config["tts"]["common"]["auto_start"] = False
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return ports


def core_command(data_dir: Path, launched_by: str = "", *extra: str):
    return [sys.executable, "-m", "st_bridge_core", "--data-dir", str(data_dir), "--launched-by", launched_by, *extra]


def core_environment():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PLUGIN_ROOT), env.get("PYTHONPATH", "")]))
    env["PYTHONUTF8"] = "1"
    return env


class FakeLogger:
    def __init__(self):
        self.infos, self.warnings, self.errors = [], [], []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)


class NullPlatform(PlatformAPI):
    async def send(self, ref, segments):
        pass

    async def send_to(self, conversation_id, segments):
        return True


async def wait_for(condition, timeout=40.0, step=0.1):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


class ProcessCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name) / "data"
        self.ports = prepare_data_dir(self.data_dir)
        self.processes = []

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def start_core(self, launched_by="test", *extra):
        process = await asyncio.create_subprocess_exec(
            *core_command(self.data_dir, launched_by, *extra),
            cwd=str(PLUGIN_ROOT), env=core_environment(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    async def port_open(self, name):
        return await tcp_probe("127.0.0.1", self.ports[name], 0.3)

    async def all_ports_closed(self):
        return not any([await self.port_open(name) for name in ("bridge_port", "console_port", "connector_port")])

    async def wait_listening(self):
        async def ready():
            return await self.port_open("connector_port")

        await wait_for(ready)

    def new_client(self, **kwargs):
        return ConnectorClient(
            name="test-connector", version=CORE_VERSION, platform=NullPlatform(),
            connect=lambda: connect_websocket(self.ports["connector_port"]),
            token=lambda: read_token(self.data_dir),
            **kwargs,
        )


class CoreProcessTests(ProcessCase):
    async def test_it_starts_serves_a_connector_and_stops_gracefully(self):
        process = await self.start_core(launched_by="unit-test")
        await self.wait_listening()
        client = self.new_client()
        self.assertTrue(await client.connect_once())
        try:
            hello = client.last_hello
            self.assertEqual((hello["core_version"], hello["launched_by"]), (CORE_VERSION, "unit-test"))
            self.assertEqual(hello["build_id"], compute_build_id(PLUGIN_ROOT / "st_bridge_core"))
            self.assertIn("unavailable_text", hello["cache"])
            # The console (another port of the same process) reports the connector.
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{self.ports['console_port']}/api/status") as response:
                    status = await response.json()
            self.assertEqual((status["connector"]["connected"], status["connector"]["name"]), (True, "test-connector"))
            self.assertTrue((self.data_dir / "connector_token").is_file())
            await client.call("shutdown", {})
        finally:
            await client.stop()
        output = await asyncio.wait_for(process.stdout.read(), 30)
        self.assertEqual(await asyncio.wait_for(process.wait(), 30), 0)
        self.assertTrue(await self.all_ports_closed())
        lines = output.decode("utf-8", errors="replace").splitlines()
        # Standard output lines are "LEVEL<TAB>message" so a starter can keep the level.
        self.assertTrue(any(line.startswith("INFO\t[STBridge] Core started version=") for line in lines), lines)
        log_file = self.data_dir / LOG_DIR / LOG_FILE
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("[STBridge] Core started", text)
        self.assertIn("[STBridge] Core stopped", text)
        self.assertIn("[STBridge] Connector connected name=test-connector", text)

    async def test_the_build_id_is_the_one_from_start_up_even_if_files_change_later(self):
        plugin = Path(self.directory.name) / "plugin"
        shutil.copytree(PLUGIN_ROOT / "st_bridge_core", plugin / "st_bridge_core", ignore=shutil.ignore_patterns("__pycache__"))
        at_start = compute_build_id(plugin / "st_bridge_core")
        env = core_environment()
        env["PYTHONPATH"] = str(plugin) + os.pathsep + env["PYTHONPATH"]
        process = await asyncio.create_subprocess_exec(
            *core_command(self.data_dir, "unit-test"), cwd=str(plugin), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self.processes.append(process)
        await self.wait_listening()
        # New files are deployed while the core runs and before any connector has said hello.
        with open(plugin / "st_bridge_core" / "core.py", "a", encoding="utf-8") as handle:
            handle.write("# deployed after the start" + chr(10))
        self.assertNotEqual(compute_build_id(plugin / "st_bridge_core"), at_start)
        client = self.new_client()
        self.assertTrue(await client.connect_once())
        try:
            self.assertEqual(client.last_hello["build_id"], at_start)
            await client.call("shutdown", {})
        finally:
            await client.stop()
        output = (await asyncio.wait_for(process.stdout.read(), 30)).decode("utf-8", errors="replace")
        await asyncio.wait_for(process.wait(), 30)
        self.assertIn(f"build_id={at_start} ", output)

    async def test_a_second_core_says_why_and_exits_without_touching_the_first(self):
        first = await self.start_core()
        await self.wait_listening()
        second = await self.start_core()
        output = (await asyncio.wait_for(second.stdout.read(), 40)).decode("utf-8", errors="replace")
        self.assertNotEqual(await asyncio.wait_for(second.wait(), 40), 0)
        failure = [line for line in output.splitlines() if "核心启动失败" in line]
        self.assertEqual(len(failure), 1, output)
        self.assertTrue(failure[0].startswith("ERROR\t"), failure)
        self.assertIn("可能已经在运行", failure[0])
        self.assertIn(str(self.ports["bridge_port"]), failure[0])
        self.assertNotIn("Core started", output)
        self.assertTrue(await self.port_open("connector_port"))  # the first one still serves
        client = self.new_client()
        self.assertTrue(await client.connect_once())
        await client.call("shutdown", {})
        await client.stop()
        self.assertEqual(await asyncio.wait_for(first.wait(), 30), 0)

    async def test_a_taken_connector_port_also_ends_the_start_clearly(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.ports["connector_port"]))
        blocker.listen()
        self.addCleanup(blocker.close)
        process = await self.start_core()
        output = (await asyncio.wait_for(process.stdout.read(), 40)).decode("utf-8", errors="replace")
        self.assertNotEqual(await asyncio.wait_for(process.wait(), 40), 0)
        self.assertIn("核心启动失败", output)
        self.assertIn(str(self.ports["connector_port"]), output)
        self.assertFalse(await self.port_open("bridge_port"))  # what had been opened was closed again


class OpenConsoleTests(ProcessCase):
    async def run_core(self, **kwargs):
        stop = asyncio.Event()
        task = asyncio.create_task(runner.run(self.data_dir, "unit-test", stop, **kwargs))
        await self.wait_listening()
        return stop, task

    async def test_open_console_opens_the_browser_on_the_console_port_once(self):
        with patch.object(runner, "open_browser") as opened:
            stop, task = await self.run_core(open_console=True)
            stop.set()
            self.assertEqual(await asyncio.wait_for(task, 60), 0)
        opened.assert_called_once_with(f"http://127.0.0.1:{self.ports['console_port']}/")

    async def test_without_the_flag_no_browser_is_opened(self):
        with patch.object(runner, "open_browser") as opened:
            stop, task = await self.run_core()
            stop.set()
            self.assertEqual(await asyncio.wait_for(task, 60), 0)
        opened.assert_not_called()

    async def test_a_browser_that_cannot_be_opened_does_not_stop_the_core(self):
        with patch.object(runner, "open_browser", side_effect=OSError("no browser")):
            stop, task = await self.run_core(open_console=True)
            self.assertFalse(task.done())
            stop.set()
            self.assertEqual(await asyncio.wait_for(task, 60), 0)

    def test_the_flag_is_parsed(self):
        self.assertTrue(runner.parse_args(["--data-dir", "x", "--open-console"]).open_console)
        self.assertFalse(runner.parse_args(["--data-dir", "x"]).open_console)
        self.assertEqual(runner.parse_args(["--data-dir", "x", "--launched-by", "start.bat"]).launched_by, "start.bat")


class LinkFileTests(unittest.TestCase):
    def make(self, content=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        link_file = Path(directory.name) / "core_link.json"
        if content is not None:
            link_file.write_text(content, encoding="utf-8")
        link = CoreLink(
            platform=NullPlatform(), plugin_dir=PLUGIN_ROOT, logger=FakeLogger(),
            capabilities=lambda: {}, import_owner=lambda: None, link_file=link_file,
        )
        return link, link_file

    def test_a_missing_file_says_so(self):
        link, _ = self.make()
        with self.assertRaises(CoreNotFound) as caught:
            link.data_dir()
        self.assertIn("没有 core_link.json", str(caught.exception))
        self.assertIsNone(link._token())

    def test_invalid_files_are_rejected(self):
        for content in ("", "{not json", "[]", "{}", '{"data_dir": 5}', '{"data_dir": "  "}', "null"):
            with self.subTest(content=content):
                link, _ = self.make(content)
                with self.assertRaises(CoreNotFound):
                    link.data_dir()

    def test_a_valid_file_gives_the_data_dir_and_is_read_again_every_time(self):
        link, link_file = self.make(json.dumps({"data_dir": "C:\\release\\data"}))
        self.assertEqual(link.data_dir(), Path("C:\\release\\data"))
        link_file.write_text(json.dumps({"data_dir": "D:\\elsewhere"}), encoding="utf-8")
        self.assertEqual(link.data_dir(), Path("D:\\elsewhere"))

    def test_the_default_file_is_next_to_the_plugin(self):
        link = CoreLink(
            platform=NullPlatform(), plugin_dir=Path("X:/plugin"), logger=FakeLogger(),
            capabilities=lambda: {}, import_owner=lambda: None,
        )
        self.assertEqual(link._link_file, Path("X:/plugin") / "core_link.json")


class PluginLinkTests(ProcessCase):
    def make_link(self, logger, *, write_link=True):
        link_file = Path(self.directory.name) / "core_link.json"
        if write_link:
            link_file.write_text(json.dumps({"data_dir": str(self.data_dir)}), encoding="utf-8")
        self.link_file = link_file
        return CoreLink(
            platform=NullPlatform(),
            plugin_dir=PLUGIN_ROOT,
            logger=logger,
            capabilities=lambda: {"wake_prefixes": ["/"], "extra_temp_dir": None},
            import_owner=lambda: {"platform_id": "qq-platform", "sender_id": "qq-user"},
            retry_delays=(0.05, 0.1),
            link_file=link_file,
        )

    async def test_without_a_link_file_it_keeps_trying_and_says_it_once(self):
        logger = FakeLogger()
        link = self.make_link(logger, write_link=False)
        await link.start()
        try:
            await asyncio.sleep(0.8)
            self.assertFalse(link.client.connected)
        finally:
            await link.stop()
        reasons = [line for line in logger.warnings if "找不到核心数据目录" in line]
        self.assertEqual(len(reasons), 1, logger.warnings)
        self.assertIn("没有 core_link.json", reasons[0])
        self.assertEqual(logger.errors, [])

    async def test_a_changed_reason_is_logged_again(self):
        logger = FakeLogger()
        link = self.make_link(logger, write_link=False)
        await link.start()
        try:
            await asyncio.sleep(0.4)
            self.link_file.write_text("{broken", encoding="utf-8")
            await asyncio.sleep(0.4)
        finally:
            await link.stop()
        reasons = [line for line in logger.warnings if "找不到核心数据目录" in line]
        self.assertEqual(len(reasons), 2, logger.warnings)
        self.assertIn("无法读取", reasons[1])

    async def test_with_no_core_running_it_retries_without_flooding_the_log(self):
        logger = FakeLogger()
        link = self.make_link(logger)
        attempts = []

        async def refused(port):
            attempts.append(port)
            raise ConnectionRefusedError("no core")

        with patch.object(core_link, "connect_websocket", refused):
            await link.start()
            try:
                async def retried():
                    return len(attempts) >= 8

                await wait_for(retried, timeout=20)
                self.assertFalse(link.client.connected)
            finally:
                await link.stop()
        self.assertEqual(set(attempts), {self.ports["connector_port"]})
        down = [line for line in logger.warnings if "核心未运行" in line]
        self.assertEqual(len(down), 1, logger.warnings)
        self.assertIn("start.bat", down[0])
        self.assertEqual(logger.errors, [])

    async def test_a_core_started_later_is_connected_to_and_the_owner_is_imported_once(self):
        logger = FakeLogger()
        link = self.make_link(logger)
        await link.start()
        try:
            await asyncio.sleep(0.3)
            self.assertFalse(link.client.connected)
            process = await self.start_core(launched_by="start.bat")
            self.assertTrue(await link.client.wait_connected(60))
            hello = link.client.last_hello
            self.assertEqual((hello["core_version"], hello["launched_by"]), (CORE_VERSION, "start.bat"))
            config = json.loads((self.data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["owner"], {"platform_id": "qq-platform", "sender_id": "qq-user", "imported": True})
            self.assertEqual([line for line in logger.warnings if "版本不一致" in line], [])  # the same code
        finally:
            await link.stop()
        process.terminate()
        await asyncio.wait_for(process.wait(), 30)

    async def test_stopping_the_plugin_leaves_the_core_running(self):
        process = await self.start_core(launched_by="start.bat")
        await self.wait_listening()
        logger = FakeLogger()
        link = self.make_link(logger)
        await link.start()
        self.assertTrue(await link.client.wait_connected(30))
        await link.stop()
        self.assertFalse(link.client.connected)
        await asyncio.sleep(0.5)
        self.assertIsNone(process.returncode)  # still running
        self.assertTrue(await self.port_open("connector_port"))
        # ... and a fresh plugin instance (a reload) connects to the same core again.
        again = self.make_link(FakeLogger())
        await again.start()
        try:
            self.assertTrue(await again.client.wait_connected(30))
        finally:
            await again.stop()
        process.terminate()
        await asyncio.wait_for(process.wait(), 30)

    async def test_a_core_that_goes_away_and_comes_back_is_reconnected_with_one_line_per_outage(self):
        process = await self.start_core()
        await self.wait_listening()
        logger = FakeLogger()
        link = self.make_link(logger)
        await link.start()
        try:
            self.assertTrue(await link.client.wait_connected(30))
            process.terminate()
            await asyncio.wait_for(process.wait(), 30)

            async def reported():
                return any("核心未运行" in line for line in logger.warnings)

            await wait_for(reported, timeout=30)
            await asyncio.sleep(0.5)
            self.assertFalse(link.client.connected)
            self.assertEqual(len([line for line in logger.warnings if "核心未运行" in line]), 1, logger.warnings)
            await self.start_core()
            await wait_for(self._connected(link))
        finally:
            await link.stop()
        self.assertEqual(len([line for line in logger.warnings if "核心未运行" in line]), 1, logger.warnings)

    @staticmethod
    def _connected(link):
        async def check():
            return link.client.connected

        return check

    async def test_version_mismatch_is_one_warning_and_nothing_else_happens(self):
        logger = FakeLogger()
        link = self.make_link(logger, write_link=False)
        calls = []

        async def call(method, params=None, timeout=None):
            calls.append(method)

        link.client.call = call
        same = {"core_version": CORE_VERSION, "build_id": compute_build_id(PLUGIN_ROOT / "st_bridge_core")}
        await link._on_connected(same)
        self.assertEqual(logger.warnings, [])
        other = {"core_version": CORE_VERSION, "build_id": "0123456789abcdef"}
        await link._on_connected(other)
        await link._on_connected(other)  # a reconnect to the same older core: no second line
        self.assertEqual(len(logger.warnings), 1)
        self.assertIn("版本不一致", logger.warnings[0])
        self.assertIn("重装插件", logger.warnings[0])
        await link._on_connected({"core_version": "0.0.1", "build_id": same["build_id"]})
        self.assertEqual(len(logger.warnings), 2)
        await link._on_connected(same)
        await link._on_connected(other)  # it was fine in between: worth saying again
        self.assertEqual(len(logger.warnings), 3)
        self.assertEqual(calls, [])  # never a shutdown or any other call to the core
        self.assertEqual(logger.errors, [])

    async def test_the_plugin_cannot_start_or_stop_a_core(self):
        # The old process-handling code is gone, not just unused.
        self.assertFalse(hasattr(core_link, "asyncio"))
        for name in ("_spawn", "_pump", "_wait_ports_closed", "_adopted", "_process"):
            self.assertFalse(hasattr(CoreLink, name), name)
        source = Path(core_link.__file__).read_text(encoding="utf-8")
        for needle in ("subprocess", "shutdown", "kill(", "terminate("):
            self.assertNotIn(needle, source)
        self.assertFalse((PLUGIN_ROOT / "core_process.py").exists())
        self.assertFalse((PLUGIN_ROOT / "start_core.bat").exists())


if __name__ == "__main__":
    unittest.main()
