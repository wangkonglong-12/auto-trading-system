"""Ordered Runtime startup, rollback, and shutdown orchestration."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Generic, Protocol, TypeVar

from auto_trading_system.runtime.decision_loop import DecisionEventLoop
from auto_trading_system.runtime.scheduler import RuntimeScheduler

EventT = TypeVar("EventT")


class LifecyclePhase(IntEnum):
    PERSISTENCE = 10
    DECISION_LOOP = 20
    BROKER_SESSION = 30
    WORKER = 40
    SCHEDULER = 50
    CONTROL_API = 60


class RuntimeLifecycleStatus(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class RuntimeLifecycleError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        component: str | None = None,
        cleanup_failures: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.component = component
        self.cleanup_failures = cleanup_failures


class AccountOwnership(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeComponent:
    name: str
    phase: LifecyclePhase
    start: Callable[[], Awaitable[None]]
    stop: Callable[[], Awaitable[None]]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]{2,63}", self.name) is None:
            raise ValueError("component name is invalid")
        if not isinstance(self.phase, LifecyclePhase):
            raise ValueError("phase must be a LifecyclePhase")
        if not callable(self.start) or not callable(self.stop):
            raise ValueError("component start and stop must be callable")


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleSnapshot:
    status: RuntimeLifecycleStatus
    owns_account: bool
    started_components: tuple[str, ...]


class ManagedRuntime:
    """Acquire account ownership first and stop every component in reverse order."""

    def __init__(
        self,
        *,
        account_ownership: AccountOwnership,
        components: Sequence[RuntimeComponent],
    ) -> None:
        names = [component.name for component in components]
        if len(names) != len(set(names)):
            raise ValueError("runtime component names must be unique")
        if not components:
            raise ValueError("at least one runtime component is required")
        singleton_phases = {
            LifecyclePhase.PERSISTENCE,
            LifecyclePhase.DECISION_LOOP,
            LifecyclePhase.BROKER_SESSION,
            LifecyclePhase.SCHEDULER,
            LifecyclePhase.CONTROL_API,
        }
        for phase in singleton_phases:
            if sum(component.phase is phase for component in components) > 1:
                raise ValueError(f"runtime phase {phase.name} permits only one component")
        self._ownership = account_ownership
        self._components = tuple(sorted(components, key=lambda component: component.phase))
        self._lock = asyncio.Lock()
        self._status = RuntimeLifecycleStatus.CREATED
        self._owns_account = False
        self._started: list[RuntimeComponent] = []

    @property
    def status(self) -> RuntimeLifecycleStatus:
        return self._status

    def snapshot(self) -> RuntimeLifecycleSnapshot:
        return RuntimeLifecycleSnapshot(
            status=self._status,
            owns_account=self._owns_account,
            started_components=tuple(component.name for component in self._started),
        )

    async def start(self) -> None:
        async with self._lock:
            if self._status is not RuntimeLifecycleStatus.CREATED:
                raise RuntimeLifecycleError(
                    "RUNTIME_NOT_STARTABLE", f"cannot start from {self._status}"
                )
            self._status = RuntimeLifecycleStatus.STARTING
            try:
                self._ownership.acquire()
                self._owns_account = True
            except Exception as exc:
                self._status = RuntimeLifecycleStatus.FAILED
                raise RuntimeLifecycleError(
                    "RUNTIME_OWNERSHIP_FAILED", "account ownership could not be acquired"
                ) from exc

            active_component: RuntimeComponent | None = None
            try:
                for component in self._components:
                    active_component = component
                    self._started.append(component)
                    await component.start()
            except asyncio.CancelledError:
                cleanup_failures = await self._rollback_started()
                self._status = (
                    RuntimeLifecycleStatus.FAILED
                    if cleanup_failures
                    else RuntimeLifecycleStatus.STOPPED
                )
                raise
            except Exception as exc:
                cleanup_failures = await self._rollback_started()
                self._status = RuntimeLifecycleStatus.FAILED
                raise RuntimeLifecycleError(
                    "RUNTIME_COMPONENT_START_FAILED",
                    "runtime component failed to start",
                    component=active_component.name if active_component is not None else None,
                    cleanup_failures=cleanup_failures,
                ) from exc
            self._status = RuntimeLifecycleStatus.RUNNING

    async def stop(self) -> None:
        async with self._lock:
            if self._status is RuntimeLifecycleStatus.STOPPED:
                return
            if self._status is RuntimeLifecycleStatus.CREATED:
                self._status = RuntimeLifecycleStatus.STOPPED
                return
            self._status = RuntimeLifecycleStatus.STOPPING
            failures = await self._stop_started()
            ownership_failure = self._release_ownership()
            if ownership_failure:
                failures.append("account-ownership")
            if failures:
                self._status = RuntimeLifecycleStatus.FAILED
                raise RuntimeLifecycleError(
                    "RUNTIME_STOP_FAILED",
                    "one or more runtime components failed to stop",
                    cleanup_failures=tuple(failures),
                )
            self._status = RuntimeLifecycleStatus.STOPPED

    async def run_until(self, stop_requested: asyncio.Event) -> None:
        await self.start()
        try:
            await stop_requested.wait()
        finally:
            await self.stop()

    async def _rollback_started(self) -> tuple[str, ...]:
        failures = await self._stop_started()
        if self._release_ownership():
            failures.append("account-ownership")
        return tuple(failures)

    async def _stop_started(self) -> list[str]:
        failures: list[str] = []
        while self._started:
            component = self._started.pop()
            try:
                await component.stop()
            except Exception:
                failures.append(component.name)
        return failures

    def _release_ownership(self) -> bool:
        if not self._owns_account:
            return False
        try:
            self._ownership.release()
        except Exception:
            return True
        self._owns_account = False
        return False


class DecisionLoopComponent(Generic[EventT]):
    """Adapt a DecisionEventLoop to the managed lifecycle without creating another loop."""

    def __init__(self, decision_loop: DecisionEventLoop[EventT]) -> None:
        self._loop = decision_loop
        self._task: asyncio.Task[None] | None = None
        self._started_once = False

    async def start(self) -> None:
        if self._started_once:
            raise RuntimeLifecycleError(
                "DECISION_LOOP_COMPONENT_ALREADY_STARTED", "decision loop component already started"
            )
        self._started_once = True
        self._task = asyncio.create_task(self._loop.run(), name="runtime-decision-loop")
        await asyncio.sleep(0)
        if self._task.done():
            await self._task

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._loop.request_stop()
        await task
        self._task = None

    def definition(self) -> RuntimeComponent:
        return RuntimeComponent(
            name="decision-loop",
            phase=LifecyclePhase.DECISION_LOOP,
            start=self.start,
            stop=self.stop,
        )


class SchedulerComponent:
    """Adapt the single RuntimeScheduler to async lifecycle ordering."""

    def __init__(self, scheduler: RuntimeScheduler) -> None:
        self._scheduler = scheduler

    async def start(self) -> None:
        self._scheduler.start()

    async def stop(self) -> None:
        self._scheduler.stop()

    def definition(self) -> RuntimeComponent:
        return RuntimeComponent(
            name="runtime-scheduler",
            phase=LifecyclePhase.SCHEDULER,
            start=self.start,
            stop=self.stop,
        )
