"""Lifecycle-managed Uvicorn host for the loopback operator console."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from enum import StrEnum
from typing import Any, Protocol

import uvicorn

from auto_trading_system.runtime import LifecyclePhase, RuntimeComponent


class LocalConsoleServerStatus(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class LocalConsoleServerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class UvicornServer(Protocol):
    started: bool
    should_exit: bool
    force_exit: bool

    def serve(self, sockets: list[Any] | None = None) -> Coroutine[Any, Any, None]: ...


class LocalConsoleServer:
    """Start after all trading workers and stop before them through RuntimeLifecycle."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        startup_timeout_seconds: float = 10.0,
        shutdown_timeout_seconds: float = 10.0,
        server_factory: Callable[[uvicorn.Config], UvicornServer] = uvicorn.Server,
    ) -> None:
        if not isinstance(config, uvicorn.Config):
            raise ValueError("config must be a uvicorn.Config")
        for value, name in (
            (startup_timeout_seconds, "startup_timeout_seconds"),
            (shutdown_timeout_seconds, "shutdown_timeout_seconds"),
        ):
            if type(value) not in {int, float} or not 0.1 <= value <= 300:
                raise ValueError(f"{name} must be between 0.1 and 300")
        if not callable(server_factory):
            raise ValueError("server_factory must be callable")
        self._config = config
        self._startup_timeout = float(startup_timeout_seconds)
        self._shutdown_timeout = float(shutdown_timeout_seconds)
        self._server_factory = server_factory
        self._status = LocalConsoleServerStatus.CREATED
        self._server: UvicornServer | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> LocalConsoleServerStatus:
        return self._status

    async def start(self) -> None:
        if self._status is not LocalConsoleServerStatus.CREATED:
            raise LocalConsoleServerError(
                "LOCAL_CONSOLE_NOT_STARTABLE",
                "local console cannot start from its current state",
            )
        self._status = LocalConsoleServerStatus.STARTING
        try:
            server = self._server_factory(self._config)
            serve = getattr(server, "serve", None)
            if not callable(serve):
                raise TypeError("server does not provide serve")
            self._server = server
            self._task = asyncio.create_task(serve(), name="local-console-server")
            await self._wait_until_started(server, self._task)
        except asyncio.CancelledError:
            await self._abort_start()
            raise
        except Exception as exc:
            await self._abort_start()
            raise LocalConsoleServerError(
                "LOCAL_CONSOLE_START_FAILED",
                "local console did not reach ready state",
            ) from exc
        self._status = LocalConsoleServerStatus.RUNNING

    async def stop(self) -> None:
        if self._status is LocalConsoleServerStatus.STOPPED:
            return
        if self._status is LocalConsoleServerStatus.CREATED:
            self._status = LocalConsoleServerStatus.STOPPED
            return
        server = self._server
        task = self._task
        if server is None or task is None:
            self._status = LocalConsoleServerStatus.FAILED
            raise LocalConsoleServerError(
                "LOCAL_CONSOLE_LIFECYCLE_INVALID",
                "local console lifecycle state is incomplete",
            )
        self._status = LocalConsoleServerStatus.STOPPING
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_timeout)
        except asyncio.CancelledError:
            await self._cancel_task(server, task)
            self._status = LocalConsoleServerStatus.FAILED
            raise
        except TimeoutError as exc:
            await self._cancel_task(server, task)
            self._status = LocalConsoleServerStatus.FAILED
            raise LocalConsoleServerError(
                "LOCAL_CONSOLE_STOP_TIMEOUT",
                "local console did not stop before the deadline",
            ) from exc
        except Exception as exc:
            self._status = LocalConsoleServerStatus.FAILED
            raise LocalConsoleServerError(
                "LOCAL_CONSOLE_STOP_FAILED",
                "local console stopped with an internal failure",
            ) from exc
        self._status = LocalConsoleServerStatus.STOPPED

    def runtime_component(self) -> RuntimeComponent:
        return RuntimeComponent(
            name="local-control-api",
            phase=LifecyclePhase.CONTROL_API,
            start=self.start,
            stop=self.stop,
        )

    async def _wait_until_started(
        self,
        server: UvicornServer,
        task: asyncio.Task[None],
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("server exited before startup")
            if loop.time() >= deadline:
                raise TimeoutError("server startup timed out")
            await asyncio.sleep(0.01)

    async def _abort_start(self) -> None:
        server = self._server
        task = self._task
        if server is not None and task is not None:
            await self._cancel_task(server, task)
        self._status = LocalConsoleServerStatus.FAILED

    @staticmethod
    async def _cancel_task(server: UvicornServer, task: asyncio.Task[None]) -> None:
        server.should_exit = True
        server.force_exit = True
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
