"""Start, watch and stop the local TTS service process (GPT-SoVITS ``api.py`` or Irodori) for the plugin."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
EXTERNAL = "external"
FAILED = "failed"

EXTERNAL_MESSAGE = "外部启动的服务，插件不管理"
DIR_NOT_SET_TEXT = "未设置目录"
NOT_RUNNING_MESSAGE = "服务未在运行"

LOG_FILE_NAME = "tts_service.log"
LOG_TAIL_LINES = 30
LOG_TAIL_BYTES = 16 * 1024
PROBE_TIMEOUT_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 1.0
STOP_WAIT_SECONDS = 10.0

_CREATE_NO_WINDOW = 0x08000000


class ServiceStartError(Exception):
    """Raised by ``ensure_started`` when the service could not be made ready."""


def _log_tail(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return [line.rstrip("\r") for line in text.split("\n") if line.strip()][-lines:]


def _exit_code(process: Any) -> int | None:
    # Popen.returncode is only refreshed by poll(), so never read it directly.
    return process.poll()


async def tcp_probe(host: str, port: int, timeout: float) -> bool:
    """Return True when a TCP connection to the API port succeeds."""
    target = "127.0.0.1" if host in ("", "0.0.0.0") else host
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(target, port), timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def spawn_process(
    args: list[str], cwd: str, env: dict[str, str], log_path: Path
) -> subprocess.Popen:
    """Start the API without a shell or console window, output going to ``log_path``."""
    flags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with open(log_path, "ab") as log_file:
        return subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )


async def kill_process_tree(pid: int) -> None:
    """End ``pid`` and all of its children (``taskkill /T /F`` on Windows)."""
    if sys.platform == "win32":
        args = ["taskkill", "/PID", str(pid), "/T", "/F"]
    else:
        args = ["kill", "-9", str(pid)]

    def _run() -> None:
        subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            check=False,
        )

    await asyncio.get_running_loop().run_in_executor(None, _run)


class KillOnCloseJob:
    """Windows Job Object that kills its processes when the last handle closes.

    The handle is owned by this AstrBot process, so the child dies with it even
    when AstrBot crashes or is force-closed.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        self._ctypes = ctypes
        self._kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle = handle

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        kernel32 = self._kernel32
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise OSError(self._ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, process):
                raise OSError(
                    self._ctypes.get_last_error(), "AssignProcessToJobObject failed"
                )
        finally:
            kernel32.CloseHandle(process)

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._kernel32.CloseHandle(handle)


class ServiceManager:
    """Lifecycle of one TTS service process; only a process started here is managed.

    By default it runs GPT-SoVITS ``api.py`` with the ``runtime/python.exe`` under ``launch_dir``.
    ``python_path``, ``args``, ``extra_env``, ``required_files`` and ``service_name``
    let another engine reuse the same start/stop/watch logic.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        launch_dir: str,
        start_timeout: float,
        stop_on_exit: bool,
        data_dir: Path,
        logger: Any,
        on_restart: Callable[[], None] | None = None,
        launcher: Callable[[list[str], str, dict[str, str], Path], Any] = spawn_process,
        probe: Callable[[str, int, float], Awaitable[bool]] = tcp_probe,
        killer: Callable[[int], Awaitable[None]] = kill_process_tree,
        job_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        service_name: str = "GPT-SoVITS",
        python_path: str | None = None,
        args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        required_files: list[str] | None = None,
        log_name: str = LOG_FILE_NAME,
    ) -> None:
        self.host = host
        self.port = port
        self.launch_dir = launch_dir
        self.start_timeout = start_timeout
        self.stop_on_exit = stop_on_exit
        self.log_path = Path(data_dir) / log_name
        self.service_name = service_name
        self._python_path = python_path
        self._args = list(args) if args is not None else None
        self._extra_env = dict(extra_env or {})
        self._required_files = list(required_files or [])
        self._logger = logger
        self._on_restart = on_restart
        self._launcher = launcher
        self._probe = probe
        self._killer = killer
        self._job_factory = job_factory if job_factory is not None else self._default_job
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._process: Any = None
        self._start_task: asyncio.Task | None = None
        self._stop_requested = False
        self._failure: str | None = None
        self._external_logged = False
        self._job: Any = None

    @staticmethod
    def _default_job() -> Any:
        if sys.platform != "win32":
            raise OSError("job objects are only available on Windows")
        return KillOnCloseJob()

    @property
    def python_path(self) -> Path:
        if self._python_path:
            return Path(self._python_path)
        return Path(self.launch_dir) / "runtime" / "python.exe"

    @property
    def command(self) -> list[str]:
        args = self._args if self._args is not None else [
            "api.py",
            "-a",
            self.host,
            "-p",
            str(self.port),
        ]
        return [str(self.python_path), *args]

    def _own_process_alive(self) -> bool:
        process = self._process
        if process is None:
            return False
        code = _exit_code(process)
        if code is None:
            return True
        self._failure = f"服务进程已退出（退出码 {code}）"
        self._logger.warning(
            f"[STBridge] {self.service_name} 服务进程已退出 code={code}"
        )
        self._process = None
        self._notify_restart()
        return False

    def _notify_restart(self) -> None:
        if self._on_restart is not None:
            self._on_restart()

    async def is_up(self, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
        return await self._probe(self.host, self.port, timeout)

    async def status(self) -> dict[str, Any]:
        alive = self._own_process_alive()
        starting = self._start_task is not None and not self._start_task.done()
        message = ""
        if starting:
            state = STARTING
        elif alive:
            state = RUNNING
        elif await self.is_up():
            state = EXTERNAL
            message = EXTERNAL_MESSAGE
        elif self._failure:
            state = FAILED
            message = self._failure
        else:
            state = STOPPED
        if not str(self.launch_dir).strip() and state in (STOPPED, FAILED):
            message = DIR_NOT_SET_TEXT
        return {
            "state": state,
            "pid": self._process.pid if alive else None,
            "message": message,
            "service_name": self.service_name,
            "launch_dir": self.launch_dir,
            "host": self.host,
            "port": self.port,
            "log_tail": _log_tail(self.log_path),
        }

    async def ensure_started(self) -> str:
        """Make sure the API answers; return ``running`` or ``external``.

        Concurrent callers share one start. Raises ``ServiceStartError``.
        """
        if self._own_process_alive() and not self._starting():
            if await self.is_up():
                return RUNNING
            # Alive but not answering (hung): replace it instead of orphaning it.
            self._logger.warning(
                f"[STBridge] {self.service_name} 服务进程仍在但端口不通，重启服务"
            )
            await self._terminate_process()
        if self._starting():
            return await self._wait_start()
        if self._process is None and await self.is_up():
            if not self._external_logged:
                self._external_logged = True
                self._logger.info(
                    f"[STBridge] 检测到外部启动的 {self.service_name} 服务，插件不管理"
                )
            return EXTERNAL
        self._external_logged = False
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.ensure_future(self._start())
        return await self._wait_start()

    def _starting(self) -> bool:
        return self._start_task is not None and not self._start_task.done()

    async def _wait_start(self) -> str:
        # shield: one waiting caller being cancelled must not abort the shared start
        return await asyncio.shield(self._start_task)

    async def _start(self) -> str:
        self._failure = None
        self._stop_requested = False
        problem = self._check_launch_files()
        if problem is not None:
            return self._fail("LaunchFilesMissing", problem)

        self._rotate_log()
        env = dict(os.environ)
        env.update(
            PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"
        )
        env.update(self._extra_env)
        self._logger.info(
            f"[STBridge] 启动 {self.service_name} 服务 dir={self.launch_dir} "
            f"port={self.port}"
        )
        begun = self._clock()
        try:
            self._process = self._launcher(
                self.command, self.launch_dir, env, self.log_path
            )
        except Exception as error:
            return self._fail(type(error).__name__, f"无法启动进程：{error}")
        self._notify_restart()
        self._attach_job()

        while True:
            process = self._process
            if process is None or self._stop_requested:
                self._failure = None
                raise ServiceStartError("服务启动已被取消")
            code = _exit_code(process)
            if code is not None:
                self._process = None
                tail = " | ".join(_log_tail(self.log_path, 5))
                return self._fail(
                    "ProcessExited",
                    f"服务进程提前退出（退出码 {code}）：{tail}",
                )
            if await self.is_up():
                elapsed = self._clock() - begun
                self._logger.info(
                    f"[STBridge] {self.service_name} 服务就绪 elapsed={elapsed:.1f}s "
                    f"pid={process.pid}"
                )
                return RUNNING
            if self._clock() - begun > self.start_timeout:
                await self._terminate_process()
                return self._fail(
                    "StartTimeout", f"启动超时（{int(self.start_timeout)} 秒）"
                )
            await self._sleep(self._poll_interval)

    def _fail(self, kind: str, message: str) -> str:
        self._failure = message
        self._logger.error(
            f"[STBridge] {self.service_name} 服务启动失败 ({kind})"
        )
        raise ServiceStartError(message)

    def _check_launch_files(self) -> str | None:
        if not str(self.launch_dir).strip():
            return DIR_NOT_SET_TEXT
        if not Path(self.launch_dir).is_dir():
            return f"启动目录不存在：{self.launch_dir}"
        if not self.python_path.is_file():
            return f"未找到 {self.python_path}"
        for required in self._required_files:
            if not Path(required).is_file():
                return f"未找到 {required}"
        return None

    def _rotate_log(self) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists():
                os.replace(self.log_path, self.log_path.with_name(self.log_path.name + ".1"))
        except OSError as error:
            self._logger.warning(
                "[STBridge] 服务日志轮换失败 "
                f"({type(error).__name__})"
            )

    def _attach_job(self) -> None:
        if not self.stop_on_exit:
            return
        try:
            if self._job is None:
                self._job = self._job_factory()
            self._job.assign(self._process.pid)
        except Exception as error:
            self._logger.warning(
                "[STBridge] 无法把服务放入 Job Object，AstrBot 异常退出时服务可能残留 "
                f"({type(error).__name__})"
            )

    async def _terminate_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            await self._killer(process.pid)
        finally:
            deadline = self._clock() + STOP_WAIT_SECONDS
            while self._clock() < deadline:
                if _exit_code(process) is not None and not await self.is_up(0.2):
                    break
                await self._sleep(0.2)
            self._notify_restart()

    async def stop(self) -> dict[str, Any]:
        """Stop the service this plugin started; refuse to touch anything else."""
        alive = self._own_process_alive()
        if not alive:
            if await self.is_up():
                return {"ok": False, "message": EXTERNAL_MESSAGE}
            return {"ok": False, "message": NOT_RUNNING_MESSAGE}
        pid = self._process.pid
        self._stop_requested = True
        await self._terminate_process()
        self._failure = None
        self._logger.info(f"[STBridge] {self.service_name} 服务已关闭 pid={pid}")
        return {"ok": True, "message": "服务已关闭"}

    async def terminate(self) -> None:
        """Plugin shutdown: stop the service only if it is ours and stop_on_exit is on."""
        if self.stop_on_exit and self._own_process_alive():
            await self.stop()
