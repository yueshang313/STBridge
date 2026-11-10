import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import tts_service as service_manager
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import (
    EXTERNAL,
    EXTERNAL_MESSAGE,
    FAILED,
    RUNNING,
    STOPPED,
    ServiceManager,
    ServiceStartError,
)


class RecordingLogger:
    def __init__(self):
        self.entries = []

    def info(self, message):
        self.entries.append(("info", message))

    def warning(self, message):
        self.entries.append(("warning", message))

    def error(self, message):
        self.entries.append(("error", message))

    def debug(self, message):
        self.entries.append(("debug", message))

    def messages(self, level):
        return [text for kind, text in self.entries if kind == level]


class FakeProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class FakeHost:
    """Fake clock, port and process table shared by the injected dependencies."""

    def __init__(self, ready_after=3.0, exit_code=None):
        self.now = 0.0
        self.port_open = False
        self.ready_after = ready_after
        self.exit_code = exit_code
        self.launches = []
        self.kills = []
        self.process = None
        self.started_at = None
        self.log_text = ""

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)
        process = self.process
        if process is None or process.returncode is not None:
            return
        if self.exit_code is not None:
            process.returncode = self.exit_code
        elif (
            self.ready_after is not None
            and self.started_at is not None
            and self.now - self.started_at >= self.ready_after
        ):
            self.port_open = True

    async def probe(self, host, port, timeout):
        await asyncio.sleep(0)
        return self.port_open

    def launcher(self, args, cwd, env, log_path):
        self.launches.append({"args": args, "cwd": cwd, "env": env, "log_path": log_path})
        self.process = FakeProcess()
        self.started_at = self.now
        if self.log_text:
            Path(log_path).write_text(self.log_text, encoding="utf-8")
        return self.process

    async def killer(self, pid):
        self.kills.append(pid)
        if self.process is not None:
            self.process.returncode = 1
        self.port_open = False


class FakeJob:
    instances = []

    def __init__(self):
        self.assigned = []
        FakeJob.instances.append(self)

    def assign(self, pid):
        self.assigned.append(pid)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.launch_dir = self.root / "GPT-SoVITS"
        (self.launch_dir / "runtime").mkdir(parents=True)
        (self.launch_dir / "runtime" / "python.exe").write_bytes(b"")
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.host = FakeHost()
        self.logger = RecordingLogger()
        self.restarts = 0
        FakeJob.instances = []

    def tearDown(self):
        self.tmp.cleanup()

    def _on_restart(self):
        self.restarts += 1

    def make(self, manager_class=ServiceManager, **overrides):
        kwargs = dict(
            host="127.0.0.1",
            port=9880,
            launch_dir=str(self.launch_dir),
            start_timeout=180,
            stop_on_exit=True,
            data_dir=self.data_dir,
            logger=self.logger,
            on_restart=self._on_restart,
            launcher=self.host.launcher,
            probe=self.host.probe,
            killer=self.host.killer,
            job_factory=FakeJob,
            clock=self.host.clock,
            sleep=self.host.sleep,
        )
        kwargs.update(overrides)
        return manager_class(**kwargs)


class ServiceManagerTests(ServiceTestCase):
    def test_concurrent_starts_share_one_process(self):
        async def scenario():
            manager = self.make()
            return manager, await asyncio.gather(
                manager.ensure_started(), manager.ensure_started(), manager.ensure_started()
            )

        manager, results = asyncio.run(scenario())
        self.assertEqual(results, [RUNNING] * 3)
        self.assertEqual(len(self.host.launches), 1)
        self.assertTrue(any("服务就绪" in m for m in self.logger.messages("info")))

    def test_launch_command_cwd_and_environment(self):
        asyncio.run(self.make().ensure_started())
        launch = self.host.launches[0]
        self.assertEqual(
            launch["args"],
            [str(self.launch_dir / "runtime" / "python.exe"), "api.py", "-a", "127.0.0.1", "-p", "9880"],
        )
        self.assertEqual(launch["cwd"], str(self.launch_dir))
        self.assertEqual(launch["env"]["PYTHONUTF8"], "1")
        self.assertEqual(launch["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(launch["log_path"], self.data_dir / "tts_service.log")

    def test_spawn_process_uses_no_shell_and_hides_the_console(self):
        captured = {}

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess()

        log_path = self.data_dir / "tts_service.log"
        with patch.object(service_manager.subprocess, "Popen", fake_popen):
            service_manager.spawn_process(["python.exe", "api.py"], str(self.launch_dir), {"A": "1"}, log_path)
        kwargs = captured["kwargs"]
        self.assertIsInstance(captured["args"], list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["cwd"], str(self.launch_dir))
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_external_service_is_used_but_never_managed(self):
        self.host.port_open = True

        async def scenario():
            manager = self.make()
            state = await manager.ensure_started()
            status = await manager.status()
            stopped = await manager.stop()
            await manager.terminate()
            return state, status, stopped

        state, status, stopped = asyncio.run(scenario())
        self.assertEqual(state, EXTERNAL)
        self.assertEqual(status["state"], EXTERNAL)
        self.assertEqual(status["message"], EXTERNAL_MESSAGE)
        self.assertEqual(stopped, {"ok": False, "message": EXTERNAL_MESSAGE})
        self.assertEqual(self.host.launches, [])
        self.assertEqual(self.host.kills, [])

    def test_start_timeout_fails_and_kills_the_process(self):
        self.host.ready_after = None

        async def scenario():
            manager = self.make(start_timeout=30)
            with self.assertRaises(ServiceStartError):
                await manager.ensure_started()
            return await manager.status()

        status = asyncio.run(scenario())
        self.assertEqual(status["state"], FAILED)
        self.assertIn("启动超时", status["message"])
        self.assertEqual(self.host.kills, [4321])
        self.assertTrue(any("StartTimeout" in m for m in self.logger.messages("error")))

    def test_early_exit_fails_immediately_with_log_tail(self):
        self.host.exit_code = 3
        self.host.log_text = "line one\nTraceback boom\n"

        async def scenario():
            manager = self.make()
            with self.assertRaises(ServiceStartError):
                await manager.ensure_started()
            return await manager.status()

        status = asyncio.run(scenario())
        self.assertEqual(status["state"], FAILED)
        self.assertIn("退出码 3", status["message"])
        self.assertIn("Traceback boom", status["message"])
        self.assertLess(self.host.now, 5)
        self.assertEqual(status["log_tail"][-1], "Traceback boom")

    def test_missing_directory_or_python_fails_without_launching(self):
        async def check(manager):
            with self.assertRaises(ServiceStartError):
                await manager.ensure_started()
            return await manager.status()

        missing_dir = asyncio.run(check(self.make(launch_dir=str(self.root / "nope"))))
        (self.launch_dir / "runtime" / "python.exe").unlink()
        missing_python = asyncio.run(check(self.make()))
        self.assertEqual(missing_dir["state"], FAILED)
        self.assertIn("启动目录不存在", missing_dir["message"])
        self.assertEqual(missing_python["state"], FAILED)
        self.assertIn("python.exe", missing_python["message"])
        self.assertEqual(self.host.launches, [])

    def test_stop_ends_only_our_process_and_resets_state(self):
        async def scenario():
            manager = self.make()
            await manager.ensure_started()
            before = await manager.status()
            result = await manager.stop()
            after = await manager.status()
            return before, result, after

        before, result, after = asyncio.run(scenario())
        self.assertEqual(before["state"], RUNNING)
        self.assertEqual(before["pid"], 4321)
        self.assertTrue(result["ok"])
        self.assertEqual(after["state"], STOPPED)
        self.assertEqual(self.host.kills, [4321])

    def test_terminate_follows_stop_on_exit(self):
        for stop_on_exit, expected_kills in ((True, [4321]), (False, [])):
            with self.subTest(stop_on_exit=stop_on_exit):
                self.host = FakeHost()

                async def scenario():
                    manager = self.make(stop_on_exit=stop_on_exit)
                    await manager.ensure_started()
                    await manager.terminate()

                asyncio.run(scenario())
                self.assertEqual(self.host.kills, expected_kills)

    def test_job_object_only_when_stop_on_exit(self):
        asyncio.run(self.make(stop_on_exit=True).ensure_started())
        self.assertEqual([job.assigned for job in FakeJob.instances], [[4321]])
        FakeJob.instances = []
        self.host = FakeHost()
        asyncio.run(self.make(stop_on_exit=False).ensure_started())
        self.assertEqual(FakeJob.instances, [])

    def test_job_failure_only_warns(self):
        def broken_job():
            raise OSError("no job")

        state = asyncio.run(self.make(job_factory=broken_job).ensure_started())
        self.assertEqual(state, RUNNING)
        self.assertTrue(any("Job Object" in m for m in self.logger.messages("warning")))

    def test_previous_log_is_kept_as_dot_one(self):
        (self.data_dir / "tts_service.log").write_text("old run", encoding="utf-8")
        asyncio.run(self.make().ensure_started())
        self.assertEqual((self.data_dir / "tts_service.log.1").read_text(encoding="utf-8"), "old run")

    def test_restart_callback_fires_on_start_and_stop(self):
        async def scenario():
            manager = self.make()
            await manager.ensure_started()
            started = self.restarts
            await manager.stop()
            return started

        started = asyncio.run(scenario())
        self.assertGreaterEqual(started, 1)
        self.assertGreater(self.restarts, started)

    def test_real_process_that_crashes_fails_fast_not_at_timeout(self):
        # Real Popen: returncode only updates through poll(), so this guards that.
        def crashing_launcher(args, cwd, env, log_path):
            return service_manager.spawn_process(
                [sys.executable, "-c", "import sys; sys.exit(3)"], cwd, env, log_path
            )

        async def scenario():
            manager = self.make(
                launcher=crashing_launcher,
                clock=time.monotonic,
                sleep=asyncio.sleep,
                poll_interval=0.05,
                start_timeout=60,
            )
            with self.assertRaises(ServiceStartError):
                await manager.ensure_started()
            return await manager.status()

        begun = time.monotonic()
        status = asyncio.run(scenario())
        self.assertLess(time.monotonic() - begun, 15)
        self.assertEqual(status["state"], FAILED)
        self.assertIn("退出码 3", status["message"])
        self.assertTrue(any("ProcessExited" in m for m in self.logger.messages("error")))

    def test_real_process_that_dies_later_is_no_longer_running(self):
        port = {"up": False}

        def short_launcher(args, cwd, env, log_path):
            process = service_manager.spawn_process(
                [sys.executable, "-c", "import sys, time; time.sleep(1); sys.exit(4)"],
                cwd, env, log_path,
            )
            port["up"] = True  # the API answers once launched
            return process

        async def scenario():
            async def probe(host, port_number, timeout):
                return port["up"]

            manager = self.make(launcher=short_launcher, probe=probe)
            await manager.ensure_started()
            port["up"] = False  # the port closes together with the process
            for _ in range(100):
                status = await manager.status()
                if status["state"] != RUNNING:
                    return status
                await asyncio.sleep(0.1)
            return status

        status = asyncio.run(scenario())
        self.assertEqual(status["state"], FAILED)
        self.assertIn("退出码 4", status["message"])

    def test_hung_own_process_is_replaced_not_orphaned(self):
        async def scenario():
            manager = self.make()
            await manager.ensure_started()
            first = self.host.process
            self.host.port_open = False  # still alive, but the port stopped answering
            state = await manager.ensure_started()
            return state, first

        state, first = asyncio.run(scenario())
        self.assertEqual(state, RUNNING)
        self.assertEqual(len(self.host.launches), 2)
        self.assertEqual(self.host.kills, [first.pid])
        self.assertIsNotNone(first.poll())
        self.assertTrue(any("端口不通" in m for m in self.logger.messages("warning")))

    def test_unexpected_exit_is_reported_as_failed(self):
        async def scenario():
            manager = self.make()
            await manager.ensure_started()
            self.host.process.returncode = 9
            self.host.port_open = False
            return await manager.status()

        status = asyncio.run(scenario())
        self.assertEqual(status["state"], FAILED)
        self.assertIn("退出码 9", status["message"])


class IrodoriServiceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.launch_dir = self.root / "Irodori-TTS"
        self.server_dir = self.launch_dir / "Irodori-TTS-Server"
        (self.launch_dir / ".venv" / "Scripts").mkdir(parents=True)
        (self.launch_dir / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        self.entry = self.server_dir / "src" / "irodori_openai_tts" / "__main__.py"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("", encoding="utf-8")
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.host = FakeHost()
        self.logger = RecordingLogger()
        FakeJob.instances = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **overrides):
        kwargs = dict(
            host="127.0.0.1",
            port=8088,
            launch_dir=str(self.launch_dir),
            start_timeout=180,
            stop_on_exit=True,
            data_dir=self.data_dir,
            logger=self.logger,
            launcher=self.host.launcher,
            probe=self.host.probe,
            killer=self.host.killer,
            job_factory=FakeJob,
            clock=self.host.clock,
            sleep=self.host.sleep,
            service_name="Irodori",
            python_path=str(self.launch_dir / ".venv" / "Scripts" / "python.exe"),
            args=["-m", "irodori_openai_tts", "--host", "127.0.0.1", "--port", "8088"],
            extra_env={"PYTHONPATH": str(self.server_dir / "src"), "IRODORI_PRELOAD": "true"},
            required_files=[str(self.entry)],
        )
        kwargs.update(overrides)
        return ServiceManager(**kwargs)

    def test_launch_command_cwd_and_environment(self):
        self.assertEqual(asyncio.run(self.make().ensure_started()), RUNNING)
        launch = self.host.launches[0]
        self.assertEqual(
            launch["args"],
            [str(self.launch_dir / ".venv" / "Scripts" / "python.exe"),
             "-m", "irodori_openai_tts", "--host", "127.0.0.1", "--port", "8088"],
        )
        self.assertEqual(launch["cwd"], str(self.launch_dir))
        self.assertEqual(launch["env"]["PYTHONPATH"], str(self.server_dir / "src"))
        self.assertEqual(launch["env"]["IRODORI_PRELOAD"], "true")
        self.assertEqual(launch["env"]["PYTHONUTF8"], "1")
        self.assertTrue(any("Irodori 服务就绪" in m for m in self.logger.messages("info")))
        self.assertFalse(any("GPT-SoVITS" in m for _, m in self.logger.entries))

    def test_status_reports_the_service_name(self):
        status = asyncio.run(self.make().status())
        self.assertEqual(status["service_name"], "Irodori")
        self.assertEqual(status["port"], 8088)

    def test_missing_files_fail_without_launching(self):
        async def check(manager):
            with self.assertRaises(ServiceStartError):
                await manager.ensure_started()
            return await manager.status()

        self.entry.unlink()
        missing_entry = asyncio.run(check(self.make()))
        self.assertEqual(missing_entry["state"], FAILED)
        self.assertIn("__main__.py", missing_entry["message"])
        self.entry.write_text("", encoding="utf-8")
        (self.launch_dir / ".venv" / "Scripts" / "python.exe").unlink()
        missing_python = asyncio.run(check(self.make()))
        self.assertIn("python.exe", missing_python["message"])
        self.assertEqual(self.host.launches, [])

    def test_default_manager_keeps_the_gpt_sovits_command(self):
        manager = ServiceManager(
            host="127.0.0.1", port=9880, launch_dir=str(self.launch_dir), start_timeout=30,
            stop_on_exit=True, data_dir=self.data_dir, logger=self.logger,
        )
        self.assertEqual(manager.service_name, "GPT-SoVITS")
        self.assertEqual(
            manager.command,
            [str(self.launch_dir / "runtime" / "python.exe"), "api.py", "-a", "127.0.0.1", "-p", "9880"],
        )

    def test_real_process_receives_the_extra_environment(self):
        # Real subprocess through the real spawn_process: only the port probe is faked.
        script = "import os,sys;print('PRELOAD='+os.environ['IRODORI_PRELOAD'],'PP='+os.environ['PYTHONPATH']);sys.exit(7)"

        async def never_up(host, port, timeout):
            return False

        manager = self.make(
            launcher=service_manager.spawn_process,
            probe=never_up,
            clock=__import__("time").monotonic,
            sleep=asyncio.sleep,
            poll_interval=0.05,
            python_path=sys.executable,
            args=["-c", script],
            required_files=[],
            job_factory=FakeJob,
        )

        async def scenario():
            with self.assertRaises(ServiceStartError) as caught:
                await manager.ensure_started()
            return str(caught.exception)

        message = asyncio.run(scenario())
        self.assertIn("退出码 7", message)
        self.assertIn("PRELOAD=true", message)
        self.assertIn(str(self.server_dir / "src"), message)
