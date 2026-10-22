"""Package an engine-owned frozen day using the existing eight V2 datasets.

No broker or Journal access occurs here. Unknown valuation/targets stay explicit
dataset gaps. Receipt APPLIED proves transport application, never settlement.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, TypedDict

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import DATASETS, DailyRecord, Dataset, DatasetStatus
from auto_trading_system.protocol.v2.records import (
    AssetRecord,
    IncidentRecord,
    PlanResultRecord,
    PositionRecord,
    ReconciliationRecord,
)
from auto_trading_system.runtime.day_snapshot import DaySnapshot
from auto_trading_system.runtime.recovery_v2 import recovery_evidence
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_export import (
    daily_record,
    export_order,
    export_submission,
    export_trade,
)
from auto_trading_system.sync.daily_files import DailyIdentity, FrozenBatch, freeze_daily


class _Scope(TypedDict):
    account_id: str
    trade_date: date
    observed_at: datetime


def freeze_engine_day(
    snapshot: DaySnapshot,
    files: ArtifactFiles,
    *,
    node_id: str,
    node_generation: int,
    journal_generation: str,
    generated_at: datetime,
    revision: int = 1,
    supersedes_batch_id: str | None = None,
) -> FrozenBatch:
    """Stable batch identity survives retry; changed evidence requires a new revision."""
    report, broker = snapshot.report, snapshot.broker
    evidence = recovery_evidence(snapshot.submissions, broker, opening_hash=snapshot.opening_hash)
    if (
        digest(canonical_bytes(evidence)) != report.evidence_hash
        or broker.account.account_id != report.account_id
        or report.submission_count != len(snapshot.submissions)
        or snapshot.journal_high_watermark < report.submission_high_watermark
        or generated_at.utcoffset() is None
        or generated_at < report.observed_at
    ):
        raise ValueError("daily snapshot evidence, scope or generation time mismatch")
    records: dict[Dataset, tuple[DailyRecord, ...]] = dict.fromkeys(DATASETS, ())
    gaps: dict[Dataset, str] = {}
    common: _Scope = {
        "account_id": report.account_id,
        "trade_date": report.trade_date,
        "observed_at": report.observed_at,
    }
    originals = {
        r.submission.client_order_id: r.submission
        for r in snapshot.submissions
        if r.submission.action_type == "NEW"
    }
    records["submission_records"] = tuple(
        export_submission(
            item,
            observed_at=report.observed_at,
            status="RESOLVED" if report.status == "CLEAR" else "UNCERTAIN",
        )
        for item in snapshot.submissions
    )
    records["broker_orders"] = tuple(
        export_order(order, **common)
        for order in broker.orders
        if order.broker_order_id is not None
    )
    if any(order.broker_order_id is None for order in broker.orders):
        gaps["broker_orders"] = "BROKER_ID_UNAVAILABLE"
    records["broker_trades"] = tuple(
        export_trade(
            trade,
            **common,
            origin="STRATEGY"
            if (report.status == "CLEAR" and trade.client_order_id in originals)
            else "UNKNOWN",
        )
        for trade in broker.trades
    )
    if report.status != "CLEAR":
        for dataset in ("submission_records", "broker_orders", "broker_trades", "positions"):
            gaps[dataset] = "RECOVERY_BLOCKED"
    elif not broker.stable or not broker.account.coherent:
        raise ValueError("clear daily snapshot is not coherent")

    positions: list[DailyRecord] = []
    if report.status == "CLEAR":
        for position in broker.account.positions:
            if position.market_value is None or position.frozen is None:
                gaps["positions"] = "BROKER_POSITION_VALUATION_UNAVAILABLE"
                continue
            strategy_quantity = sum(
                lot.remaining for lot in snapshot.lots if lot.symbol == position.symbol
            )
            if strategy_quantity != position.quantity:
                raise ValueError("frozen lot quantities differ from broker holdings")
            positions.append(
                daily_record(
                    position.symbol,
                    PositionRecord(
                        **common,
                        snapshot_id=broker.account.snapshot_id,
                        symbol=position.symbol,
                        total_quantity=position.quantity,
                        available_quantity=position.sellable,
                        frozen_quantity=position.frozen,
                        strategy_quantity=strategy_quantity,
                        market_value=position.market_value,
                        cost_amount=position.cost_amount,
                        attribution_status="MATCHED",
                    ),
                )
            )
    records["positions"] = tuple(positions)
    account = broker.account
    if account.cash_frozen is None or account.market_value is None or account.total_equity is None:
        gaps["assets"] = "BROKER_ASSET_FIELDS_UNAVAILABLE"
    else:
        records["assets"] = (
            daily_record(
                account.snapshot_id,
                AssetRecord(
                    **common,
                    snapshot_id=account.snapshot_id,
                    cash_available=account.cash,
                    cash_frozen=account.cash_frozen,
                    market_value=account.market_value,
                    total_equity=account.total_equity,
                    quality="CONFIRMED" if account.coherent and broker.stable else "INDETERMINATE",
                ),
            ),
        )
    plan = snapshot.plan
    results: list[DailyRecord] = []
    if plan is None:
        gaps["plan_results"] = "EXIT_RESULTS_CONTRACT_PENDING"
    elif (plan.payload.account_id, plan.payload.trade_date) != (
        report.account_id,
        report.trade_date,
    ):
        raise ValueError("daily plan scope mismatch")
    else:
        durable_targets = {t.allocation_id: t for t in snapshot.entry_targets}
        if len(durable_targets) != len(snapshot.entry_targets):
            raise ValueError("duplicate durable entry targets")
        entry_policy = next(p for p in plan.payload.policy_bundle if p.component == "entry")
        for persisted in snapshot.entry_targets:
            allocation = next(
                (a for a in plan.payload.allocations if a.allocation_id == persisted.allocation_id),
                None,
            )
            if (
                allocation is None
                or persisted.account_id != report.account_id
                or persisted.trade_date != report.trade_date
                or persisted.symbol != allocation.symbol
                or persisted.plan_hash != plan.envelope.payload_hash
                or persisted.policy_hash != entry_policy.payload_hash
            ):
                raise ValueError("durable entry target scope differs from daily plan")
        for allocation in plan.payload.allocations:
            children = [
                item
                for item in originals.values()
                if (
                    item.side == "BUY"
                    and item.symbol == allocation.symbol
                    and item.plan_hash == plan.envelope.payload_hash
                )
            ]
            targets = {
                item.entry_binding.target_quantity for item in children if item.entry_binding
            }
            decision = durable_targets.get(allocation.allocation_id)
            if decision is not None:
                targets.add(decision.target_quantity)
            if (
                len(targets) != 1
                or report.status != "CLEAR"
                or any(
                    item.entry_binding is None
                    or item.entry_binding.plan_id != plan.payload.plan_id
                    or item.entry_binding.allocation_id != allocation.allocation_id
                    or (
                        decision is not None
                        and item.entry_binding.state_hash != decision.payload_hash
                    )
                    or (decision is None and item.entry_binding.state_hash is not None)
                    for item in children
                )
            ):
                # max_qty is a ceiling, not the execution target. Never substitute it.
                gaps["plan_results"] = "ENTRY_TARGET_EVIDENCE_UNAVAILABLE"
                continue
            target = targets.pop()
            if target > allocation.max_qty:
                raise ValueError("entry target exceeds plan allocation ceiling")
            ids = {item.client_order_id for item in children}
            filled = sum(t.quantity.value for t in broker.trades if t.client_order_id in ids)
            if filled > target:
                raise ValueError("entry fills exceed frozen target")
            status: Literal["COMPLETED", "PARTIAL", "CANCELLED", "SKIPPED"] = (
                "SKIPPED"
                if target == 0
                else "COMPLETED"
                if filled == target
                else "PARTIAL"
                if filled
                else "CANCELLED"
            )
            results.append(
                daily_record(
                    allocation.allocation_id,
                    PlanResultRecord(
                        **common,
                        plan_id=plan.payload.plan_id,
                        plan_hash=plan.envelope.payload_hash,
                        allocation_id=allocation.allocation_id,
                        symbol=allocation.symbol,
                        target_quantity=target,
                        filled_quantity=filled,
                        status=status,
                        reason_code=decision.reason
                        if decision and target == 0
                        else "FROZEN_ENTRY_TARGET_RECONCILED",
                    ),
                )
            )
    records["plan_results"] = tuple(results)
    # 数量核对与正式费用/现金结算分开；CLEAR 恢复不能冒充财务已结算。
    records["reconciliations"] = (
        daily_record(
            "execution",
            ReconciliationRecord(
                **common,
                reconciliation_id=report.report_id,
                scope="DAILY",
                status="MATCHED" if report.status == "CLEAR" else "DISCREPANCY",
                reason_code="EXECUTION_QUANTITIES_ONLY"
                if report.status == "CLEAR"
                else "RECOVERY_BLOCKED",
                related_ids=(report.report_id,),
            ),
        ),
        daily_record(
            "settlement",
            ReconciliationRecord(
                **common,
                reconciliation_id="settlement-pending",
                scope="CASH",
                status="INDETERMINATE",
                reason_code="BROKER_STATEMENT_PENDING",
            ),
        ),
    )
    incidents = set(report.issue_codes) | set(gaps.values())
    records["incidents"] = tuple(
        daily_record(
            code,
            IncidentRecord(
                **common,
                incident_id=code,
                severity="WARNING",
                status="OPEN",
                code=code,
                summary="日终证据待补齐: " + code,
            ),
        )
        for code in sorted(incidents)
    )
    statuses = tuple(
        DatasetStatus(
            dataset=dataset,
            status=("INCOMPLETE" if records[dataset] else "UNAVAILABLE")
            if dataset in gaps
            else "COMPLETE",
            record_count=len(records[dataset]),
            reason=gaps.get(dataset, ""),
        )
        for dataset in DATASETS
    )
    # ID includes actual content, source Journal generation and revision, not wall time.
    identity_hash = digest(
        canonical_bytes(
            {
                "node": node_id,
                "generation": node_generation,
                "journal": journal_generation,
                "journal_high_watermark": snapshot.journal_high_watermark,
                "account": report.account_id,
                "day": report.trade_date.isoformat(),
                "revision": revision,
                "supersedes": supersedes_batch_id,
                "cutoff": report.observed_at.isoformat(),
                "records": {
                    key: [item.model_dump(mode="json") for item in value]
                    for key, value in records.items()
                },
                "statuses": [status.model_dump(mode="json") for status in statuses],
            }
        )
    )
    identity = DailyIdentity(
        batch_id="day-" + identity_hash[7:],
        node_id=node_id,
        node_generation=node_generation,
        account_id=report.account_id,
        trade_date=report.trade_date,
        revision=revision,
        supersedes_batch_id=supersedes_batch_id,
        # Freeze once at source cutoff; upload/retry time is tracked in JobStore.
        generated_at=report.observed_at,
        data_as_of=report.observed_at,
        journal_generation=journal_generation,
        last_record_seq=snapshot.journal_high_watermark,
    )
    return freeze_daily(identity, records, statuses, files)
