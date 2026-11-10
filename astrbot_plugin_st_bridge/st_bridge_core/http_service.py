"""Lifecycle wrapper for the bridge's loopback-only aiohttp listener."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Any, TypeVar

from aiohttp import web

from .http_protocol import BridgeRecoveryGate, HttpProtocolConfig


_T = TypeVar("_T")


class HttpServiceState(str, Enum):
    NEW = "NEW"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class LocalHttpService:
    """Own one aiohttp runner/site pair and any tasks created by this service."""

    def __init__(
        self,
        app: web.Application,
        config: HttpProtocolConfig,
        recovery_gate: BridgeRecoveryGate,
        *,
        runner_factory: Callable[..., web.AppRunner] = web.AppRunner,
        site_factory: Callable[..., web.TCPSite] = web.TCPSite,
    ) -> None:
        if config.listen_host != "127.0.0.1":
            raise ValueError("listen_host must be 127.0.0.1")

        self._app = app
        self._config = config
        self._recovery_gate = recovery_gate
        self._runner_factory = runner_factory
        self._site_factory = site_factory
        self._lock = asyncio.Lock()
        self._state = HttpServiceState.NEW
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def app(self) -> web.Application:
        return self._app

    @property
    def state(self) -> HttpServiceState:
        return self._state

    @property
    def runner(self) -> web.AppRunner | None:
        return self._runner

    @property
    def site(self) -> web.TCPSite | None:
        return self._site

    @property
    def owned_task_count(self) -> int:
        return len(self._owned_tasks)

    async def start(self) -> None:
        async with self._lock:
            if self._state is HttpServiceState.RUNNING:
                return
            if self._state is not HttpServiceState.NEW:
                raise RuntimeError(f"cannot start HTTP service in {self._state.value}")

            self._state = HttpServiceState.STARTING
            runner: web.AppRunner | None = None
            try:
                runner = self._runner_factory(self._app, handle_signals=False)
                self._runner = runner
                await runner.setup()
                site = self._site_factory(
                    runner,
                    host="127.0.0.1",
                    port=self._config.listen_port,
                    reuse_address=False,
                    reuse_port=False,
                )
                self._site = site
                await site.start()
            except BaseException as start_error:
                self._state = HttpServiceState.STOPPING
                try:
                    if runner is not None:
                        await self._cleanup_runner_shielded(runner)
                except BaseException as cleanup_error:
                    self._state = HttpServiceState.FAILED
                    raise cleanup_error from start_error

                self._runner = None
                self._site = None
                self._state = HttpServiceState.NEW
                raise

            self._state = HttpServiceState.RUNNING

    async def stop(self) -> None:
        async with self._lock:
            if self._state is HttpServiceState.STOPPED:
                return
            if self._state is HttpServiceState.NEW:
                self._state = HttpServiceState.STOPPING
                self._recovery_gate.begin_shutdown()
                self._state = HttpServiceState.STOPPED
                self._recovery_gate.mark_stopped()
                return

            if self._stop_task is None or self._stop_task.done():
                self._state = HttpServiceState.STOPPING
                self._recovery_gate.begin_shutdown()
                self._stop_task = asyncio.create_task(
                    self._stop_resources(),
                    name="st-bridge-http-cleanup",
                )
            stop_task = self._stop_task

        await self._await_cleanup_task(stop_task)

    def create_owned_task(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[_T]:
        if self._state is not HttpServiceState.RUNNING:
            coroutine.close()
            raise RuntimeError("HTTP service is not running")
        task = asyncio.create_task(coroutine, name=name)
        self._owned_tasks.add(task)
        return task

    async def _stop_resources(self) -> None:
        site = self._site
        runner = self._runner
        site_failure: BaseException | None = None
        runner_failure: BaseException | None = None

        if site is not None:
            try:
                await site.stop()
            except BaseException as exc:
                site_failure = exc

        tasks = tuple(self._owned_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()

        if runner is not None:
            try:
                await runner.cleanup()
            except BaseException as exc:
                runner_failure = exc

        if runner_failure is not None:
            failure = runner_failure
        elif runner is None:
            failure = site_failure
        else:
            failure = None
        if failure is not None:
            self._state = HttpServiceState.FAILED
            raise failure

        self._site = None
        self._runner = None
        self._state = HttpServiceState.STOPPED
        self._recovery_gate.mark_stopped()

    @staticmethod
    async def _cleanup_runner_shielded(runner: web.AppRunner) -> None:
        cleanup_task = asyncio.create_task(
            runner.cleanup(),
            name="st-bridge-http-startup-rollback",
        )
        await LocalHttpService._await_cleanup_task(cleanup_task)

    @staticmethod
    async def _await_cleanup_task(task: asyncio.Task[_T]) -> _T:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
