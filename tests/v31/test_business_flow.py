from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import get_ident
from time import monotonic, sleep

import pytest
from pydantic import ValidationError

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario, settle
from auto_trading_system.control.plan_delivery import PlanDeliveryMailbox
from auto_trading_system.gateways.broker.mock import SubmitOutcome, SubmitScenario
from auto_trading_system.journal.critical_models import EntryBinding, Submission
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.daily_sync import DailyRecord, Dataset
from auto_trading_system.runtime.day_snapshot import DaySnapshot
from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact, recovery_evidence
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_files import FrozenBatch
from auto_trading_system.sync.daily_upload import RetryableSyncError, upload_once
from auto_trading_system.sync.engine_daily import freeze_engine_day
from auto_trading_system.testing.daily_receiver import InMemoryDailyReceiver


def closed_day(tmp_path: Path, *, binding: bool = True, fill: bool = True) -> DaySnapshot:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path, clock=lambda: clock.instant) as runtime:
        engine = runtime.kernel
        engine.start_recovery()
        wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        if fill:
            runtime.confirmed_arm()
            order = runtime.buy()
            if binding:
                order = order.model_copy(
                    update={
                        "entry_binding": EntryBinding(
                            plan_id=runtime.plan.payload.plan_id,
                            allocation_id=runtime.plan.payload.allocations[0].allocation_id,
                            target_quantity=100,
                        )
                    }
                )
            runtime.broker.queue_submit(fill_scenario(100, full=True))
            settle(runtime, order)
        clock.instant = DEMO_NOW.replace(hour=15, minute=1)
        engine.start_recovery()
        wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        return engine.capture_day()


def freeze(
    snapshot: DaySnapshot, root: Path, *, revision: int = 1, previous: str | None = None
) -> tuple[FrozenBatch, ArtifactFiles]:
    files = ArtifactFiles(root)
    return freeze_engine_day(
        snapshot,
        files,
        node_id="simulation-node",
        node_generation=1,
        journal_generation="journal-1",
        generated_at=snapshot.report.observed_at + timedelta(minutes=29),
        revision=revision,
        supersedes_batch_id=previous,
    ), files


def rows(batch: FrozenBatch, files: ArtifactFiles, dataset: Dataset) -> tuple[DailyRecord, ...]:
    values: list[DailyRecord] = []
    for part in batch.manifest.parts:
        if part.dataset == dataset:
            raw = parse_bytes(files.read(part.part_hash))
            assert isinstance(raw, list)
            values.extend(DailyRecord.model_validate_json(canonical_bytes(value)) for value in raw)
    return tuple(values)


def rebind(snapshot: DaySnapshot) -> DaySnapshot:
    evidence = recovery_evidence(
        snapshot.submissions, snapshot.broker, opening_hash=snapshot.opening_hash
    )
    return replace(
        snapshot,
        report=snapshot.report.model_copy(
            update={"evidence_hash": digest(canonical_bytes(evidence))}
        ),
    )


def test_day_export_uses_actual_engine_facts_and_stable_artifacts(tmp_path: Path) -> None:
    snapshot = closed_day(tmp_path / "engine")
    batch, files = freeze(snapshot, tmp_path / "files")
    again = freeze_engine_day(
        snapshot,
        files,
        node_id="simulation-node",
        node_generation=1,
        journal_generation="journal-1",
        generated_at=snapshot.report.observed_at + timedelta(days=1),
    )
    assert again == batch
    assert all(status.status == "COMPLETE" for status in batch.manifest.dataset_status)
    assert batch.manifest.last_record_seq == snapshot.journal_high_watermark == 1
    assert rows(batch, files, "plan_results")[0].data["target_quantity"] == 100
    assert (
        rows(batch, files, "broker_trades")[0].data["client_order_id"]
        == snapshot.submissions[0].submission.client_order_id
    )
    assert rows(batch, files, "positions")[0].data["strategy_quantity"] == 100
    assert rows(batch, files, "assets")[0].data["cash_available"] == "9000.00"
    receiver = InMemoryDailyReceiver()
    assert upload_once(batch, files, receiver).status == "SYNCED"
    receipt = receiver.receipt(batch.manifest.batch_id)
    assert receipt and receipt.financial_status == "PROVISIONAL"
    assert receiver.records(
        snapshot.report.account_id, str(snapshot.report.trade_date), "broker_trades"
    ) == rows(batch, files, "broker_trades")
    assert receiver.records("other", str(snapshot.report.trade_date), "broker_trades") == ()


@pytest.mark.parametrize(
    "case",
    [
        "startup",
        "before-close",
        "armed",
        "expired",
        "next-day",
        "callback",
        "halt",
        "recovery-pending",
    ],
)
def test_capture_day_rejects_unstable_or_unfinished_window(tmp_path: Path, case: str) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path, clock=lambda: clock.instant) as runtime:
        engine = runtime.kernel
        if case != "startup":
            engine.start_recovery()
            wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
            if case == "armed":
                runtime.confirmed_arm()
            if case not in ("before-close", "armed"):
                clock.instant = DEMO_NOW.replace(hour=15, minute=1)
                engine.start_recovery()
                if case != "recovery-pending":
                    wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
            if case == "expired":
                clock.advance(31)
            elif case == "next-day":
                clock.advance(86400)
            elif case == "callback":
                # Event arrival invalidates the report even before callback persistence settles.
                engine.broker_event(object())  # type: ignore[arg-type]
            elif case == "halt":
                engine.emergency_halt()
        with pytest.raises(ValueError, match="daily export"):
            engine.capture_day()


def test_capture_day_has_one_owner_and_sealed_day_stays_exportable(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path, clock=lambda: clock.instant) as runtime:
        clock.instant = DEMO_NOW.replace(hour=15, minute=1)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        snapshot = runtime.kernel.capture_day()
        with ThreadPoolExecutor(1) as worker, pytest.raises(RuntimeError, match="single owner"):
            worker.submit(runtime.kernel.capture_day).result(2)
        runtime.kernel.seal_portfolio().result(2)
        assert runtime.kernel.capture_day() == snapshot


def test_blocked_recovery_can_upload_unresolved_facts_without_arming(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path / "engine", clock=lambda: clock.instant) as runtime:
        runtime.journal.commit(runtime.buy(), deadline=monotonic() + 2).result(3)
        clock.instant = DEMO_NOW.replace(hour=15, minute=1)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "RECOVERY_BLOCKED")
        snapshot = runtime.kernel.capture_day()
        assert runtime.kernel.snapshot().halted
    batch, files = freeze(snapshot, tmp_path / "files")
    assert rows(batch, files, "submission_records")[0].data["status"] == "UNCERTAIN"
    assert rows(batch, files, "incidents")
    receiver = InMemoryDailyReceiver()
    assert upload_once(batch, files, receiver).status == "SYNCED"
    receipt = receiver.receipt(batch.manifest.batch_id)
    assert receipt and receipt.business_status == "INCOMPLETE"


@pytest.mark.parametrize(
    "missing", ["valuation", "asset", "entry-target", "exit-plan", "empty-day"]
)
def test_missing_fields_are_explicit_and_zero_day_is_not_failed_query(
    tmp_path: Path, missing: str
) -> None:
    snapshot = closed_day(
        tmp_path / "engine", binding=missing != "entry-target", fill=missing != "empty-day"
    )
    expected: Dataset = "plan_results"
    if missing == "valuation":
        account = snapshot.broker.account
        account = account.model_copy(
            update={
                "positions": tuple(
                    p.model_copy(update={"market_value": None}) for p in account.positions
                )
            }
        )
        snapshot = rebind(replace(snapshot, broker=replace(snapshot.broker, account=account)))
        expected = "positions"
    elif missing == "asset":
        account = snapshot.broker.account.model_copy(update={"total_equity": None})
        snapshot = rebind(replace(snapshot, broker=replace(snapshot.broker, account=account)))
        expected = "assets"
    elif missing == "exit-plan":
        snapshot = replace(snapshot, plan=None)
    batch, files = freeze(snapshot, tmp_path / "files")
    status = next(s for s in batch.manifest.dataset_status if s.dataset == expected)
    assert status.status == "UNAVAILABLE" and status.reason and not rows(batch, files, expected)
    if missing == "empty-day":
        assert (
            next(s for s in batch.manifest.dataset_status if s.dataset == "broker_trades").status
            == "COMPLETE"
        )
        assert rows(batch, files, "broker_trades") == ()


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "account",
        "count",
        "watermark",
        "plan",
        "lots",
        "coherence",
        "early-generation",
        "naive-generation",
    ],
)
def test_day_packaging_rejects_mixed_or_mutated_evidence(tmp_path: Path, case: str) -> None:
    snapshot = closed_day(tmp_path / "engine")
    generated = snapshot.report.observed_at
    if case == "hash":
        snapshot = replace(
            snapshot,
            report=snapshot.report.model_copy(update={"evidence_hash": "sha256:" + "0" * 64}),
        )
    elif case == "account":
        account = snapshot.broker.account.model_copy(update={"account_id": "other"})
        snapshot = rebind(replace(snapshot, broker=replace(snapshot.broker, account=account)))
    elif case == "count":
        snapshot = replace(
            snapshot, report=snapshot.report.model_copy(update={"submission_count": 5})
        )
    elif case == "watermark":
        snapshot = replace(snapshot, journal_high_watermark=0)
    elif case == "plan":
        assert snapshot.plan
        snapshot = replace(
            snapshot,
            plan=snapshot.plan.model_copy(
                update={"payload": snapshot.plan.payload.model_copy(update={"account_id": "other"})}
            ),
        )
    elif case == "lots":
        snapshot = replace(snapshot, lots=())
    elif case == "coherence":
        snapshot = rebind(replace(snapshot, broker=replace(snapshot.broker, stable=False)))
    elif case == "early-generation":
        generated -= timedelta(seconds=1)
    else:
        generated = generated.replace(tzinfo=None)
    with pytest.raises(ValueError):
        freeze_engine_day(
            snapshot,
            ArtifactFiles(tmp_path / "files"),
            node_id="simulation-node",
            node_generation=1,
            journal_generation="journal-1",
            generated_at=generated,
        )


def test_remote_queries_keep_old_revision_until_all_new_parts_apply(tmp_path: Path) -> None:
    snapshot = closed_day(tmp_path / "engine")
    batch, files = freeze(snapshot, tmp_path / "files")
    receiver = InMemoryDailyReceiver()
    receiver.create(batch.manifest)
    assert (
        receiver.records(
            snapshot.report.account_id, str(snapshot.report.trade_date), "broker_trades"
        )
        == ()
    )
    assert upload_once(batch, files, receiver).status == "SYNCED"
    second, _ = freeze(snapshot, tmp_path / "files", revision=2, previous=batch.manifest.batch_id)
    receiver.create(second.manifest)
    assert receiver.current(
        snapshot.report.account_id, str(snapshot.report.trade_date)
    ) == receiver.receipt(batch.manifest.batch_id)
    assert upload_once(second, files, receiver).status == "SYNCED"
    assert receiver.current(
        snapshot.report.account_id, str(snapshot.report.trade_date)
    ) == receiver.receipt(second.manifest.batch_id)


@pytest.mark.parametrize(
    "field,value", [("plan_id", "wrong"), ("allocation_id", "wrong"), ("target_quantity", 200)]
)
def test_entry_binding_is_enforced_before_j1(tmp_path: Path, field: str, value: str | int) -> None:
    with open_offline_engine(tmp_path) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm()
        binding = EntryBinding(
            plan_id=runtime.plan.payload.plan_id,
            allocation_id=runtime.plan.payload.allocations[0].allocation_id,
            target_quantity=100,
        ).model_copy(update={field: value})
        with pytest.raises(ValueError):
            runtime.kernel.submit(
                runtime.buy().model_copy(update={"entry_binding": binding}),
                valid_until=monotonic() + 1,
            )
        assert runtime.journal.recovery_page().result(2) == ()


def test_entry_binding_fields_legacy_bytes_and_invalid_direction(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as runtime:
        item = runtime.buy()
        assert "entry_binding" not in item.model_dump(mode="json")
        assert all(field.description for field in EntryBinding.model_fields.values())
        body = item.model_dump(mode="json")
        body["entry_binding"] = {"plan_id": "p", "allocation_id": "a", "target_quantity": 99}
        with pytest.raises(ValidationError):
            Submission.model_validate_json(canonical_bytes(body))
        body["entry_binding"] = {"plan_id": "p", "allocation_id": "a", "target_quantity": 100}
        body["side"] = "SELL"
        with pytest.raises(ValidationError):
            Submission.model_validate_json(canonical_bytes(body))


def test_optional_broker_values_have_comments_and_validate_frozen_quantity() -> None:
    for model, names in (
        (AccountFact, ("cash_frozen", "market_value", "total_equity")),
        (PositionFact, ("frozen", "market_value", "cost_amount")),
    ):
        assert all(model.model_fields[name].description for name in names)
    with pytest.raises(ValidationError):
        PositionFact(symbol="000001.SZ", quantity=100, sellable=100, frozen=1)


@pytest.mark.parametrize("second_binding", ["same", "removed", "changed"])
def test_repeated_buy_cannot_exceed_remove_or_change_its_frozen_target(
    tmp_path: Path,
    second_binding: str,
) -> None:
    with open_offline_engine(tmp_path, max_orders=2) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm()
        binding = EntryBinding(
            plan_id=runtime.plan.payload.plan_id,
            allocation_id=runtime.plan.payload.allocations[0].allocation_id,
            target_quantity=100,
        )
        runtime.broker.queue_submit(fill_scenario(100, full=True))
        settle(runtime, runtime.buy().model_copy(update={"entry_binding": binding}))
        candidate = (
            None
            if second_binding == "removed"
            else (
                binding.model_copy(update={"target_quantity": 200})
                if second_binding == "changed"
                else binding
            )
        )
        with pytest.raises(ValueError, match="gate rejected"):
            runtime.kernel.submit(
                runtime.buy().model_copy(update={"entry_binding": candidate}),
                valid_until=monotonic() + 1,
            )
        assert len(runtime.broker.query_orders()) == 1
        assert len(runtime.journal.recovery_page().result(2)) == 1


@pytest.mark.parametrize("filled,expected", [(0, "CANCELLED"), (50, "PARTIAL"), (100, "COMPLETED")])
def test_plan_results_follow_real_reject_partial_cancel_or_fill(
    tmp_path: Path,
    filled: int,
    expected: str,
) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path / "engine", clock=lambda: clock.instant) as runtime:
        engine = runtime.kernel
        engine.start_recovery()
        wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm()
        item = runtime.buy().model_copy(
            update={
                "entry_binding": EntryBinding(
                    plan_id=runtime.plan.payload.plan_id,
                    allocation_id=runtime.plan.payload.allocations[0].allocation_id,
                    target_quantity=100,
                )
            }
        )
        runtime.broker.queue_submit(
            fill_scenario(filled, full=filled == 100)
            if filled
            else SubmitScenario(outcome=SubmitOutcome.REJECT)
        )
        settle(runtime, item)
        if filled == 50:
            original = runtime.broker.query_orders()[0]
            cancel = item.model_copy(
                update={
                    "client_order_id": "ord_test_cancel_0001",
                    "action_type": "CANCEL",
                    "original_client_order_id": item.client_order_id,
                    "original_order_id": original.broker_order_id,
                }
            )
            settle(runtime, cancel)
        clock.instant = DEMO_NOW.replace(hour=15, minute=1)
        engine.start_recovery()
        wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        snapshot = engine.capture_day()
    batch, files = freeze(snapshot, tmp_path / "files")
    result = rows(batch, files, "plan_results")[0].data
    assert result["status"] == expected and result["filled_quantity"] == filled
    assert len(rows(batch, files, "submission_records")) == (2 if filled == 50 else 1)


def test_mixed_bound_and_legacy_buy_cannot_report_partial_target_as_complete(
    tmp_path: Path,
) -> None:
    snapshot = closed_day(tmp_path / "engine")
    first = snapshot.submissions[0]
    legacy = first.submission.model_copy(
        update={"client_order_id": "ord_legacy_child_001", "entry_binding": None}
    )
    second = first.model_copy(
        update={
            "record_seq": 2,
            "submission": legacy,
            "payload_hash": digest(canonical_bytes(legacy.model_dump(mode="json"))),
        }
    )
    snapshot = rebind(
        replace(
            snapshot,
            submissions=(first, second),
            journal_high_watermark=2,
            report=snapshot.report.model_copy(
                update={"submission_count": 2, "submission_high_watermark": 2}
            ),
        )
    )
    batch, files = freeze(snapshot, tmp_path / "files")
    assert not rows(batch, files, "plan_results")
    assert (
        next(s for s in batch.manifest.dataset_status if s.dataset == "plan_results").status
        == "UNAVAILABLE"
    )


def lookup_and_receipt(tmp_path: Path) -> tuple[PlanLookup, PlanReceipt]:
    with open_offline_engine(tmp_path) as runtime:
        p = runtime.plan
        lookup = PlanLookup(
            account_id=p.payload.account_id,
            strategy_id=p.payload.strategy_id,
            trade_date=p.payload.trade_date,
            plan_id=p.payload.plan_id,
            state="PUBLISHED",
            state_version=1,
            etag=p.envelope.payload_hash,
            plan=p,
        )
        receipt = PlanReceipt(
            receipt_id="receipt",
            plan_id=p.payload.plan_id,
            payload_hash=p.envelope.payload_hash,
            node_id="simulation-node",
            node_generation=1,
            account_id=p.payload.account_id,
            engine_boot_id=runtime.kernel.boot_id,
            status="LOADED",
            reason_code="LOADED",
            observed_at=DEMO_NOW,
        )
        return lookup, receipt


def test_plan_handoff_runs_apply_only_on_owner_and_propagates_result(tmp_path: Path) -> None:
    lookup, receipt = lookup_and_receipt(tmp_path)
    mailbox = PlanDeliveryMailbox()
    calls: list[int] = []

    def apply(_: PlanLookup, __: str | None) -> PlanReceipt:
        calls.append(get_ident())
        return receipt

    assert not mailbox.poll(apply)
    with pytest.raises(RuntimeError, match="must not block"):
        mailbox.apply(lookup, None)
    with ThreadPoolExecutor(1) as worker:
        pending = worker.submit(mailbox.apply, lookup, None)
        until = monotonic() + 2
        while not pending.done() and monotonic() < until:
            mailbox.poll(apply)
            sleep(0.001)
        assert pending.result(1) == receipt and calls == [get_ident()]
        with pytest.raises(RuntimeError, match="single engine owner"):
            worker.submit(mailbox.poll, apply).result(1)
        with pytest.raises(RuntimeError, match="close on engine"):
            worker.submit(mailbox.close).result(1)
    mailbox.close()


@pytest.mark.parametrize(
    "case", ["expire-before", "expire-after", "close-pending", "full", "apply-error", "closed"]
)
def test_plan_handoff_expiration_capacity_close_and_apply_failure(
    tmp_path: Path, case: str
) -> None:
    lookup, receipt = lookup_and_receipt(tmp_path)
    mailbox = PlanDeliveryMailbox(capacity=1, timeout=0.05 if case.startswith("expire") else 1)
    calls: list[str] = []

    def apply(_: PlanLookup, __: str | None) -> PlanReceipt:
        calls.append("applied")
        if case == "expire-after":
            sleep(0.1)
        if case == "apply-error":
            raise ValueError("invalid plan")
        return receipt

    if case == "closed":
        mailbox.close()
    with ThreadPoolExecutor(2) as worker:
        pending = worker.submit(mailbox.apply, lookup, None)
        until = monotonic() + 2
        while mailbox._queue.empty() and not pending.done() and monotonic() < until:
            sleep(0.001)
        if case == "full":
            with pytest.raises(RetryableSyncError, match="BUSY"):
                worker.submit(mailbox.apply, lookup, None).result(1)
            mailbox.close()
        elif case == "close-pending":
            mailbox.close()
        elif case in ("expire-after", "apply-error"):
            mailbox.poll(apply)
        error = ValueError if case == "apply-error" else RetryableSyncError
        with pytest.raises(error):
            pending.result(2)
        if case == "expire-before":
            mailbox.poll(apply)
            assert calls == []
        if case == "expire-after":
            assert calls == ["applied"]  # Uncertain receipt never rolls back engine application.
    mailbox.close()


@pytest.mark.parametrize("capacity,timeout", [(0, 1), (33, 1), (True, 1), (1, 0), (1, 9)])
def test_plan_delivery_budget_validation(capacity: int, timeout: float) -> None:
    with pytest.raises(ValueError):
        PlanDeliveryMailbox(capacity=capacity, timeout=timeout)


def test_business_cli_links_actual_received_buy_and_sell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--simulate-business-flow", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ack_loss_repaired"] and result["plan_receipts"] == 1
    assert result["same_lot_across_days"] and result["restart_paused"]
    assert result["bought_quantity"] == result["sold_quantity"] == 100
    assert result["remaining_quantity"] == 0 and result["final_cash"] == "10000.00"
    assert [day["trade"]["side"] for day in result["days"]] == ["BUY", "SELL"]
    assert result["days"][0]["business_status"] == "COMPLETE"
    assert result["days"][1]["business_status"] == "INCOMPLETE"
    assert all(day["financial_status"] == "PROVISIONAL" for day in result["days"])
    assert not result["real_trading_enabled"]
