"""Internal scheduling that publishes work into the sole decision ingress."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import partial
from threading import Lock
from typing import Literal, Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from auto_trading_system.runtime.decision_loop import (
    BoundedEventIngress,
    EventPriority,
    PublishOutcome,
    RuntimeEventSource,
)


class ScheduledTask(StrEnum):
    PLAN_SYNC = "PLAN_SYNC"
    COMMAND_SYNC = "COMMAND_SYNC"
    EVENT_UPLOAD = "EVENT_UPLOAD"
    HEARTBEAT = "HEARTBEAT"
    POSITION_SNAPSHOT = "POSITION_SNAPSHOT"
    ASSET_SNAPSHOT = "ASSET_SNAPSHOT"
    ACTIVE_ORDER_RECONCILIATION = "ACTIVE_ORDER_RECONCILIATION"
    POST_CLOSE_RECONCILIATION = "POST_CLOSE_RECONCILIATION"
    WAL_CHECKPOINT = "WAL_CHECKPOINT"
    JOURNAL_BACKUP = "JOURNAL_BACKUP"
    TICK_RETENTION = "TICK_RETENTION"


class SchedulerStatus(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class RuntimeSchedulerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class IntervalSchedule:
    seconds: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.seconds, Decimal):
            raise ValueError("seconds must be a Decimal")
        if not self.seconds.is_finite() or self.seconds <= 0 or self.seconds > Decimal("86400"):
            raise ValueError("seconds must be finite and within (0, 86400]")


@dataclass(frozen=True, slots=True)
class DailySchedule:
    hour: int
    minute: int
    day_of_week: Literal["mon-fri"] = "mon-fri"

    def __post_init__(self) -> None:
        if type(self.hour) is not int or not 0 <= self.hour <= 23:
            raise ValueError("hour must be an integer from 0 to 23")
        if type(self.minute) is not int or not 0 <= self.minute <= 59:
            raise ValueError("minute must be an integer from 0 to 59")
        if self.day_of_week != "mon-fri":
            raise ValueError("day_of_week must be mon-fri")


@dataclass(frozen=True, slots=True)
class ScheduledJobDefinition:
    job_id: str
    task: ScheduledTask
    schedule: IntervalSchedule | DailySchedule
    priority: EventPriority = EventPriority.NORMAL
    misfire_grace_seconds: int = 30

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", self.job_id) is None:
            raise ValueError("job_id is invalid")
        if not isinstance(self.task, ScheduledTask):
            raise ValueError("task must be a ScheduledTask")
        if not isinstance(self.schedule, IntervalSchedule | DailySchedule):
            raise ValueError("schedule type is invalid")
        if not isinstance(self.priority, EventPriority):
            raise ValueError("priority must be an EventPriority")
        if (
            type(self.misfire_grace_seconds) is not int
            or not 1 <= self.misfire_grace_seconds <= 3600
        ):
            raise ValueError("misfire_grace_seconds must be an integer from 1 to 3600")


@dataclass(frozen=True, slots=True)
class ScheduledRuntimeEvent:
    job_id: str
    task: ScheduledTask
    fire_sequence: int


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    status: SchedulerStatus
    configured_jobs: int
    fired: int
    accepted: int
    overflowed: int
    closed: int
    suppressed: int


class SchedulerBackend(Protocol):
    def add_interval_job(
        self,
        *,
        job_id: str,
        seconds: Decimal,
        callback: Callable[[], None],
        misfire_grace_seconds: int,
    ) -> None: ...

    def add_daily_job(
        self,
        *,
        job_id: str,
        hour: int,
        minute: int,
        day_of_week: str,
        callback: Callable[[], None],
        misfire_grace_seconds: int,
    ) -> None: ...

    def reschedule_interval(self, *, job_id: str, seconds: Decimal) -> None: ...

    def start(self) -> None: ...

    def shutdown(self, *, wait: bool) -> None: ...


class ApschedulerBackend:
    """APScheduler adapter fixed to the Shanghai timezone and one job instance."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(
            timezone="Asia/Shanghai",
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    def add_interval_job(
        self,
        *,
        job_id: str,
        seconds: Decimal,
        callback: Callable[[], None],
        misfire_grace_seconds: int,
    ) -> None:
        self._scheduler.add_job(
            callback,
            trigger=IntervalTrigger(seconds=float(seconds), timezone="Asia/Shanghai"),
            id=job_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=misfire_grace_seconds,
            replace_existing=False,
        )

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
        self._scheduler.add_job(
            callback,
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone="Asia/Shanghai",
            ),
            id=job_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=misfire_grace_seconds,
            replace_existing=False,
        )

    def reschedule_interval(self, *, job_id: str, seconds: Decimal) -> None:
        self._scheduler.reschedule_job(
            job_id,
            trigger=IntervalTrigger(seconds=float(seconds), timezone="Asia/Shanghai"),
        )

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self, *, wait: bool) -> None:
        self._scheduler.shutdown(wait=wait)


class RuntimeScheduler:
    """Own one internal scheduler; scheduled callbacks only publish typed events."""

    def __init__(
        self,
        *,
        ingress: BoundedEventIngress[object],
        jobs: Iterable[ScheduledJobDefinition],
        backend: SchedulerBackend | None = None,
    ) -> None:
        definitions = tuple(jobs)
        identifiers = [job.job_id for job in definitions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("scheduled job ids must be unique")
        if not definitions:
            raise ValueError("at least one scheduled job is required")
        self._ingress = ingress
        self._jobs = {job.job_id: job for job in definitions}
        self._backend = ApschedulerBackend() if backend is None else backend
        self._lock = Lock()
        self._status = SchedulerStatus.CREATED
        self._accepting = False
        self._fired = 0
        self._accepted = 0
        self._overflowed = 0
        self._closed = 0
        self._suppressed = 0

    @property
    def status(self) -> SchedulerStatus:
        with self._lock:
            return self._status

    def start(self) -> None:
        with self._lock:
            if self._status is not SchedulerStatus.CREATED:
                raise RuntimeSchedulerError(
                    "SCHEDULER_NOT_STARTABLE", f"cannot start from {self._status}"
                )
            self._status = SchedulerStatus.STARTING
        try:
            for job in self._jobs.values():
                callback = partial(self._fire, job)
                if isinstance(job.schedule, IntervalSchedule):
                    self._backend.add_interval_job(
                        job_id=job.job_id,
                        seconds=job.schedule.seconds,
                        callback=callback,
                        misfire_grace_seconds=job.misfire_grace_seconds,
                    )
                else:
                    self._backend.add_daily_job(
                        job_id=job.job_id,
                        hour=job.schedule.hour,
                        minute=job.schedule.minute,
                        day_of_week=job.schedule.day_of_week,
                        callback=callback,
                        misfire_grace_seconds=job.misfire_grace_seconds,
                    )
            self._backend.start()
        except Exception as exc:
            with self._lock:
                self._accepting = False
                self._status = SchedulerStatus.FAILED
            with suppress(Exception):
                self._backend.shutdown(wait=True)
            raise RuntimeSchedulerError(
                "SCHEDULER_START_FAILED", "scheduler failed to start"
            ) from exc
        with self._lock:
            self._accepting = True
            self._status = SchedulerStatus.RUNNING

    def stop(self) -> None:
        with self._lock:
            if self._status is SchedulerStatus.STOPPED:
                return
            if self._status is SchedulerStatus.CREATED:
                self._status = SchedulerStatus.STOPPED
                return
            if self._status is SchedulerStatus.STARTING:
                raise RuntimeSchedulerError(
                    "SCHEDULER_START_IN_PROGRESS", "start must finish before stop"
                )
            self._accepting = False
        try:
            self._backend.shutdown(wait=True)
        except Exception as exc:
            with self._lock:
                self._status = SchedulerStatus.FAILED
            raise RuntimeSchedulerError(
                "SCHEDULER_STOP_FAILED", "scheduler failed to stop"
            ) from exc
        with self._lock:
            self._status = SchedulerStatus.STOPPED

    def set_interval(self, job_id: str, seconds: Decimal) -> None:
        replacement = IntervalSchedule(seconds)
        with self._lock:
            if self._status is not SchedulerStatus.RUNNING:
                raise RuntimeSchedulerError(
                    "SCHEDULER_NOT_RUNNING", "interval can change only while running"
                )
            job = self._jobs.get(job_id)
            if job is None:
                raise RuntimeSchedulerError("SCHEDULED_JOB_UNKNOWN", "scheduled job does not exist")
            if not isinstance(job.schedule, IntervalSchedule):
                raise RuntimeSchedulerError(
                    "SCHEDULED_JOB_NOT_INTERVAL", "daily job cannot use an interval"
                )
        try:
            self._backend.reschedule_interval(job_id=job_id, seconds=replacement.seconds)
        except Exception as exc:
            raise RuntimeSchedulerError(
                "SCHEDULED_JOB_RESCHEDULE_FAILED", "interval update failed"
            ) from exc
        with self._lock:
            self._jobs[job_id] = ScheduledJobDefinition(
                job_id=job.job_id,
                task=job.task,
                schedule=replacement,
                priority=job.priority,
                misfire_grace_seconds=job.misfire_grace_seconds,
            )

    def snapshot(self) -> SchedulerSnapshot:
        with self._lock:
            return SchedulerSnapshot(
                status=self._status,
                configured_jobs=len(self._jobs),
                fired=self._fired,
                accepted=self._accepted,
                overflowed=self._overflowed,
                closed=self._closed,
                suppressed=self._suppressed,
            )

    def _fire(self, job: ScheduledJobDefinition) -> None:
        with self._lock:
            if not self._accepting:
                self._suppressed += 1
                return
            self._fired += 1
            sequence = self._fired
        outcome = self._ingress.publish(
            ScheduledRuntimeEvent(job.job_id, job.task, sequence),
            source=RuntimeEventSource.SCHEDULER,
            priority=job.priority,
        )
        with self._lock:
            if outcome is PublishOutcome.ACCEPTED:
                self._accepted += 1
            elif outcome is PublishOutcome.OVERFLOW:
                self._overflowed += 1
            else:
                self._closed += 1
