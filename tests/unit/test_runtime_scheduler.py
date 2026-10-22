from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Literal, cast

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import build_runtime_jobs
from auto_trading_system.config import RuntimeConfig
from auto_trading_system.runtime import (
    ApschedulerBackend,
    BoundedEventIngress,
    DailySchedule,
    EventPriority,
    IntervalSchedule,
    RuntimeEventEnvelope,
    RuntimeEventSource,
    RuntimeScheduler,
    RuntimeSchedulerError,
    ScheduledJobDefinition,
    ScheduledRuntimeEvent,
    ScheduledTask,
    SchedulerSnapshot,
    SchedulerStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass
class FakeSchedulerBackend:
    intervals: dict[str, tuple[Decimal, object, int]] = field(default_factory=dict)
    dailies: dict[str, tuple[int, int, str, object, int]] = field(default_factory=dict)
    rescheduled: list[tuple[str, Decimal]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    start_error: Exception | None = None
    shutdown_error: Exception | None = None
    reschedule_error: Exception | None = None
    fire_during_start: str | None = None

    def add_interval_job(
        self,
        *,
        job_id: str,
        seconds: Decimal,
        callback: object,
        misfire_grace_seconds: int,
    ) -> None:
        self.intervals[job_id] = (seconds, callback, misfire_grace_seconds)
        self.calls.append(f"add:{job_id}")

    def add_daily_job(
        self,
        *,
        job_id: str,
        hour: int,
        minute: int,
        day_of_week: str,
        callback: object,
        misfire_grace_seconds: int,
    ) -> None:
        self.dailies[job_id] = (
            hour,
            minute,
            day_of_week,
            callback,
            misfire_grace_seconds,
        )
        self.calls.append(f"add:{job_id}")

    def reschedule_interval(self, *, job_id: str, seconds: Decimal) -> None:
        if self.reschedule_error is not None:
            raise self.reschedule_error
        self.rescheduled.append((job_id, seconds))

    def start(self) -> None:
        self.calls.append("start")
        if self.fire_during_start is not None:
            self.fire(self.fire_during_start)
        if self.start_error is not None:
            raise self.start_error

    def shutdown(self, *, wait: bool) -> None:
        self.calls.append(f"shutdown:{wait}")
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def fire(self, job_id: str) -> None:
        if job_id in self.intervals:
            callback = self.intervals[job_id][1]
        else:
            callback = self.dailies[job_id][3]
        assert callable(callback)
        callback()


def interval_job(
    job_id: str = "plan-sync",
    *,
    task: ScheduledTask = ScheduledTask.PLAN_SYNC,
    priority: EventPriority = EventPriority.NORMAL,
) -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=job_id,
        task=task,
        schedule=IntervalSchedule(Decimal("2")),
        priority=priority,
    )


def daily_job() -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id="post-close-reconciliation",
        task=ScheduledTask.POST_CLOSE_RECONCILIATION,
        schedule=DailySchedule(15, 10),
        priority=EventPriority.CRITICAL,
        misfire_grace_seconds=1800,
    )


def make_scheduler(
    *,
    jobs: tuple[ScheduledJobDefinition, ...] | None = None,
    normal_capacity: int = 8,
    critical_capacity: int = 4,
    backend: FakeSchedulerBackend | None = None,
) -> tuple[
    RuntimeScheduler,
    BoundedEventIngress[object],
    FakeSchedulerBackend,
]:
    ingress = BoundedEventIngress[object](
        normal_capacity=normal_capacity,
        critical_capacity=critical_capacity,
        monotonic_clock=IncrementingClock(),
    )
    effective_backend = backend or FakeSchedulerBackend()
    scheduler = RuntimeScheduler(
        ingress=ingress,
        jobs=jobs or (interval_job(), daily_job()),
        backend=effective_backend,
    )
    return scheduler, ingress, effective_backend


@pytest.mark.parametrize("seconds", [Decimal("0"), Decimal("-1"), Decimal("86400.1")])
def test_interval_schedule_rejects_out_of_range_values(seconds: Decimal) -> None:
    with pytest.raises(ValueError, match="within"):
        IntervalSchedule(seconds)


def test_interval_schedule_requires_finite_decimal() -> None:
    with pytest.raises(ValueError, match="Decimal"):
        IntervalSchedule(cast(Decimal, 2))
    with pytest.raises(ValueError, match="finite"):
        IntervalSchedule(Decimal("NaN"))


@pytest.mark.parametrize(("hour", "minute"), [(-1, 0), (24, 0), (0, -1), (0, 60)])
def test_daily_schedule_rejects_invalid_time(hour: int, minute: int) -> None:
    with pytest.raises(ValueError):
        DailySchedule(hour, minute)


def test_daily_schedule_rejects_boolean_time() -> None:
    with pytest.raises(ValueError, match="hour"):
        DailySchedule(cast(int, True), 0)
    with pytest.raises(ValueError, match="minute"):
        DailySchedule(0, cast(int, False))
    with pytest.raises(ValueError, match="day_of_week"):
        DailySchedule(15, 10, cast(Literal["mon-fri"], "mon-sun"))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"job_id": "BAD"}, "job_id"),
        ({"task": "PLAN_SYNC"}, "ScheduledTask"),
        ({"schedule": "interval"}, "schedule"),
        ({"priority": "NORMAL"}, "EventPriority"),
        ({"misfire_grace_seconds": 0}, "misfire_grace_seconds"),
        ({"misfire_grace_seconds": True}, "misfire_grace_seconds"),
    ],
)
def test_job_definition_is_strict(overrides: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "job_id": "plan-sync",
        "task": ScheduledTask.PLAN_SYNC,
        "schedule": IntervalSchedule(Decimal("2")),
        "priority": EventPriority.NORMAL,
        "misfire_grace_seconds": 30,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        ScheduledJobDefinition(**values)  # type: ignore[arg-type]


def test_scheduler_requires_nonempty_unique_job_ids() -> None:
    ingress = BoundedEventIngress[object](
        normal_capacity=2,
        critical_capacity=1,
        monotonic_clock=IncrementingClock(),
    )
    with pytest.raises(ValueError, match="at least one"):
        RuntimeScheduler(ingress=ingress, jobs=(), backend=FakeSchedulerBackend())
    with pytest.raises(ValueError, match="unique"):
        RuntimeScheduler(
            ingress=ingress,
            jobs=(interval_job(), interval_job()),
            backend=FakeSchedulerBackend(),
        )


def test_start_registers_interval_and_daily_jobs_before_backend_start() -> None:
    scheduler, _, backend = make_scheduler()

    scheduler.start()

    assert scheduler.status is SchedulerStatus.RUNNING
    assert backend.calls == ["add:plan-sync", "add:post-close-reconciliation", "start"]
    assert backend.intervals["plan-sync"][0] == Decimal("2")
    assert backend.intervals["plan-sync"][2] == 30
    assert backend.dailies["post-close-reconciliation"][:3] == (15, 10, "mon-fri")
    assert backend.dailies["post-close-reconciliation"][4] == 1800


def test_scheduled_callback_publishes_typed_event_with_configured_priority() -> None:
    scheduler, ingress, backend = make_scheduler()
    scheduler.start()

    backend.fire("post-close-reconciliation")
    envelope = ingress.take_blocking()

    assert isinstance(envelope, RuntimeEventEnvelope)
    assert envelope.source is RuntimeEventSource.SCHEDULER
    assert envelope.priority is EventPriority.CRITICAL
    assert envelope.payload == ScheduledRuntimeEvent(
        "post-close-reconciliation",
        ScheduledTask.POST_CLOSE_RECONCILIATION,
        1,
    )
    assert scheduler.snapshot() == SchedulerSnapshot(
        status=SchedulerStatus.RUNNING,
        configured_jobs=2,
        fired=1,
        accepted=1,
        overflowed=0,
        closed=0,
        suppressed=0,
    )


def test_queue_overflow_and_closed_ingress_are_observable_without_blocking_callback() -> None:
    scheduler, ingress, backend = make_scheduler(normal_capacity=1)
    scheduler.start()

    backend.fire("plan-sync")
    backend.fire("plan-sync")
    ingress.close()
    backend.fire("plan-sync")

    snapshot = scheduler.snapshot()
    assert (snapshot.fired, snapshot.accepted, snapshot.overflowed, snapshot.closed) == (3, 1, 1, 1)


def test_callback_during_start_is_suppressed_until_backend_start_succeeds() -> None:
    backend = FakeSchedulerBackend(fire_during_start="plan-sync")
    scheduler, ingress, _ = make_scheduler(jobs=(interval_job(),), backend=backend)

    scheduler.start()

    assert ingress.depth().total == 0
    assert scheduler.snapshot().suppressed == 1


def test_stop_disables_publication_before_waiting_for_backend() -> None:
    scheduler, ingress, backend = make_scheduler()
    scheduler.start()
    scheduler.stop()

    backend.fire("plan-sync")

    assert scheduler.status is SchedulerStatus.STOPPED
    assert ingress.depth().total == 0
    assert backend.calls[-1] == "shutdown:True"
    assert scheduler.snapshot().suppressed == 1
    scheduler.stop()


def test_stop_before_start_is_idempotent_and_does_not_start_backend() -> None:
    scheduler, _, backend = make_scheduler()

    scheduler.stop()
    scheduler.stop()

    assert scheduler.status is SchedulerStatus.STOPPED
    assert backend.calls == []
    with pytest.raises(RuntimeSchedulerError, match="SCHEDULER_NOT_STARTABLE"):
        scheduler.start()


def test_backend_start_and_stop_failures_are_stable_and_fail_closed() -> None:
    start_backend = FakeSchedulerBackend(start_error=RuntimeError("secret start detail"))
    start_scheduler, ingress, _ = make_scheduler(backend=start_backend)
    with pytest.raises(RuntimeSchedulerError, match="SCHEDULER_START_FAILED") as start_error:
        start_scheduler.start()
    assert isinstance(start_error.value.__cause__, RuntimeError)
    assert start_scheduler.status is SchedulerStatus.FAILED
    assert start_backend.calls[-1] == "shutdown:True"
    start_backend.fire("plan-sync")
    assert ingress.depth().total == 0

    stop_backend = FakeSchedulerBackend(shutdown_error=RuntimeError("secret stop detail"))
    stop_scheduler, _, _ = make_scheduler(backend=stop_backend)
    stop_scheduler.start()
    with pytest.raises(RuntimeSchedulerError, match="SCHEDULER_STOP_FAILED"):
        stop_scheduler.stop()
    assert stop_scheduler.status is SchedulerStatus.FAILED


def test_interval_can_be_changed_only_for_running_interval_job() -> None:
    scheduler, _, backend = make_scheduler()
    with pytest.raises(RuntimeSchedulerError, match="SCHEDULER_NOT_RUNNING"):
        scheduler.set_interval("plan-sync", Decimal("5"))
    scheduler.start()

    scheduler.set_interval("plan-sync", Decimal("5"))

    assert backend.rescheduled == [("plan-sync", Decimal("5"))]
    with pytest.raises(RuntimeSchedulerError, match="SCHEDULED_JOB_UNKNOWN"):
        scheduler.set_interval("missing-job", Decimal("5"))
    with pytest.raises(RuntimeSchedulerError, match="SCHEDULED_JOB_NOT_INTERVAL"):
        scheduler.set_interval("post-close-reconciliation", Decimal("5"))
    with pytest.raises(ValueError, match="within"):
        scheduler.set_interval("plan-sync", Decimal("0"))


def test_reschedule_backend_failure_does_not_replace_job_definition() -> None:
    backend = FakeSchedulerBackend(reschedule_error=RuntimeError("backend detail"))
    scheduler, _, _ = make_scheduler(backend=backend)
    scheduler.start()

    with pytest.raises(RuntimeSchedulerError, match="SCHEDULED_JOB_RESCHEDULE_FAILED"):
        scheduler.set_interval("plan-sync", Decimal("5"))
    backend.reschedule_error = None
    scheduler.set_interval("plan-sync", Decimal("2"))
    assert backend.rescheduled == [("plan-sync", Decimal("2"))]


def test_parallel_callbacks_have_unique_sequences_and_bounded_ingress() -> None:
    scheduler, ingress, backend = make_scheduler(
        jobs=(interval_job(),), normal_capacity=100, critical_capacity=1
    )
    scheduler.start()
    threads = [Thread(target=backend.fire, args=("plan-sync",)) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    ingress.close()
    envelopes = [ingress.take_blocking() for _ in range(100)]
    events = [item.payload for item in envelopes if item is not None]

    assert all(isinstance(event, ScheduledRuntimeEvent) for event in events)
    assert sorted(
        event.fire_sequence for event in events if isinstance(event, ScheduledRuntimeEvent)
    ) == list(range(1, 101))
    assert scheduler.snapshot().accepted == 100


def test_default_runtime_jobs_cover_sync_reconciliation_and_post_close_maintenance() -> None:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    settings = RuntimeConfig.model_validate(copy.deepcopy(payload))

    idle_jobs = build_runtime_jobs(settings)
    active_jobs = build_runtime_jobs(settings, active_cadence=True)
    idle_by_id = {job.job_id: job for job in idle_jobs}
    active_by_id = {job.job_id: job for job in active_jobs}

    assert len(idle_jobs) == 11
    assert {job.task for job in idle_jobs} == set(ScheduledTask)
    assert idle_by_id["plan-sync"].schedule == IntervalSchedule(Decimal("30"))
    assert active_by_id["plan-sync"].schedule == IntervalSchedule(Decimal("2"))
    assert idle_by_id["heartbeat"].schedule == IntervalSchedule(Decimal("30"))
    assert active_by_id["heartbeat"].schedule == IntervalSchedule(Decimal("5"))
    assert idle_by_id["active-order-reconciliation"].priority is EventPriority.CRITICAL
    assert idle_by_id["post-close-reconciliation"].schedule == DailySchedule(15, 10)
    assert idle_by_id["wal-checkpoint"].schedule == DailySchedule(15, 20)
    assert idle_by_id["journal-backup"].schedule == DailySchedule(15, 30)
    assert idle_by_id["tick-retention"].schedule == DailySchedule(15, 40)


def test_default_job_builder_rejects_non_boolean_cadence() -> None:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    settings = RuntimeConfig.model_validate(payload)

    with pytest.raises(ValueError, match="active_cadence"):
        build_runtime_jobs(settings, active_cadence=cast(bool, 1))


def test_real_apscheduler_backend_starts_and_stops_on_current_asyncio_loop() -> None:
    async def scenario() -> None:
        ingress = BoundedEventIngress[object](
            normal_capacity=2,
            critical_capacity=1,
            monotonic_clock=IncrementingClock(),
        )
        scheduler = RuntimeScheduler(
            ingress=ingress,
            jobs=(
                ScheduledJobDefinition(
                    "real-backend-smoke",
                    ScheduledTask.HEARTBEAT,
                    IntervalSchedule(Decimal("3600")),
                ),
            ),
            backend=ApschedulerBackend(),
        )

        scheduler.start()
        await asyncio.sleep(0)
        scheduler.stop()
        await asyncio.sleep(0)

        assert scheduler.status is SchedulerStatus.STOPPED
        assert ingress.depth().total == 0

    asyncio.run(scenario())
