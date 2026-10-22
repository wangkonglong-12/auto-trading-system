"""Nonblocking V2 J1-to-SDK state machine, driven by the sole decision owner."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import get_ident
from time import monotonic

from auto_trading_system.execution.dispatch_boundary import (
    DispatchOutcome,
    DispatchPermission,
    SDKDispatcher,
)
from auto_trading_system.journal.critical_models import CommitEvidence, Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest


@dataclass(slots=True)
class _Pending:
    submission: Submission
    persist_deadline: float
    dispatch_deadline: float
    commit: Future[CommitEvidence]
    dispatch: Future[DispatchOutcome] | None = None
    status: str = "PERSISTING"


@dataclass(frozen=True, slots=True)
class SubmissionStatus:
    client_order_id: str
    status: str


class SubmissionCoordinator:
    """Poll once per event-loop turn; Futures are read only after done() is true.

    The application must reserve money/shares before begin() and supply a fresh
    risk/window guard. Unknown or expired operations keep their reservations until
    reconciliation. A timed-out Future is never cancelled or revived for dispatch.
    """

    def __init__(
        self,
        journal: CriticalJournalOwner,
        dispatcher: SDKDispatcher,
        permission: DispatchPermission,
        guard: Callable[[Submission], bool],
        *,
        capacity: int = 2048,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._owner = get_ident()
        self._journal = journal
        self._dispatcher = dispatcher
        self._permission = permission
        self._guard = guard
        self._capacity = capacity
        self._pending: dict[str, _Pending] = {}

    def _assert_owner(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("submission state has a single decision owner")

    def _guard_allows(self, submission: Submission) -> bool:
        try:
            return self._guard(submission)
        except Exception:
            self._permission.halt("DISPATCH_GUARD_FAILED")
            return False

    def begin(
        self, submission: Submission, *, valid_until: float, wait_seconds: float = 0.25
    ) -> None:
        self._assert_owner()
        if submission.client_order_id in self._pending:
            raise ValueError("client_order_id has already been used in this session")
        permission = self._permission.snapshot()
        if (
            not self._journal.healthy
            or permission.halted
            or submission.permission_epoch != permission.epoch
            or valid_until <= monotonic()
            or not 0 < wait_seconds <= 0.25
            or not self._guard_allows(submission)
        ):
            raise ValueError("submission prerequisites are not satisfied")
        if len(self._pending) >= self._capacity:
            self._permission.halt("SUBMISSION_CAPACITY")
            raise ValueError("submission capacity reached; explicit session rollover required")
        deadline = min(monotonic() + wait_seconds, valid_until)
        self._pending[submission.client_order_id] = _Pending(
            submission,
            deadline,
            valid_until,
            self._journal.commit(submission, deadline=deadline),
        )

    def poll(self) -> tuple[SubmissionStatus, ...]:
        self._assert_owner()
        if not self._journal.healthy and not self._permission.snapshot().halted:
            self._permission.halt("CRITICAL_JOURNAL_FAILED")
        for pending in self._pending.values():
            if pending.status == "PERSISTING":
                self._poll_commit(pending)
            if pending.status == "DISPATCHING":
                assert pending.dispatch is not None
                if pending.dispatch.done():
                    pending.status = pending.dispatch.result().status
                elif monotonic() >= pending.dispatch_deadline:
                    pending.status = "UNCERTAIN"
                    self._permission.halt("SDK_DEADLINE_EXCEEDED")
        return self.snapshot()

    def _poll_commit(self, pending: _Pending) -> None:
        # Check the deadline BEFORE looking at the Future. A commit already done
        # but not observed in time is also too late to grant a send permit.
        if monotonic() >= pending.persist_deadline:
            pending.status = "PERSISTENCE_UNCERTAIN"
            self._permission.halt("J1_DEADLINE_EXCEEDED")
            return
        if not pending.commit.done():
            return
        try:
            evidence = pending.commit.result()
        except Exception:
            pending.status = "PERSISTENCE_FAILED"
            self._permission.halt("J1_FAILED")
            return
        expected_hash = digest(canonical_bytes(pending.submission.model_dump(mode="json")))
        if (
            evidence.client_order_id != pending.submission.client_order_id
            or evidence.payload_hash != expected_hash
        ):
            pending.status = "EVIDENCE_MISMATCH"
            self._permission.halt("J1_EVIDENCE_MISMATCH")
            return
        if not evidence.fresh:
            pending.status = "RECOVERY_REQUIRED"
            self._permission.halt("J1_ALREADY_EXISTS")
            return
        if not self._guard_allows(pending.submission) or not self._journal.healthy:
            pending.status = "NOT_DISPATCHED"
            return
        pending.dispatch = self._dispatcher.dispatch(
            pending.submission,
            deadline=pending.dispatch_deadline,
        )
        pending.status = "DISPATCHING"

    def snapshot(self) -> tuple[SubmissionStatus, ...]:
        self._assert_owner()
        return tuple(SubmissionStatus(key, item.status) for key, item in self._pending.items())
