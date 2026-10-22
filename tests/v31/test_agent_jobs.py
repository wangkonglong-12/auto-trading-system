from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v2_files_sync import sample_batch

from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope, sign_plan
from auto_trading_system.runtime.single_instance import WindowsNamedMutexBackend
from auto_trading_system.sync.agent_jobs import AgentJobs
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_upload import PermanentSyncError, RetryableSyncError
from auto_trading_system.sync.job_store import JobStore, SyncJob
from auto_trading_system.testing.daily_receiver import InMemoryDailyReceiver
from auto_trading_system.testing.v31_scenarios import example_plan

NOW = datetime.fromisoformat("2026-09-07T14:41:00+08:00")


class Remote(InMemoryDailyReceiver):
    def __init__(self, plan: PlanEnvelope) -> None:
        super().__init__()
        self.fixed = plan
        self.lookup: PlanLookup | None = PlanLookup(
            account_id=plan.payload.account_id,
            strategy_id=plan.payload.strategy_id,
            trade_date=plan.payload.trade_date,
            plan_id=plan.payload.plan_id,
            state="PUBLISHED",
            state_version=1,
            etag="sha256:" + "1" * 64,
            plan=plan,
        )
        self.acks: dict[str, PlanReceipt] = {}
        self.failure: Exception | None = None

    def current_plan(
        self, account_id: str, strategy_id: str, trade_date: date, etag: str | None = None
    ) -> PlanLookup | None:
        return self.lookup

    def plan(self, plan_id: str) -> PlanEnvelope:
        assert plan_id == self.fixed.payload.plan_id
        return self.fixed

    def plan_receipt(self, receipt: PlanReceipt) -> None:
        if self.failure:
            raise self.failure
        assert self.acks.setdefault(receipt.receipt_id, receipt) == receipt


def system(tmp_path: Path) -> tuple[AgentJobs, EnginePlanControl, Remote, list[bool]]:
    files = ArtifactFiles(tmp_path / "artifacts")
    key = Ed25519PrivateKey.generate()
    plan = sign_plan(example_plan(), "plan-key", key)
    remote = Remote(plan)
    pauses: list[bool] = []
    engine = EnginePlanControl(
        files,
        {"plan-key": key.public_key()},
        node_id="simulation-node",
        account_id="simulation-account",
        node_generation=1,
        engine_boot_id="boot-1",
        policies=plan.payload.policy_bundle,
        rulebook=plan.payload.rulebook_version,
        clock=lambda: NOW,
        pause_for_change=lambda: pauses.append(True),
    )
    store = JobStore(
        tmp_path / "jobs",
        node_id="simulation-node",
        account_id="simulation-account",
        backend=WindowsNamedMutexBackend(),
    )
    agent = AgentJobs(
        files,
        store,
        remote,
        engine,
        node_id="simulation-node",
        account_id="simulation-account",
        generation=1,
        strategy_id=plan.payload.strategy_id,
        trusted_keys={"plan-key": key.public_key()},
        clock=lambda: NOW,
        is_trading_day=lambda day: day == NOW.date(),
    )
    return agent, engine, remote, pauses


def test_plan_job_persists_engine_receipt_and_resumes_after_network_loss(tmp_path: Path) -> None:
    agent, engine, remote, _ = system(tmp_path)
    with pytest.raises(RuntimeError, match="ownership"):
        agent.pull_plan()
    assert engine.plans.loaded() is None
    with agent.store:
        remote.failure = RetryableSyncError("offline")
        first = agent.pull_plan()
        assert first and first.state == "PENDING"
        assert engine.plans.loaded() == remote.fixed
        remote.failure = None
        result = agent.retry_retained()
        assert result[0].state == "SYNCED" and len(remote.acks) == 1
        assert agent.pull_plan() == result[0]
        assert agent.retry_retained() == ()
    with agent.store:
        assert agent.store.list()[0].state == "SYNCED"


@pytest.mark.parametrize("armed", [False, True])
def test_withdrawal_clears_candidate_or_pauses_frozen_plan(tmp_path: Path, armed: bool) -> None:
    agent, engine, remote, pauses = system(tmp_path)
    with agent.store:
        agent.pull_plan()
        if armed:
            engine.mark_armed()
        assert remote.lookup
        remote.lookup = remote.lookup.model_copy(
            update={
                "state": "WITHDRAWN",
                "state_version": 2,
                "etag": "sha256:" + "2" * 64,
                "plan": None,
            }
        )
        job = agent.pull_plan()
        assert job and job.state == "SYNCED"
        receipt = remote.acks[job.job_id]
        assert receipt.status == ("CHANGE_REQUIRES_PAUSE" if armed else "WITHDRAWN")
        assert pauses == ([True] if armed else [])
        assert (engine.plans.loaded() is not None) == armed
        assert agent.pull_plan() == job
        assert pauses == ([True] if armed else [])
        if armed:
            with pytest.raises(ValueError, match="armed"):
                engine.plans.clear_unarmed()


def test_daily_revisions_and_successful_receipts_rechecked_after_remote_restore(
    tmp_path: Path,
) -> None:
    agent, _, remote, _ = system(tmp_path)
    with agent.store:
        first = sample_batch(agent.files, count=1)
        second = sample_batch(agent.files, count=2, revision=2)
        one = agent.register_daily(first)
        two = agent.register_daily(second)
        assert agent.register_daily(first) == one
        assert [item.state for item in agent.retry_retained()] == ["SYNCED", "SYNCED"]
        assert remote.receipt(two.job_id) is not None
        fresh = Remote(remote.fixed)
        agent.remote = fresh
        assert [item.state for item in agent.retry_retained()] == ["SYNCED", "SYNCED"]
        assert fresh.receipt(two.job_id) is not None
        with pytest.raises(ValueError, match="budget"):
            agent.retry_retained(maximum_jobs=257)


def test_agent_job_index_ownership_capacity_identity_and_stale_updates(tmp_path: Path) -> None:
    agent, _, _, _ = system(tmp_path)
    store = agent.store
    with store:
        with pytest.raises(RuntimeError, match="already"):
            store.__enter__()
        contender = JobStore(
            tmp_path / "other-jobs",
            node_id="simulation-node",
            account_id="simulation-account",
            backend=WindowsNamedMutexBackend(),
        )
        with pytest.raises(RuntimeError):
            contender.__enter__()
        job = SyncJob(
            job_id="one", kind="DAILY", artifact_hash="sha256:" + "1" * 64, updated_at=NOW
        )
        store.register(job)
        with pytest.raises(ValueError, match="conflicts"):
            store.register(job.model_copy(update={"artifact_hash": "sha256:" + "2" * 64}))
        with pytest.raises(ValueError, match="verified receipt"):
            store.result(job, state="SYNCED", now=NOW, reason="TEST")
        store.result(job, state="PENDING", now=NOW, reason="TEST")
        with pytest.raises(ValueError, match="stale"):
            store.result(job, state="PENDING", now=NOW, reason="TEST")
    different = JobStore(
        tmp_path / "jobs",
        node_id="different-node",
        account_id="simulation-account",
        backend=WindowsNamedMutexBackend(),
    )
    with pytest.raises(ValueError, match="different node"):
        different.__enter__()
    # The failed scope check released its own mutex.
    with pytest.raises(ValueError, match="different node"):
        different.__enter__()
    limited = JobStore(
        tmp_path / "limited",
        node_id="node-capacity",
        account_id="test-account",
        backend=WindowsNamedMutexBackend(),
        maximum_jobs=1,
    )
    with limited:
        limited.register(job)
        with pytest.raises(ValueError, match="full"):
            limited.register(job.model_copy(update={"job_id": "two"}))


def test_corrupt_index_is_not_treated_as_empty_or_success(tmp_path: Path) -> None:
    agent, _, _, _ = system(tmp_path)
    with agent.store:
        job = agent.pull_plan()
        assert job
        path = agent.store.root / (digest(job.job_id.encode())[7:] + ".json")
        path.write_bytes(b"{}")
        with pytest.raises(ValueError, match="envelope"):
            agent.store.list()
        path.write_bytes(canonical_bytes({"body": job.model_dump(mode="json"), "hash": "wrong"}))
        with pytest.raises(ValueError, match="checksum"):
            agent.store.list()


def test_clock_windows_errors_and_owner_thread(tmp_path: Path) -> None:
    agent, engine, remote, _ = system(tmp_path)
    with agent.store:
        agent.clock = lambda: NOW + timedelta(days=1)
        assert agent.pull_plan() is None
        agent.clock = lambda: NOW.replace(hour=13)
        assert agent.pull_plan() is None
        agent.clock = lambda: NOW.replace(hour=14, minute=29, second=59)
        assert agent.pull_plan() is None
        agent.clock = lambda: NOW
        remote.failure = PermanentSyncError("rejected")
        job = agent.pull_plan()
        assert job and job.state == "NEEDS_ATTENTION"
        assert agent.run_job(job) == job
        assert remote.lookup
        with (
            ThreadPoolExecutor(max_workers=1) as worker,
            pytest.raises(RuntimeError, match="owner thread"),
        ):
            worker.submit(engine.apply, remote.lookup, None).result()
        wrong = remote.lookup.model_copy(update={"account_id": "wrong"})
        with pytest.raises(ValueError, match="another account"):
            engine.apply(wrong, None)
        conflict = remote.lookup.model_copy(update={"etag": "sha256:" + "f" * 64})
        with pytest.raises(ValueError, match="conflicting"):
            engine.apply(conflict, None)
        old = remote.lookup.model_copy(update={"state_version": 0})
        with pytest.raises(ValueError, match="backwards"):
            engine.apply(old, None)


def test_failed_permission_reduction_never_acknowledges_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, engine, remote, _ = system(tmp_path)

    def fail_pause() -> None:
        raise ValueError("injected permission fault")

    with agent.store:
        agent.pull_plan()
        engine.mark_armed()
        assert remote.lookup
        remote.lookup = remote.lookup.model_copy(
            update={
                "state": "WITHDRAWN",
                "state_version": 2,
                "etag": "sha256:" + "2" * 64,
                "plan": None,
            }
        )
        monkeypatch.setattr(engine, "_pause", fail_pause)
        with pytest.raises(RuntimeError, match="permission reduction"):
            agent.pull_plan()
        assert len(remote.acks) == 1
        assert len(agent.store.list()) == 1
        monkeypatch.setattr(engine, "_pause", lambda: None)
        job = agent.pull_plan()
        assert job and remote.acks[job.job_id].status == "CHANGE_REQUIRES_PAUSE"


def test_failed_job_result_write_propagates_once_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, remote, _ = system(tmp_path)
    calls = 0

    def fail_result(*args: object, **kwargs: object) -> SyncJob:
        nonlocal calls
        calls += 1
        raise OSError("injected result disk fault")

    with agent.store:
        with monkeypatch.context() as patch:
            patch.setattr(agent.store, "result", fail_result)
            with pytest.raises(OSError, match="result disk fault"):
                agent.pull_plan()
        assert calls == 1 and len(remote.acks) == 1
        assert agent.store.list()[0].state == "PENDING"
        assert agent.retry_retained()[0].state == "SYNCED"
        assert len(remote.acks) == 1


def test_withdrawal_must_reference_same_day_strategy_and_verified_hash(tmp_path: Path) -> None:
    agent, engine, remote, _ = system(tmp_path)
    with agent.store:
        agent.pull_plan()
        assert remote.lookup
        withdrawal = remote.lookup.model_copy(
            update={
                "state": "WITHDRAWN",
                "state_version": 2,
                "etag": "sha256:" + "2" * 64,
                "plan": None,
            }
        )
        artifact_hash = agent.files.put(canonical_bytes(remote.fixed.model_dump(mode="json")))
        wrong = withdrawal.model_copy(update={"strategy_id": "other-strategy"})
        with pytest.raises(ValueError, match="identity differs"):
            engine.apply(wrong, artifact_hash)

        class WrongReceipt:
            def apply(self, lookup: PlanLookup, artifact_hash: str | None) -> PlanReceipt:
                receipt = engine.apply(lookup, artifact_hash)
                return receipt.model_copy(update={"payload_hash": "sha256:" + "f" * 64})

        remote.lookup = withdrawal
        agent.engine = WrongReceipt()
        with pytest.raises(PermanentSyncError, match="PLAN_HASH_MISMATCH"):
            agent.pull_plan()
        assert len(remote.acks) == 1
