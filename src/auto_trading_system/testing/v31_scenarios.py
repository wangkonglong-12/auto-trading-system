"""Public deterministic fixtures, deliberately unrelated to real accounts or keys."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.protocol.v2.plans import (
    Allocation,
    ExecutionWindow,
    OfflineAuthorization,
    PlanPayload,
    PolicyVersion,
    RiskBudget,
    SourceRevision,
)


@dataclass(frozen=True)
class ScenarioClock:
    """A fixed business timestamp; real monotonic deadlines still use monotonic()."""

    instant: datetime

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp.from_instant(self.instant)


def example_plan() -> PlanPayload:
    now = datetime.fromisoformat("2026-09-07T14:40:00+08:00")
    return PlanPayload(
        plan_id="simulation-plan-1",
        account_id="simulation-account",
        strategy_id="tail-entry",
        trade_date=now.date(),
        source=SourceRevision(
            source_run_id="simulation-source-1",
            source_revision=1,
            source_hash="sha256:" + "a" * 64,
            algorithm_version="simulation-v1",
            completed_at=now,
            market_data_cutoff=now,
            scheduled_at=now,
            published_at=now,
            market_coverage_bps=10000,
            run_status="SUCCEEDED",
            data_completeness="FULL",
            publication_status="FINAL",
            finalized=True,
        ),
        execution_window=ExecutionWindow(
            entry_not_before=now,
            stop_new_buy_at=now.replace(hour=14, minute=55, second=30),
            cancel_by=now.replace(hour=14, minute=56, second=30),
            min_execution_seconds=30,
        ),
        allocations=(
            Allocation(
                allocation_id="simulation-allocation-1",
                symbol="000001.SZ",
                rank=1,
                target_value="1000",
                max_value="1005",
                max_qty=100,
            ),
        ),
        policy_bundle=(
            PolicyVersion(
                component="entry", version="simulation-v1", payload_hash="sha256:" + "b" * 64
            ),
            PolicyVersion(
                component="exit", version="simulation-v1", payload_hash="sha256:" + "b" * 64
            ),
            PolicyVersion(
                component="execution", version="simulation-v1", payload_hash="sha256:" + "b" * 64
            ),
            PolicyVersion(
                component="risk", version="simulation-v1", payload_hash="sha256:" + "b" * 64
            ),
        ),
        risk_budget=RiskBudget(max_buy_value="1005", budget_basis_as_of=now),
        offline_authorization=OfflineAuthorization(
            directions=("BUY", "SELL"),
            valid_until=now.replace(hour=15, minute=0),
        ),
        rulebook_version="simulation-v1",
        target_node_generation=1,
    )
