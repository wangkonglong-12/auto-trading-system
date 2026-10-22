from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import (
    ExitScenarioClock,
    fixture_context,
    open_exit_owner,
    publish,
    pump,
)
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
    next_day_plan,
)
from auto_trading_system.apps.trade_runtime.journal_simulation import seed_capacity_fixture
from auto_trading_system.execution.sell_policy import EXIT_RULE, allows_sell, exit_action_id
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.journal.apsw_store import APSWCriticalStore, EvidenceTable
from auto_trading_system.journal.checkpoint_blobs import read_checkpoint
from auto_trading_system.journal.critical_models import (
    ActionTarget,
    RecoveryRecord,
    SellAllocation,
    SellBinding,
    Submission,
)
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.journal_scan import JournalScan, PrefixDigest
from auto_trading_system.reconcile.portfolio_checkpoint import PortfolioCheckpoint, prefix_hash
from auto_trading_system.reconcile.position_lots import PortfolioProjection, PositionLot


def records(submission: Submission, count: int, offset: int = 0) -> tuple[RecoveryRecord, ...]:
    result = []
    for index in range(offset + 1, offset + count + 1):
        item = submission.model_copy(update={"client_order_id": f"ord_scan_{index:08d}"})
        result.append(
            RecoveryRecord(
                record_seq=index,
                payload_hash=digest(canonical_bytes(item.model_dump(mode="json"))),
                submission=item,
            )
        )
    return tuple(result)


def checkpoint(
    items: tuple[RecoveryRecord, ...], lots: tuple[PositionLot, ...] = ()
) -> PortfolioCheckpoint:
    return PortfolioCheckpoint(
        account_id="simulation-account",
        trade_date=DEMO_NOW.date(),
        calendar_version="offline-calendar-v1",
        recovery_report_hash="sha256:" + "a" * 64,
        submission_high_watermark=items[-1].record_seq if items else 0,
        submission_prefix_hash=prefix_hash(items),
        lots=lots,
    )


def live_lot() -> PositionLot:
    return PositionLot(
        lot_id="lot-history",
        strategy_id="tail-entry",
        symbol="000001.SZ",
        trade_date_acquired=DEMO_NOW.date() - timedelta(days=1),
        eligible_sell_date=DEMO_NOW.date(),
        calendar_version="offline-calendar-v1",
        bought_quantity=100,
    )


@pytest.mark.parametrize("count", [0, 1, 199, 200, 201, 3001])
def test_stream_digest_matches_legacy_canonical_prefix(submission: Submission, count: int) -> None:
    items = records(submission, count)
    prefix = PrefixDigest()
    assert prefix.payload_hash == prefix_hash(())
    for item in items:
        prefix.add(item)
        assert (
            prefix.payload_hash == prefix.payload_hash
        )  # Snapshot hashing does not consume state.
    assert prefix.payload_hash == prefix_hash(items)
    assert prefix.count == count and prefix.high_watermark == count
    if items:
        with pytest.raises(ValueError):
            prefix.add(items[-1])


def test_history_is_verified_then_released_but_current_day_is_retained(
    submission: Submission,
) -> None:
    history = records(submission, 3500)
    today = records(submission.model_copy(update={"trade_date": D1_NOW.date()}), 23, len(history))
    scan = JournalScan(
        account_id=submission.account_id, trade_date=D1_NOW.date(), checkpoint=checkpoint(history)
    )
    both = history + today
    for offset in range(0, len(both), 200):
        scan.consume(both[offset : offset + 200])
        assert len(scan.current) <= len(today)
    scan.finish()
    assert scan.current == list(today)
    assert scan.prefix.count == 3523 and scan.prefix.payload_hash == prefix_hash(both)
    assert scan.targets == {}
    with pytest.raises(ValueError, match="finished"):
        scan.finish()
    with pytest.raises(ValueError, match="lifecycle"):
        scan.consume(today)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "wrong-account",
        "future-history",
        "unsealed",
        "future-current",
        "reorder",
        "wrong-digest",
        "wrong-watermark",
        "empty-page",
        "oversized-page",
    ],
)
def test_stream_scan_rejects_scope_prefix_and_pagination_gaps(
    submission: Submission, case: str
) -> None:
    history = records(submission, 3)
    cp = checkpoint(history)
    today = records(submission.model_copy(update={"trade_date": D1_NOW.date()}), 1, 3)
    supplied = history + today
    if case == "missing":
        supplied = supplied[1:]
    elif case == "wrong-account":
        supplied = (
            history[0].model_copy(
                update={"submission": submission.model_copy(update={"account_id": "other"})}
            ),
        )
    elif case == "future-history":
        supplied = (
            history[0].model_copy(
                update={"submission": submission.model_copy(update={"trade_date": D1_NOW.date()})}
            ),
        )
    elif case == "unsealed":
        supplied = history + records(submission, 1, 3)
    elif case == "future-current":
        supplied = history + records(
            submission.model_copy(update={"trade_date": D1_NOW.date() + timedelta(days=1)}), 1, 3
        )
    elif case == "reorder":
        supplied = (history[1], history[0], history[2])
    elif case == "wrong-digest":
        cp = cp.model_copy(update={"submission_prefix_hash": "sha256:" + "f" * 64})
    elif case == "wrong-watermark":
        cp = cp.model_copy(update={"submission_high_watermark": 4})
        supplied = history
    elif case == "empty-page":
        supplied = ()
    elif case == "oversized-page":
        supplied = records(submission, 201)
    scan = JournalScan(account_id=submission.account_id, trade_date=D1_NOW.date(), checkpoint=cp)
    with pytest.raises(ValueError):
        scan.consume(supplied)
        scan.finish()


@pytest.mark.parametrize("capacity", ["history", "current"])
def test_scan_limits_are_explicit_not_silent_truncation(
    submission: Submission, monkeypatch: pytest.MonkeyPatch, capacity: str
) -> None:
    monkeypatch.setattr(
        "auto_trading_system.reconcile.journal_scan.MAX_SCAN_RECORDS"
        if capacity == "history"
        else "auto_trading_system.reconcile.journal_scan.MAX_CURRENT_RECORDS",
        2,
    )
    scan = JournalScan(
        account_id=submission.account_id, trade_date=DEMO_NOW.date(), checkpoint=None
    )
    with pytest.raises(ValueError, match="capacity"):
        scan.consume(records(submission, 3))


@pytest.mark.parametrize("case", ["retain", "closed", "widen", "rule"])
def test_historical_sell_target_is_compact_and_preserves_monotonic_guard(
    submission: Submission, case: str
) -> None:
    lot = live_lot()
    target = ActionTarget(
        action_id=exit_action_id(submission.account_id, lot.strategy_id, lot.lot_id),
        target_remaining=50,
        rule_id=EXIT_RULE,
    )
    first = submission.model_copy(
        update={
            "side": "SELL",
            "quantity": 50,
            "maximum_amount": "0",
            "target": target,
            "sell_binding": SellBinding(
                strategy_id=lot.strategy_id,
                opening_hash="sha256:" + "b" * 64,
                allocations=(SellAllocation(lot_id=lot.lot_id, quantity=50),),
            ),
        }
    )
    second = first.model_copy(
        update={
            "target": target.model_copy(
                update={
                    "target_remaining": 60 if case == "widen" else 20,
                    "rule_id": "other-rule" if case == "rule" else EXIT_RULE,
                }
            )
        }
    )
    history = records(first, 1) + records(second, 1, 1)
    if case == "closed":
        lot = lot.model_copy(update={"sold_quantity": 100})
    scan = JournalScan(
        account_id=submission.account_id,
        trade_date=D1_NOW.date(),
        checkpoint=checkpoint(history, (lot,)),
    )
    if case in ("widen", "rule"):
        with pytest.raises(ValueError, match="target"):
            scan.consume(history)
        return
    scan.consume(history)
    scan.finish()
    assert len(scan.targets) == (0 if case == "closed" else 1)
    assert scan.current == []
    if case == "retain":
        assert next(iter(scan.targets.values())).target_remaining == 20
        assert not allows_sell(
            first,
            portfolio=PortfolioProjection((lot,), frozenset(), (), ()),
            strategy_id=lot.strategy_id,
            opening_hash="sha256:" + "b" * 64,
            as_of=D1_NOW.date(),
            broker_sellable=100,
            reflected_order_ids=frozenset(),
            prior=(),
            historical_targets=tuple(scan.targets.values()),
        )


def test_capacity_cli_recovers_more_than_2048_history_without_keeping_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--simulate-journal", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scanned_records"] == 3320 and result["retained_records"] == 120
    assert result["checkpoint_bytes"] > 64 * 1024 and result["checkpoint_fragments"] >= 3
    assert result["restart_paused"] and not result["real_trading_enabled"]
    assert result["historical_rows_deleted"] == 0 and result["payload_verified"]


@pytest.mark.parametrize("fault", ["HALT", "DEADLINE", "DISK"])
def test_segmented_recovery_is_never_published_after_failure_or_permission_change(
    tmp_path: Path, fault: str
) -> None:
    fixture = seed_capacity_fixture(tmp_path, history_count=5, current_count=120)
    started, release = Event(), Event()

    class DelayedRoot(APSWCriticalStore):
        def append_evidence(self, table: EvidenceTable, key: str, body: bytes) -> bool:
            if key.startswith("recovery-"):
                started.set()
                assert release.wait(3)
                if fault == "DISK":
                    raise OSError("injected manifest publication failure")
            return super().append_evidence(table, key, body)

    with open_offline_engine(
        tmp_path,
        broker=fixture.broker,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=fixture.opening,
        journal_factory=lambda path, account: CriticalJournalOwner(
            lambda: DelayedRoot(path, account)
        ),
    ) as runtime:
        try:
            kernel = runtime.kernel
            kernel.start_recovery()
            until = monotonic() + 3
            while not started.is_set() and monotonic() < until:
                kernel.poll()
                sleep(0.001)
            assert started.is_set() and kernel.snapshot().halted
            report = kernel.snapshot().recovery
            assert report is not None
            request = runtime.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
            runtime.approvals.approve(request, authenticated_actor="test-operator")
            assert kernel.control(request).status == "REJECTED"
            if fault == "HALT":
                kernel.emergency_halt("TEST_HALT_DURING_ROOT")
            elif fault == "DEADLINE":
                kernel._recovery_deadline = monotonic() - 1
            kernel.poll()
            release.set()
            until = monotonic() + 2
            while kernel.poll().state != "HALTED" and monotonic() < until:
                sleep(0.001)
            assert kernel.snapshot().halted and kernel.snapshot().recovery is None
            assert kernel.snapshot().grant_id is None
            for _ in range(10):
                kernel.poll()
                sleep(0.001)
            assert kernel.snapshot().state == "HALTED"
        finally:
            release.set()


def test_new_closing_uses_full_stream_digest_and_keeps_historical_j1(tmp_path: Path) -> None:
    fixture = seed_capacity_fixture(tmp_path, history_count=2200, current_count=4)
    now = D1_NOW
    with open_offline_engine(
        tmp_path,
        broker=fixture.broker,
        clock=lambda: now,
        plan_payload=next_day_plan(),
        opening_reference=fixture.opening,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        # A plan is verified during its loading window, then retained for the
        # paused end-of-day closing, as in the existing production boundary.
        now = D1_NOW.replace(hour=15, minute=1)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        closing = runtime.kernel.seal_portfolio().result(3)
        body = runtime.journal.request(
            lambda store: read_checkpoint(store, closing.key), deadline=monotonic() + 2
        ).result(3)
        assert body is not None
        saved = PortfolioCheckpoint.model_validate_json(body)
        assert saved.submission_high_watermark == 2204 and saved.lots == ()
        rows: list[RecoveryRecord] = []
        page = runtime.journal.recovery_page().result(3)
        while page:
            rows.extend(page)
            page = runtime.journal.recovery_page(page[-1].record_seq).result(3)
        assert len(rows) == 2204 and saved.submission_prefix_hash == prefix_hash(tuple(rows))


def test_fully_exited_lots_are_carried_in_history_not_next_opening(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    context = fixture_context(tmp_path, opening)
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_exit_owner(tmp_path, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        broker.queue_submit(fill_scenario(100, full=True))
        publish(owner, clock, "10.7", 1)
        pump(
            owner,
            lambda: runtime.kernel.snapshot().lots[0].remaining == 0
            and runtime.kernel.critical_idle,
        )
        clock.instant = D1_NOW.replace(hour=15, minute=1)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        ref = runtime.kernel.seal_portfolio().result(3)
        body = runtime.journal.request(
            lambda store: read_checkpoint(store, ref.key), deadline=monotonic() + 2
        ).result(3)
        assert body is not None and PortfolioCheckpoint.model_validate_json(body).lots == ()
        assert len(runtime.journal.recovery_page().result(3)) == 2


def test_fixture_never_reuses_an_existing_journal(tmp_path: Path) -> None:
    seed_capacity_fixture(tmp_path, history_count=0, current_count=0)
    with pytest.raises(ValueError, match="fresh private"):
        seed_capacity_fixture(tmp_path, history_count=0, current_count=0)
    with pytest.raises(ValueError, match="budget"):
        seed_capacity_fixture(tmp_path / "other", history_count=-1)


def test_large_opening_is_verified_and_resealed_without_dropping_lots(tmp_path: Path) -> None:
    from auto_trading_system.journal.checkpoint_blobs import write_checkpoint

    # Synthetic local opening evidence with 1,000 live one-share batches. The
    # account query independently reports 1,000 shares; this is not financial
    # qualification of a real account or a shortcut for production sealing.
    lots = tuple(
        PositionLot(
            lot_id=f"lot-large-{n}",
            strategy_id="tail-entry",
            symbol="000001.SZ",
            trade_date_acquired=DEMO_NOW.date(),
            eligible_sell_date=D1_NOW.date(),
            calendar_version="offline-calendar-v1",
            bought_quantity=1,
        )
        for n in range(1000)
    )
    cp = checkpoint((), lots)
    store = APSWCriticalStore(tmp_path / "critical/journal-v2.db", "simulation-account")
    store.open()
    try:
        write_checkpoint(store, cp.reference().key, cp.body())
    finally:
        store.close()
    now = D1_NOW
    with open_offline_engine(
        tmp_path,
        clock=lambda: now,
        plan_payload=next_day_plan(),
        opening_reference=cp.reference(),
        broker_opening_quantity=1000,
    ) as runtime:
        runtime.kernel.start_recovery()
        state = wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        assert len(state.lots) == 1000 and sum(lot.remaining for lot in state.lots) == 1000
        assert state.halted and state.retained_recovery_records == 0
        now = D1_NOW.replace(hour=15, minute=1)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        closing = runtime.kernel.seal_portfolio().result(3)
        body = runtime.journal.request(
            lambda owner: read_checkpoint(owner, closing.key), deadline=monotonic() + 2
        ).result(3)
        assert body is not None and len(body) > 64 * 1024
        assert len(PortfolioCheckpoint.model_validate_json(body).lots) == 1000
