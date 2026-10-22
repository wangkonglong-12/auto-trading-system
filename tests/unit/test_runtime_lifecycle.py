from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Event, Thread
from typing import cast

import pytest

from auto_trading_system.runtime import (
    BoundedEventIngress,
    DecisionEventLoop,
    DecisionLoopComponent,
    IntervalSchedule,
    LifecyclePhase,
    ManagedRuntime,
    RuntimeComponent,
    RuntimeEventSource,
    RuntimeLifecycleError,
    RuntimeLifecycleSnapshot,
    RuntimeLifecycleStatus,
    RuntimeScheduler,
    ScheduledJobDefinition,
    ScheduledTask,
    SchedulerComponent,
)


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass
class FakeSchedulerBackend:
    intervals: dict[str, Callable[[], None]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def add_interval_job(
        self,
        *,
        job_id: str,
        seconds: Decimal,
        callback: Callable[[], None],
        misfire_grace_seconds: int,
    ) -> None:
        self.intervals[job_id] = callback
        self.calls.append(f"add:{job_id}")

    def add_daily_job(
        self,
        *,
        job_id: str,
        hour: int,
        minute: int,
        day_of_week: str,
        callback: Callable[[], None],
        misfire_grace_seconds: int,
    ) -> None:
        self.intervals[job_id] = callback
        self.calls.append(f"add:{job_id}")

    def reschedule_interval(self, *, job_id: str, seconds: Decimal) -> None:
        self.calls.append(f"reschedule:{job_id}:{seconds}")

    def start(self) -> None:
        self.calls.append("start")

    def shutdown(self, *, wait: bool) -> None:
        self.calls.append(f"shutdown:{wait}")

    def fire(self, job_id: str) -> None:
        self.intervals[job_id]()


@dataclass
class FakeAccountOwnership:
    log: list[str]
    acquire_error: Exception | None = None
    release_error: Exception | None = None
    acquired: bool = False
    acquire_calls: int = 0
    release_calls: int = 0

    def acquire(self) -> None:
        self.acquire_calls += 1
        self.log.append("ownership:start")
        if self.acquire_error is not None:
            raise self.acquire_error
        self.acquired = True

    def release(self) -> None:
        self.release_calls += 1
        self.log.append("ownership:stop")
        if self.release_error is not None:
            raise self.release_error
        self.acquired = False


@dataclass
class FakeComponentBehavior:
    log: list[str]
    start_error: Exception | None = None
    stop_error: Exception | None = None
    starts: int = 0
    stops: int = 0

    async def start(self, name: str) -> None:
        self.starts += 1
        self.log.append(f"{name}:start")
        if self.start_error is not None:
            raise self.start_error

    async def stop(self, name: str) -> None:
        self.stops += 1
        self.log.append(f"{name}:stop")
        if self.stop_error is not None:
            raise self.stop_error


def component(
    name: str,
    phase: LifecyclePhase,
    behavior: FakeComponentBehavior,
) -> RuntimeComponent:
    return RuntimeComponent(
        name=name,
        phase=phase,
        start=lambda: behavior.start(name),
        stop=lambda: behavior.stop(name),
    )


@pytest.mark.parametrize(
    ("name", "phase", "start", "stop", "message"),
    [
        (
            "BAD_NAME",
            LifecyclePhase.WORKER,
            lambda: asyncio.sleep(0),
            lambda: asyncio.sleep(0),
            "name",
        ),
        (
            "worker-one",
            "WORKER",
            lambda: asyncio.sleep(0),
            lambda: asyncio.sleep(0),
            "phase",
        ),
        (
            "worker-one",
            LifecyclePhase.WORKER,
            None,
            lambda: asyncio.sleep(0),
            "callable",
        ),
        (
            "worker-one",
            LifecyclePhase.WORKER,
            lambda: asyncio.sleep(0),
            None,
            "callable",
        ),
    ],
)
def test_runtime_component_definition_is_strict(
    name: str,
    phase: LifecyclePhase,
    start: Callable[[], Awaitable[None]],
    stop: Callable[[], Awaitable[None]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RuntimeComponent(name, phase, start, stop)


def test_managed_runtime_requires_nonempty_unique_components() -> None:
    log: list[str] = []
    ownership = FakeAccountOwnership(log)
    behavior = FakeComponentBehavior(log)
    worker = component("worker-one", LifecyclePhase.WORKER, behavior)

    with pytest.raises(ValueError, match="at least one"):
        ManagedRuntime(account_ownership=ownership, components=())
    with pytest.raises(ValueError, match="unique"):
        ManagedRuntime(account_ownership=ownership, components=(worker, worker))
    with pytest.raises(ValueError, match="permits only one"):
        ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("broker-one", LifecyclePhase.BROKER_SESSION, behavior),
                component("broker-two", LifecyclePhase.BROKER_SESSION, behavior),
            ),
        )


def test_start_orders_phases_and_stop_reverses_before_releasing_account() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log)
        persistence = FakeComponentBehavior(log)
        decision = FakeComponentBehavior(log)
        broker = FakeComponentBehavior(log)
        worker = FakeComponentBehavior(log)
        scheduler = FakeComponentBehavior(log)
        control = FakeComponentBehavior(log)
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("control-api", LifecyclePhase.CONTROL_API, control),
                component("runtime-scheduler", LifecyclePhase.SCHEDULER, scheduler),
                component("sync-worker", LifecyclePhase.WORKER, worker),
                component("broker-session", LifecyclePhase.BROKER_SESSION, broker),
                component("decision-loop", LifecyclePhase.DECISION_LOOP, decision),
                component("local-journal", LifecyclePhase.PERSISTENCE, persistence),
            ),
        )

        await runtime.start()
        running_snapshot = runtime.snapshot()
        await runtime.stop()

        assert log == [
            "ownership:start",
            "local-journal:start",
            "decision-loop:start",
            "broker-session:start",
            "sync-worker:start",
            "runtime-scheduler:start",
            "control-api:start",
            "control-api:stop",
            "runtime-scheduler:stop",
            "sync-worker:stop",
            "broker-session:stop",
            "decision-loop:stop",
            "local-journal:stop",
            "ownership:stop",
        ]
        assert running_snapshot == RuntimeLifecycleSnapshot(
            RuntimeLifecycleStatus.RUNNING,
            True,
            (
                "local-journal",
                "decision-loop",
                "broker-session",
                "sync-worker",
                "runtime-scheduler",
                "control-api",
            ),
        )
        assert runtime.snapshot() == RuntimeLifecycleSnapshot(
            RuntimeLifecycleStatus.STOPPED, False, ()
        )

    asyncio.run(scenario())


def test_account_ownership_failure_starts_no_component_and_is_sanitized() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log, acquire_error=RuntimeError("secret account"))
        behavior = FakeComponentBehavior(log)
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(component("worker-one", LifecyclePhase.WORKER, behavior),),
        )

        with pytest.raises(RuntimeLifecycleError, match="RUNTIME_OWNERSHIP_FAILED") as exc_info:
            await runtime.start()

        assert "secret account" not in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert behavior.starts == 0
        assert runtime.status is RuntimeLifecycleStatus.FAILED

    asyncio.run(scenario())


def test_component_start_failure_rolls_back_reverse_order_and_releases_account() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log)
        persistence = FakeComponentBehavior(log)
        decision = FakeComponentBehavior(log)
        broker = FakeComponentBehavior(log, start_error=RuntimeError("sdk secret"))
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("broker-session", LifecyclePhase.BROKER_SESSION, broker),
                component("decision-loop", LifecyclePhase.DECISION_LOOP, decision),
                component("local-journal", LifecyclePhase.PERSISTENCE, persistence),
            ),
        )

        with pytest.raises(
            RuntimeLifecycleError, match="RUNTIME_COMPONENT_START_FAILED"
        ) as exc_info:
            await runtime.start()

        assert exc_info.value.component == "broker-session"
        assert exc_info.value.cleanup_failures == ()
        assert "sdk secret" not in str(exc_info.value)
        assert log == [
            "ownership:start",
            "local-journal:start",
            "decision-loop:start",
            "broker-session:start",
            "broker-session:stop",
            "decision-loop:stop",
            "local-journal:stop",
            "ownership:stop",
        ]
        assert runtime.status is RuntimeLifecycleStatus.FAILED
        assert not ownership.acquired

    asyncio.run(scenario())


def test_start_failure_reports_cleanup_failures_and_failed_ownership_can_retry_stop() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log, release_error=RuntimeError("release"))
        persistence = FakeComponentBehavior(log, stop_error=RuntimeError("close"))
        broker = FakeComponentBehavior(log, start_error=RuntimeError("start"))
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("local-journal", LifecyclePhase.PERSISTENCE, persistence),
                component("broker-session", LifecyclePhase.BROKER_SESSION, broker),
            ),
        )

        with pytest.raises(RuntimeLifecycleError) as exc_info:
            await runtime.start()

        assert exc_info.value.cleanup_failures == ("local-journal", "account-ownership")
        assert runtime.snapshot().owns_account is True
        ownership.release_error = None
        await runtime.stop()
        assert runtime.status is RuntimeLifecycleStatus.STOPPED
        assert ownership.release_calls == 2

    asyncio.run(scenario())


def test_stop_attempts_every_component_and_account_release_after_failures() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log)
        first = FakeComponentBehavior(log, stop_error=RuntimeError("first"))
        second = FakeComponentBehavior(log, stop_error=RuntimeError("second"))
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("worker-one", LifecyclePhase.WORKER, first),
                component("worker-two", LifecyclePhase.WORKER, second),
            ),
        )
        await runtime.start()

        with pytest.raises(RuntimeLifecycleError, match="RUNTIME_STOP_FAILED") as exc_info:
            await runtime.stop()

        assert exc_info.value.cleanup_failures == ("worker-two", "worker-one")
        assert first.stops == second.stops == 1
        assert ownership.release_calls == 1
        assert runtime.status is RuntimeLifecycleStatus.FAILED

    asyncio.run(scenario())


def test_stop_failure_keeps_uncertain_account_ownership_for_retry() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log, release_error=RuntimeError("release"))
        behavior = FakeComponentBehavior(log)
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(component("worker-one", LifecyclePhase.WORKER, behavior),),
        )
        await runtime.start()

        with pytest.raises(RuntimeLifecycleError) as exc_info:
            await runtime.stop()
        assert exc_info.value.cleanup_failures == ("account-ownership",)
        assert runtime.snapshot().owns_account is True

        ownership.release_error = None
        await runtime.stop()
        assert runtime.snapshot().owns_account is False
        assert ownership.release_calls == 2

    asyncio.run(scenario())


def test_stop_before_start_and_repeated_stop_are_idempotent() -> None:
    async def scenario() -> None:
        log: list[str] = []
        behavior = FakeComponentBehavior(log)
        runtime = ManagedRuntime(
            account_ownership=FakeAccountOwnership(log),
            components=(component("worker-one", LifecyclePhase.WORKER, behavior),),
        )

        await runtime.stop()
        await runtime.stop()

        assert runtime.status is RuntimeLifecycleStatus.STOPPED
        assert log == []
        with pytest.raises(RuntimeLifecycleError, match="RUNTIME_NOT_STARTABLE"):
            await runtime.start()

    asyncio.run(scenario())


def test_runtime_cannot_start_twice() -> None:
    async def scenario() -> None:
        log: list[str] = []
        behavior = FakeComponentBehavior(log)
        runtime = ManagedRuntime(
            account_ownership=FakeAccountOwnership(log),
            components=(component("worker-one", LifecyclePhase.WORKER, behavior),),
        )
        await runtime.start()

        with pytest.raises(RuntimeLifecycleError, match="RUNTIME_NOT_STARTABLE"):
            await runtime.start()
        await runtime.stop()

    asyncio.run(scenario())


def test_start_cancellation_rolls_back_started_components_and_ownership() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log)
        first = FakeComponentBehavior(log)
        entered = asyncio.Event()

        async def blocking_start() -> None:
            log.append("blocking-worker:start")
            entered.set()
            await asyncio.Event().wait()

        async def blocking_stop() -> None:
            log.append("blocking-worker:stop")

        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("worker-one", LifecyclePhase.WORKER, first),
                RuntimeComponent(
                    "blocking-worker",
                    LifecyclePhase.SCHEDULER,
                    blocking_start,
                    blocking_stop,
                ),
            ),
        )
        task = asyncio.create_task(runtime.start())
        await entered.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert runtime.status is RuntimeLifecycleStatus.STOPPED
        assert first.stops == 1
        assert "blocking-worker:stop" in log
        assert ownership.release_calls == 1

    asyncio.run(scenario())


def test_run_until_starts_and_stops_around_event() -> None:
    async def scenario() -> None:
        log: list[str] = []
        behavior = FakeComponentBehavior(log)
        stop_requested = asyncio.Event()
        stop_requested.set()
        runtime = ManagedRuntime(
            account_ownership=FakeAccountOwnership(log),
            components=(component("worker-one", LifecyclePhase.WORKER, behavior),),
        )

        await runtime.run_until(stop_requested)

        assert log == [
            "ownership:start",
            "worker-one:start",
            "worker-one:stop",
            "ownership:stop",
        ]
        assert runtime.status is RuntimeLifecycleStatus.STOPPED

    asyncio.run(scenario())


def test_decision_loop_component_starts_one_task_and_drains_on_stop() -> None:
    async def scenario() -> None:
        ingress = BoundedEventIngress[object](
            normal_capacity=4,
            critical_capacity=2,
            monotonic_clock=IncrementingClock(),
        )
        handled: list[object] = []

        async def handler(dispatch: object) -> None:
            handled.append(dispatch)

        loop = DecisionEventLoop(ingress=ingress, handler=handler)
        adapter = DecisionLoopComponent(loop)
        definition = adapter.definition()

        await definition.start()
        ingress.publish("one", source=RuntimeEventSource.INTERNAL)
        ingress.publish("two", source=RuntimeEventSource.INTERNAL)
        await definition.stop()
        await definition.stop()

        assert len(handled) == 2
        with pytest.raises(RuntimeLifecycleError, match="ALREADY_STARTED"):
            await definition.start()

    asyncio.run(scenario())


def test_decision_loop_component_rejects_duplicate_active_start() -> None:
    async def scenario() -> None:
        ingress = BoundedEventIngress[object](
            normal_capacity=2,
            critical_capacity=1,
            monotonic_clock=IncrementingClock(),
        )
        loop = DecisionEventLoop(ingress=ingress, handler=lambda _: asyncio.sleep(0))
        adapter = DecisionLoopComponent(loop)
        await adapter.start()

        with pytest.raises(RuntimeLifecycleError, match="ALREADY_STARTED"):
            await adapter.start()
        await adapter.stop()

    asyncio.run(scenario())


def test_scheduler_component_uses_scheduler_lifecycle_without_another_process() -> None:
    async def scenario() -> None:
        ingress = BoundedEventIngress[object](
            normal_capacity=4,
            critical_capacity=2,
            monotonic_clock=IncrementingClock(),
        )
        backend = FakeSchedulerBackend()
        scheduler = RuntimeScheduler(
            ingress=ingress,
            jobs=(
                ScheduledJobDefinition(
                    "heartbeat",
                    ScheduledTask.HEARTBEAT,
                    IntervalSchedule(Decimal("5")),
                ),
            ),
            backend=backend,
        )
        adapter = SchedulerComponent(scheduler)
        definition = adapter.definition()

        await definition.start()
        await definition.stop()

        assert definition.phase is LifecyclePhase.SCHEDULER
        assert backend.calls == ["add:heartbeat", "start", "shutdown:True"]

    asyncio.run(scenario())


def test_many_scheduled_fires_do_not_create_another_broker_session() -> None:
    async def scenario() -> None:
        log: list[str] = []
        ownership = FakeAccountOwnership(log)
        broker = FakeComponentBehavior(log)
        ingress = BoundedEventIngress[object](
            normal_capacity=20,
            critical_capacity=2,
            monotonic_clock=IncrementingClock(),
        )
        backend = FakeSchedulerBackend()
        scheduler = RuntimeScheduler(
            ingress=ingress,
            jobs=(
                ScheduledJobDefinition(
                    "plan-sync",
                    ScheduledTask.PLAN_SYNC,
                    IntervalSchedule(Decimal("2")),
                ),
            ),
            backend=backend,
        )
        runtime = ManagedRuntime(
            account_ownership=ownership,
            components=(
                component("broker-session", LifecyclePhase.BROKER_SESSION, broker),
                SchedulerComponent(scheduler).definition(),
            ),
        )
        await runtime.start()

        for _ in range(10):
            backend.fire("plan-sync")
        await runtime.stop()

        assert broker.starts == 1
        assert broker.stops == 1
        assert ingress.depth().total == 10

    asyncio.run(scenario())


def test_runtime_component_rejects_untyped_phase_at_runtime() -> None:
    with pytest.raises(ValueError, match="phase"):
        RuntimeComponent(
            "worker-one",
            cast(LifecyclePhase, 40),
            lambda: asyncio.sleep(0),
            lambda: asyncio.sleep(0),
        )


def test_scheduler_start_in_progress_rejects_concurrent_stop() -> None:
    class BlockingBackend(FakeSchedulerBackend):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def start(self) -> None:
            self.entered.set()
            assert self.release.wait(timeout=2)

    backend = BlockingBackend()
    ingress = BoundedEventIngress[object](
        normal_capacity=2,
        critical_capacity=1,
        monotonic_clock=IncrementingClock(),
    )
    scheduler = RuntimeScheduler(
        ingress=ingress,
        jobs=(
            ScheduledJobDefinition(
                "heartbeat",
                ScheduledTask.HEARTBEAT,
                IntervalSchedule(Decimal("5")),
            ),
        ),
        backend=backend,
    )
    starter = Thread(target=scheduler.start)
    starter.start()
    assert backend.entered.wait(timeout=2)

    with pytest.raises(RuntimeError, match="SCHEDULER_START_IN_PROGRESS"):
        scheduler.stop()
    backend.release.set()
    starter.join(timeout=2)
    assert not starter.is_alive()
    scheduler.stop()
