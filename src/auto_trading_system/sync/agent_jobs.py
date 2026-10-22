"""Plan and daily agent jobs; engine control and network synchronization are separate.

The caller holds JobStore's account/node lease for the complete invocation.
Task Scheduler may repeat an invocation: stable job IDs and immutable bodies
make repetition safe. No broker call or critical SQLite connection occurs here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import PlanEnvelope, check_plan_binding, verify_plan
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_files import FrozenBatch, check_daily_successor, load_daily
from auto_trading_system.sync.daily_upload import (
    DailyTransport,
    PermanentSyncError,
    RetryableSyncError,
    upload_once,
)
from auto_trading_system.sync.job_store import JobStore, SyncJob

CN = timezone(timedelta(hours=8))


class QuantAgentTransport(DailyTransport, Protocol):
    def current_plan(
        self, account_id: str, strategy_id: str, trade_date: date, etag: str | None = None
    ) -> PlanLookup | None: ...
    def plan_receipt(self, receipt: PlanReceipt) -> None: ...
    def plan(self, plan_id: str) -> PlanEnvelope: ...


class PlanControlPort(Protocol):
    def apply(self, lookup: PlanLookup, artifact_hash: str | None) -> PlanReceipt: ...


class AgentJobs:
    def __init__(
        self,
        files: ArtifactFiles,
        store: JobStore,
        remote: QuantAgentTransport,
        engine: PlanControlPort,
        *,
        node_id: str,
        account_id: str,
        generation: int,
        strategy_id: str,
        trusted_keys: Mapping[str, Ed25519PublicKey],
        clock: Callable[[], datetime],
        is_trading_day: Callable[[date], bool],
    ) -> None:
        self.files, self.store, self.remote, self.engine = files, store, remote, engine
        self.node_id, self.account_id, self.generation = node_id, account_id, generation
        self.strategy_id = strategy_id
        self.keys = dict(trusted_keys)
        self.clock, self.is_trading_day = clock, is_trading_day

    def pull_plan(self) -> SyncJob | None:
        self.store.require_lease()
        if self.clock().tzinfo is None:
            raise ValueError("agent clock must be timezone aware")
        now = self.clock().astimezone(CN)
        if not self.is_trading_day(now.date()) or not time(14, 30) <= now.time() <= time(
            14, 55, 59
        ):
            return None
        lookup = self.remote.current_plan(self.account_id, self.strategy_id, now.date())
        if lookup is None:
            return None
        if (lookup.account_id, lookup.strategy_id, lookup.trade_date) != (
            self.account_id,
            self.strategy_id,
            now.date(),
        ):
            raise PermanentSyncError("PLAN_SCOPE_MISMATCH")
        fixed = lookup.plan if lookup.plan else self.remote.plan(lookup.plan_id)
        body = canonical_bytes(fixed.model_dump(mode="json"))
        verified = verify_plan(body, self.keys)
        if (
            verified.payload.plan_id,
            verified.payload.account_id,
            verified.payload.strategy_id,
            verified.payload.trade_date,
            verified.payload.target_node_generation,
        ) != (lookup.plan_id, self.account_id, self.strategy_id, now.date(), self.generation):
            raise PermanentSyncError("FIXED_PLAN_SCOPE_MISMATCH")
        if lookup.plan:
            check_plan_binding(
                verified.payload,
                account_id=self.account_id,
                node_generation=self.generation,
                now=now,
            )
        artifact_hash = self.files.put(body)
        receipt = self.engine.apply(lookup, artifact_hash)
        if (receipt.node_id, receipt.account_id, receipt.node_generation, receipt.plan_id) != (
            self.node_id,
            self.account_id,
            self.generation,
            lookup.plan_id,
        ):
            raise PermanentSyncError("ENGINE_RECEIPT_SCOPE_MISMATCH")
        if receipt.payload_hash != verified.envelope.payload_hash:
            raise PermanentSyncError("ENGINE_RECEIPT_PLAN_HASH_MISMATCH")
        receipt_hash = self.files.put(canonical_bytes(receipt.model_dump(mode="json")))
        job = self.store.register(
            SyncJob(
                job_id=receipt.receipt_id,
                kind="PLAN_RECEIPT",
                artifact_hash=receipt_hash,
                updated_at=self.clock(),
            )
        )
        return self.run_job(job)

    def register_daily(self, batch: FrozenBatch) -> SyncJob:
        self.store.require_lease()
        m = batch.manifest
        if (m.node_id, m.account_id, m.node_generation) != (
            self.node_id,
            self.account_id,
            self.generation,
        ):
            raise ValueError("daily batch scope differs from this agent")
        # Verify all files before making the batch discoverable to retry jobs.
        if load_daily(self.files, batch.artifact_hash) != batch:
            raise ValueError("daily artifact does not equal frozen export")
        return self.store.register(
            SyncJob(
                job_id=m.batch_id,
                kind="DAILY",
                artifact_hash=batch.artifact_hash,
                updated_at=self.clock(),
            )
        )

    def _predecessor_block(self, batch: FrozenBatch) -> str | None:
        predecessor = batch.manifest.supersedes_batch_id
        if predecessor is None:
            return None
        prior_job = self.store.get(predecessor)
        if prior_job is None or prior_job.kind != "DAILY":
            raise PermanentSyncError("DAILY_PREDECESSOR_MISSING")
        prior = load_daily(self.files, prior_job.artifact_hash)
        check_daily_successor(prior.manifest, batch.manifest)
        if prior_job.state == "NEEDS_ATTENTION":
            return "PREDECESSOR_ATTENTION"
        # Local SYNCED is not enough after a remote database restore. APPLIED
        # for this exact parent proves the receiver applied its preceding chain.
        receipt = self.remote.receipt(predecessor)
        if receipt is None:
            return "PREDECESSOR_PENDING"
        receipt.check_manifest(prior.manifest)
        return None

    def run_job(self, job: SyncJob) -> SyncJob:
        self.store.require_lease()
        if job.state == "NEEDS_ATTENTION":
            return job
        state: Literal["PENDING", "SYNCED", "NEEDS_ATTENTION", "LOCAL_STORAGE_ERROR"]
        receipt_hash: str | None = None
        try:
            if job.kind == "PLAN_RECEIPT":
                if job.state == "SYNCED":
                    return job
                receipt = PlanReceipt.model_validate_json(self.files.read(job.artifact_hash))
                if (receipt.node_id, receipt.account_id, receipt.node_generation) != (
                    self.node_id,
                    self.account_id,
                    self.generation,
                ):
                    raise PermanentSyncError("RETAINED_RECEIPT_SCOPE_MISMATCH")
                self.remote.plan_receipt(receipt)
                state, reason, receipt_hash = "SYNCED", "RECEIPT_ACCEPTED", job.artifact_hash
            else:
                batch = load_daily(self.files, job.artifact_hash)
                if (
                    batch.manifest.node_id,
                    batch.manifest.account_id,
                    batch.manifest.node_generation,
                ) != (self.node_id, self.account_id, self.generation):
                    raise PermanentSyncError("RETAINED_DAILY_SCOPE_MISMATCH")
                blocked = self._predecessor_block(batch)
                if blocked:
                    state, reason = "PENDING", blocked
                else:
                    outcome = upload_once(batch, self.files, self.remote)
                    if outcome.status not in (
                        "SYNCED",
                        "PENDING",
                        "NEEDS_ATTENTION",
                        "LOCAL_STORAGE_ERROR",
                    ):
                        raise ValueError("unknown upload outcome")
                    state = cast(
                        Literal["PENDING", "SYNCED", "NEEDS_ATTENTION", "LOCAL_STORAGE_ERROR"],
                        outcome.status,
                    )
                    reason, receipt_hash = outcome.status, outcome.receipt_artifact_hash
        except RetryableSyncError:
            state, reason = "PENDING", "NETWORK_RETRY"
        except (PermanentSyncError, ValueError):
            state, reason = "NEEDS_ATTENTION", "SYNC_VALIDATION"
        except OSError:
            state, reason = "LOCAL_STORAGE_ERROR", "ARTIFACT_IO"
        # Persist once, outside the transport/artifact error handler. If this
        # write fails, leave the prior job intact and propagate the storage
        # fault; a second write could hide a disk failure or stale-index error.
        return self.store.result(
            job, state=state, now=self.clock(), reason=reason, receipt_hash=receipt_hash
        )

    def retry_retained(self, *, maximum_jobs: int = 64) -> tuple[SyncJob, ...]:
        if not 1 <= maximum_jobs <= 256:
            raise ValueError("retry invocation exceeds job budget")
        # A durable round-robin cursor gives every job a turn even after reboot
        # or clock rollback. Recursing through ancestors would starve long chains.
        # Each correction instead validates its direct parent in run_job.
        eligible = [
            job
            for job in self.store.list()
            if job.state != "NEEDS_ATTENTION"
            and not (job.kind == "PLAN_RECEIPT" and job.state == "SYNCED")
        ]
        if maximum_jobs == 1:
            return self._retry_lane(eligible, 1, "all")
        pending = [job for job in eligible if job.state != "SYNCED"]
        synced = [job for job in eligible if job.state == "SYNCED"]
        # New/failed uploads get priority over years of successful history, while
        # one reserved audit slot continues detecting a restored remote database.
        pending_budget = min(len(pending), maximum_jobs - bool(synced))
        return self._retry_lane(pending, pending_budget, "pending") + self._retry_lane(
            synced, maximum_jobs - pending_budget, "synced"
        )

    def _retry_lane(
        self, jobs: list[SyncJob], budget: int, lane: Literal["all", "pending", "synced"]
    ) -> tuple[SyncJob, ...]:
        cursor = self.store.retry_cursor(lane)
        ordered = sorted(
            jobs, key=lambda job: (cursor is not None and job.job_id <= cursor, job.job_id)
        )
        results: list[SyncJob] = []
        for job in ordered[:budget]:
            result = self.run_job(job)
            # Advance only after result persistence. A lost cursor write merely
            # repeats the same idempotent job; it can never skip unfinished data.
            self.store.advance_retry_cursor(job.job_id, lane)
            results.append(result)
        return tuple(results)
