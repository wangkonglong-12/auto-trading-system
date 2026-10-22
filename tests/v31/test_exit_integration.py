from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    OfflineEngine,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
    next_day_plan,
    prepare_child,
    settle,
)
from auto_trading_system.domain import Price
from auto_trading_system.execution.sell_policy import allows_sell
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import (
    CommitEvidence,
    CriticalStoreError,
    RecoveryRecord,
    SellAllocation,
    SellBinding,
    Submission,
)
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.portfolio_checkpoint import (
    OpeningReference,
    PortfolioCheckpoint,
    prefix_hash,
    verify_opening,
)
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    LotAllocation,
    LotIntent,
    PortfolioProjection,
    PositionLot,
    project_lots,
)
from auto_trading_system.runtime.engine_v2 import EngineSnapshot
from auto_trading_system.strategies.exit_execution import exit_child, restore_exit_state
from auto_trading_system.strategies.exit_v3 import ExitDecision, ExitState
from auto_trading_system.testing.v31_scenarios import ScenarioClock


def sessions() -> ExchangeCalendar:
    return ExchangeCalendar(
        version="offline-calendar-v1",
        sessions=(
            DEMO_NOW.date(),
            D1_NOW.date(),
            (D1_NOW + timedelta(days=1)).date(),
        ),
    )


def lot_fixture() -> PositionLot:
    return PositionLot(
        lot_id="lot-test",
        strategy_id="tail-entry",
        symbol="000001.SZ",
        trade_date_acquired=DEMO_NOW.date(),
        eligible_sell_date=D1_NOW.date(),
        calendar_version=sessions().version,
        bought_quantity=100,
    )


def child_fixture(*, lot: PositionLot | None = None) -> Submission:
    child = exit_child(
        lot=lot or lot_fixture(),
        decision=ExitDecision(ExitState(50), (), True),
        account_id="simulation-account",
        trade_date=D1_NOW.date(),
        plan_hash="sha256:" + "a" * 64,
        opening_hash="sha256:" + "b" * 64,
        boot_id="boot-test",
        permission_epoch=2,
        limit_price=Price.parse("10"),
        quantity_limit=100,
        broker_sellable=100,
    )
    assert child
    return child


def poll_until(
    runtime: OfflineEngine, predicate: Callable[[EngineSnapshot], bool]
) -> EngineSnapshot:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        state = runtime.kernel.poll()
        if predicate(state):
            return state
        sleep(0.001)
    raise AssertionError("state did not settle")


@contextmanager
def armed_sell(directory: Path) -> Iterator[tuple[OfflineEngine, OpeningReference]]:
    opening = close_buy_day(directory)
    with open_offline_engine(
        directory,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm(directions=("SELL",))
        yield runtime, opening


def test_cross_day_cli_partial_cancel_restart_keeps_original_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--simulate-exit", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["first_fill"], result["residual_child_quantity"], result["final_remaining"]) == (
        20,
        30,
        50,
    )
    assert result["restart_paused"] and not result["restored_path_complete"]
    assert not result["real_trading_enabled"] and result["d1_broker_orders"] == 2


def test_legacy_j1_payload_hash_and_duplicate_semantics_are_unchanged(
    tmp_path: Path,
    submission: Submission,
) -> None:
    legacy = submission.model_dump(mode="json")
    assert "sell_binding" not in legacy
    assert legacy["original_order_id"] is None and legacy["original_client_order_id"] is None
    body = canonical_bytes(legacy)
    restored = Submission.model_validate_json(body)
    assert canonical_bytes(restored.model_dump(mode="json")) == body
    store = APSWCriticalStore(tmp_path / "legacy.db", submission.account_id)
    store.open()
    try:
        assert store.commit_submission(restored).payload_hash == digest(body)
        assert not store.commit_submission(restored).fresh
        assert store.read_submissions()[0].payload_hash == digest(body)
    finally:
        store.close()


def test_sell_binding_is_in_same_durable_payload_and_conflicting_rebind_fails(
    tmp_path: Path,
) -> None:
    child = child_fixture()
    path = tmp_path / "critical.db"
    store = APSWCriticalStore(path, child.account_id)
    store.open()
    evidence = store.commit_submission(child)
    store.close()
    store.open()
    try:
        restored = store.read_submissions()[0]
        assert restored.submission == child and restored.payload_hash == evidence.payload_hash
        assert not store.commit_submission(child).fresh
        assert child.sell_binding
        changed = child.model_copy(
            update={
                "sell_binding": child.sell_binding.model_copy(
                    update={
                        "opening_hash": "sha256:" + "c" * 64,
                    }
                )
            }
        )
        with pytest.raises(CriticalStoreError, match="CONTENT_CONFLICT"):
            store.commit_submission(changed)
        assert store.read_submissions()[0] == restored
    finally:
        store.close()


@pytest.mark.parametrize("case", ["duplicate", "total", "buy", "rules", "zero"])
def test_invalid_sell_binding_contract(case: str) -> None:
    child = child_fixture()
    assert child.sell_binding
    data = child.model_dump(mode="json")
    binding = child.sell_binding.model_dump(mode="json")
    if case == "duplicate":
        binding["allocations"] *= 2
    elif case == "total":
        data["quantity"] = 51
    elif case == "buy":
        data.update(side="BUY", maximum_amount="1005")
    elif case == "rules":
        binding["fired_rules"] = ["z", "a"]
    else:
        binding["allocations"] = [{"lot_id": "lot-test", "quantity": 0}]
    data["sell_binding"] = binding
    with pytest.raises(ValueError):
        Submission.model_validate_json(canonical_bytes(data))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "hash",
        "account",
        "calendar",
        "session",
        "prefix",
        "watermark",
        "removed",
        "historical_tail",
        "future_tail",
        "prefix_date",
        "prefix_account",
    ],
)
def test_opening_checkpoint_never_skips_unverified_history(
    submission: Submission, mutation: str
) -> None:
    record = RecoveryRecord(record_seq=1, payload_hash="sha256:" + "a" * 64, submission=submission)
    records: tuple[RecoveryRecord, ...] = (record,)
    checkpoint = PortfolioCheckpoint(
        account_id=submission.account_id,
        trade_date=DEMO_NOW.date(),
        calendar_version=sessions().version,
        recovery_report_hash="sha256:" + "c" * 64,
        submission_high_watermark=1,
        submission_prefix_hash=prefix_hash(records),
        lots=(lot_fixture(),),
    )
    if mutation in ("account", "calendar", "prefix", "watermark"):
        field, value = {
            "account": ("account_id", "other"),
            "calendar": ("calendar_version", "other"),
            "prefix": ("submission_prefix_hash", "sha256:" + "d" * 64),
            "watermark": ("submission_high_watermark", 2),
        }[mutation]
        checkpoint = checkpoint.model_copy(update={field: value})
    elif mutation == "removed":
        records = ()
    elif mutation in ("historical_tail", "future_tail"):
        tail = (
            submission
            if mutation == "historical_tail"
            else submission.model_copy(
                update={
                    "trade_date": D1_NOW.date() + timedelta(days=1),
                }
            )
        )
        records += (record.model_copy(update={"record_seq": 2, "submission": tail}),)
    elif mutation in ("prefix_date", "prefix_account"):
        item = submission.model_copy(
            update={
                "trade_date": D1_NOW.date(),
            }
            if mutation == "prefix_date"
            else {"account_id": "other"}
        )
        records = (record.model_copy(update={"submission": item}),)
    body: bytes | None = checkpoint.body()
    reference = checkpoint.reference()
    if mutation == "missing":
        body = None
    elif mutation == "hash":
        reference = reference.model_copy(update={"payload_hash": "sha256:" + "e" * 64})
    day = D1_NOW.date() + timedelta(days=1) if mutation == "session" else D1_NOW.date()
    with pytest.raises(ValueError):
        verify_opening(
            body,
            reference,
            records,
            account_id=submission.account_id,
            trade_date=day,
            calendar=sessions(),
        )


@pytest.mark.parametrize("case", ["duplicate", "reserved", "calendar", "future"])
def test_closing_rejects_unsettled_or_misdated_lots(case: str) -> None:
    lot = lot_fixture()
    if case != "duplicate":
        changes: dict[str, dict[str, object]] = {
            "reserved": {"reserved_quantity": 1},
            "calendar": {"calendar_version": "wrong"},
            "future": {
                "trade_date_acquired": D1_NOW.date(),
                "eligible_sell_date": D1_NOW.date() + timedelta(days=1),
            },
        }
        lot = lot.model_copy(update=changes[case])
    with pytest.raises(ValueError):
        PortfolioCheckpoint(
            account_id="simulation-account",
            trade_date=DEMO_NOW.date(),
            calendar_version=sessions().version,
            recovery_report_hash="sha256:" + "a" * 64,
            submission_high_watermark=0,
            submission_prefix_hash=prefix_hash(()),
            lots=(lot, lot) if case == "duplicate" else (lot,),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "no_binding",
        "opening",
        "strategy",
        "lot",
        "action",
        "rule",
        "excess_target",
        "missing_position",
        "T0",
        "locked",
        "broker_short",
        "reserved",
        "widen_target",
        "multi_lot",
    ],
)
def test_sell_policy_fails_closed(mutation: str) -> None:
    child = child_fixture()
    assert child.sell_binding
    lot, binding = lot_fixture(), child.sell_binding
    prior: tuple[Submission, ...] = ()
    if mutation in ("opening", "strategy", "lot", "multi_lot"):
        changes: dict[str, dict[str, object]] = {
            "opening": {"opening_hash": "sha256:" + "c" * 64},
            "strategy": {"strategy_id": "wrong"},
            "lot": {"allocations": (SellAllocation(lot_id="wrong", quantity=50),)},
            "multi_lot": {
                "allocations": (
                    SellAllocation(lot_id=lot.lot_id, quantity=25),
                    SellAllocation(lot_id="other", quantity=25),
                )
            },
        }
        child = child.model_copy(
            update={"sell_binding": binding.model_copy(update=changes[mutation])}
        )
    elif mutation == "no_binding":
        child = child.model_copy(update={"sell_binding": None})
    elif mutation in ("action", "rule", "excess_target"):
        changes = {
            "action": {"action_id": "wrong"},
            "rule": {"rule_id": "wrong"},
            "excess_target": {"target_remaining": 51},
        }
        child = child.model_copy(
            update={"target": child.target.model_copy(update=changes[mutation])}
        )
    elif mutation == "reserved":
        lot = lot.model_copy(update={"reserved_quantity": 1})
    elif mutation == "widen_target":
        prior = (
            child.model_copy(
                update={
                    "client_order_id": "ord_prior001",
                    "target": child.target.model_copy(update={"target_remaining": 0}),
                }
            ),
        )
    portfolio = PortfolioProjection(
        () if mutation == "missing_position" else (lot,),
        frozenset({lot.symbol}) if mutation == "locked" else frozenset(),
        (),
        (),
    )
    assert not allows_sell(
        child,
        portfolio=portfolio,
        strategy_id=lot.strategy_id,
        opening_hash=binding.opening_hash,
        as_of=DEMO_NOW.date() if mutation == "T0" else D1_NOW.date(),
        broker_sellable=49 if mutation == "broker_short" else 100,
        reflected_order_ids=frozenset(),
        prior=prior,
    )


@pytest.mark.parametrize(
    "mutation", ["no_binding", "opening", "strategy", "lot", "action", "target", "notional"]
)
def test_engine_rejects_unbound_or_excess_sell_before_any_j1(tmp_path: Path, mutation: str) -> None:
    with armed_sell(tmp_path) as (runtime, opening):
        child = prepare_child(runtime, opening)
        assert child and child.sell_binding
        if mutation == "no_binding":
            child = child.model_copy(update={"sell_binding": None})
        elif mutation in ("opening", "strategy", "lot"):
            assert child.sell_binding is not None
            changes: dict[str, dict[str, object]] = {
                "opening": {"opening_hash": "sha256:" + "f" * 64},
                "strategy": {"strategy_id": "other"},
                "lot": {"allocations": (SellAllocation(lot_id="other", quantity=50),)},
            }
            child = child.model_copy(
                update={"sell_binding": child.sell_binding.model_copy(update=changes[mutation])}
            )
        elif mutation == "notional":
            child = child.model_copy(update={"limit_price": "21"})
        else:
            child = child.model_copy(
                update={
                    "target": child.target.model_copy(
                        update={
                            "action_id": "other",
                        }
                        if mutation == "action"
                        else {"target_remaining": 51}
                    )
                }
            )
        with pytest.raises(ValueError, match="gate rejected"):
            runtime.kernel.submit(child, valid_until=monotonic() + 1)
        assert len(runtime.journal.recovery_page().result(2)) == 1
        assert runtime.broker.query_orders() == ()


@pytest.mark.parametrize("case", ["absent", "wrong_hash", "broker_mismatch", "no_reference"])
def test_cross_day_invalid_inputs_never_authorize(tmp_path: Path, case: str) -> None:
    opening: OpeningReference | None = close_buy_day(tmp_path)
    if case == "no_reference":
        opening = None
    elif case == "wrong_hash":
        assert opening is not None
        opening = opening.model_copy(update={"payload_hash": "sha256:" + "f" * 64})
    directory = tmp_path / "other-journal" if case == "absent" else tmp_path
    with open_offline_engine(
        directory,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=99 if case == "broker_mismatch" else 100,
    ) as runtime:
        runtime.kernel.start_recovery()
        state = poll_until(runtime, lambda s: s.state in ("HALTED", "RECOVERY_BLOCKED"))
        assert state.halted and state.grant_id is None
        assert not state.recovery or state.recovery.status == "BLOCKED"
        assert runtime.broker.query_orders() == ()


def test_active_sell_keeps_lot_freeze_and_blocks_another_child(tmp_path: Path) -> None:
    with armed_sell(tmp_path) as (runtime, opening):
        child = prepare_child(runtime, opening)
        assert child
        runtime.broker.queue_submit(fill_scenario(20, full=False))
        settle(runtime, child)
        lot = runtime.kernel.snapshot().lots[0]
        assert (lot.remaining, lot.reserved_quantity) == (80, 30)
        assert prepare_child(runtime, opening) is None
        with pytest.raises(ValueError, match="gate rejected"):
            runtime.kernel.submit(
                child.model_copy(update={"client_order_id": "ord_another01"}),
                valid_until=monotonic() + 1,
            )


def test_sell_j1_halt_race_retains_binding_without_dispatch(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    entered, release = Event(), Event()

    class SlowStore(APSWCriticalStore):
        def commit_submission(self, item: Submission) -> CommitEvidence:
            entered.set()
            assert release.wait(3)
            return super().commit_submission(item)

    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
        journal_factory=lambda p, a: CriticalJournalOwner(lambda: SlowStore(p, a)),
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm(directions=("SELL",))
        child = prepare_child(runtime, opening)
        assert child
        try:
            runtime.kernel.submit(child, valid_until=monotonic() + 1)
            assert entered.wait(1)
            runtime.kernel.emergency_halt()
        finally:
            release.set()
        poll_until(
            runtime, lambda s: bool(s.submissions) and s.submissions[0].status != "PERSISTING"
        )
        records = runtime.journal.recovery_page().result(2)
        assert records[-1].submission.sell_binding == child.sell_binding
        assert runtime.broker.query_orders() == ()
        assert runtime.kernel.snapshot().halted


def test_uncertain_sell_never_replays_and_blocks_closing(tmp_path: Path) -> None:
    with armed_sell(tmp_path) as (runtime, opening):
        child = prepare_child(runtime, opening)
        assert child
        runtime.broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_AFTER_ACCEPT))
        runtime.kernel.submit(child, valid_until=monotonic() + 1)
        poll_until(
            runtime, lambda s: bool(s.submissions) and s.submissions[0].status == "UNCERTAIN"
        )
        runtime.kernel.start_recovery()
        state = poll_until(runtime, lambda s: s.state == "RECOVERY_BLOCKED")
        assert state.halted and state.recovery and state.recovery.status == "BLOCKED"
        with pytest.raises(ValueError):
            runtime.kernel.seal_portfolio()
        with pytest.raises(ValueError, match="cannot be replayed"):
            runtime.kernel.submit(child, valid_until=monotonic() + 1)
        assert len(runtime.broker.query_orders()) == 1


def test_sealing_before_close_and_with_pending_orders_is_rejected(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        with pytest.raises(ValueError, match="closing requires"):
            runtime.kernel.seal_portfolio()


def test_restored_exit_drops_monotonic_path_and_retains_trigger_and_target() -> None:
    child = child_fixture()
    assert child.sell_binding
    child = child.model_copy(
        update={
            "sell_binding": child.sell_binding.model_copy(
                update={
                    "fired_rules": ("OPEN_OBSERVED", "OPEN_REDUCE"),
                }
            )
        }
    )
    state = restore_exit_state(lot_fixture(), (child,), account_id=child.account_id)
    assert state.target_remaining == 50 and state.fired == frozenset(
        {"OPEN_OBSERVED", "OPEN_REDUCE"}
    )
    assert not state.path_complete and state.peak_price is None
    assert state.drawdown_since_ns is None and state.last_received_ns is None
    conflicting = child.model_copy(
        update={"target": child.target.model_copy(update={"target_remaining": 60})}
    )
    with pytest.raises(ValueError, match="inconsistent"):
        restore_exit_state(lot_fixture(), (child, conflicting), account_id=child.account_id)


@pytest.mark.parametrize("case", ["T0", "disabled", "target", "quantity", "broker", "reached"])
def test_exit_adapter_bounds_and_no_action_cases(case: str) -> None:
    # Keep a typed callable instead of weakening production boundaries for tests.
    def invoke() -> Submission | None:
        return exit_child(
            lot=lot_fixture().model_copy(update={"sold_quantity": 50})
            if case == "reached"
            else lot_fixture(),
            decision=ExitDecision(
                ExitState(101 if case == "target" else 50), (), case != "disabled"
            ),
            account_id="simulation-account",
            trade_date=DEMO_NOW.date() if case == "T0" else D1_NOW.date(),
            plan_hash="sha256:" + "a" * 64,
            opening_hash="sha256:" + "b" * 64,
            boot_id="boot-test",
            permission_epoch=2,
            limit_price=Price.parse("10"),
            quantity_limit=0 if case == "quantity" else 100,
            broker_sellable=-1 if case == "broker" else 100,
        )

    if case in ("target", "quantity", "broker"):
        with pytest.raises(ValueError):
            invoke()
    else:
        assert invoke() is None


def test_new_durable_models_have_field_descriptions() -> None:
    for model in (SellBinding, SellAllocation, PortfolioCheckpoint, OpeningReference):
        assert all(field.description for field in model.model_fields.values())


def test_projection_cannot_reassign_a_durable_sell_binding() -> None:
    child = child_fixture()
    with pytest.raises(ValueError, match="differs from durable"):
        project_lots(
            account_id=child.account_id,
            as_of=D1_NOW.date(),
            calendar=sessions(),
            intents=(
                LotIntent(child, "tail-entry", (LotAllocation(lot_id="other", quantity=50),)),
            ),
            trades=(),
            opening=(lot_fixture(),),
        )


def test_closing_payload_retains_all_lots_beyond_one_sql_row() -> None:
    checkpoint = PortfolioCheckpoint(
        account_id="simulation-account",
        trade_date=DEMO_NOW.date(),
        calendar_version=sessions().version,
        recovery_report_hash="sha256:" + "a" * 64,
        submission_high_watermark=0,
        submission_prefix_hash=prefix_hash(()),
        lots=tuple(lot_fixture().model_copy(update={"lot_id": f"lot-{n}"}) for n in range(1000)),
    )
    # dev44 uses a manifest for large logical payloads; the 64KiB SQL-row
    # boundary now applies to each fragment rather than discarding lots.
    assert len(checkpoint.body()) > 64 * 1024
    assert PortfolioCheckpoint.model_validate_json(checkpoint.body()) == checkpoint
    assert len(checkpoint.lots) == 1000


def test_cancel_must_preserve_original_sell_binding(tmp_path: Path) -> None:
    with armed_sell(tmp_path) as (runtime, opening):
        child = prepare_child(runtime, opening)
        assert child
        settle(runtime, child)
        observed = runtime.broker.query_orders()[0]
        cancel = child.model_copy(
            update={
                "client_order_id": "ord_badcancel01",
                "action_type": "CANCEL",
                "original_order_id": observed.broker_order_id,
                "original_client_order_id": child.client_order_id,
                "sell_binding": None,
            }
        )
        with pytest.raises(ValueError, match="gate rejected"):
            runtime.kernel.submit(cancel, valid_until=monotonic() + 1)
        assert runtime.kernel.snapshot().lots[0].reserved_quantity == 50
        assert len(runtime.journal.recovery_page().result(2)) == 2


def test_no_sell_permission_does_not_follow_from_verified_opening(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm()  # BUY-only local permission.
        child = prepare_child(runtime, opening)
        assert child
        with pytest.raises(ValueError, match="gate rejected"):
            runtime.kernel.submit(child, valid_until=monotonic() + 1)
        assert runtime.broker.query_orders() == ()


def test_old_sell_without_binding_cannot_be_recovered_as_owned(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    broker = MockBrokerGateway(ScenarioClock(D1_NOW))
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm(directions=("SELL",))
        child = prepare_child(runtime, opening)
        assert child
        broker.queue_submit(fill_scenario(50, full=True))
        settle(runtime, child)
    # A separate legacy journal is seeded explicitly for this recovery test;
    # no accepted checkpoint or trading directory is altered in place.
    legacy = tmp_path / "legacy"
    from auto_trading_system.journal.critical_writer import open_critical_journal

    owner = open_critical_journal(legacy / "critical" / "journal-v2.db", child.account_id)
    try:
        owner.commit(
            child.model_copy(update={"sell_binding": None}), deadline=monotonic() + 1
        ).result(2)
    finally:
        owner.close()
    with open_offline_engine(
        legacy,
        broker=broker,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        state = poll_until(runtime, lambda s: s.state in ("HALTED", "RECOVERY_BLOCKED"))
        assert state.halted and state.grant_id is None
