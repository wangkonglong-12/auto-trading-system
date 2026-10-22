from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, time, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import (
    OfflineEngine,
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
)
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.journal.apsw_store import APSWCriticalStore, EvidenceTable
from auto_trading_system.journal.critical_models import (
    CriticalStoreError,
    SellAllocation,
    SellBinding,
    Submission,
)
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.strategies.exit_context import (
    ExitLotContext,
    ExitSessionContext,
    SignedExitContext,
    sign_exit_context,
    verify_exit_context,
)
from auto_trading_system.strategies.exit_owner import D1StrategyOwner, ExitQuote, ExitStateGate
from auto_trading_system.strategies.exit_state_store import (
    CAPACITY,
    DurableExitState,
    ExitTransition,
    append_exit_transition,
    load_exit_state,
    read_exit_history,
    state_key,
)
from auto_trading_system.strategies.sell_rules import SellRules


@pytest.fixture
def context(tmp_path: Path) -> ExitSessionContext:
    opening = close_buy_day(tmp_path)
    return fixture_context(tmp_path, opening)


def transition(context: ExitSessionContext, target: int = 100) -> ExitTransition:
    return ExitTransition(
        revision=1,
        previous_hash=None,
        context_hash=context.payload_hash,
        account_id=context.account_id,
        trade_date=context.trade_date,
        lot_id=context.lots[0].lot_id,
        boot_id="test-boot",
        observed_at=D1_NOW,
        state=DurableExitState(
            target_remaining=target,
            fired_rules=("OPEN_OBSERVED",),
            profit_armed=False,
            path_uncertain=True,
        ),
    )


@contextmanager
def armed(
    directory: Path, *, open_price: str = "10", at_1030: bool = False
) -> Iterator[tuple[OfflineEngine, D1StrategyOwner, ExitScenarioClock]]:
    opening = close_buy_day(directory)
    context = fixture_context(directory, opening, open_price=open_price)
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=30) if at_1030 else D1_NOW)
    broker = MockBrokerGateway(clock)
    with open_exit_owner(directory, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        yield runtime, owner, clock


def test_continuous_exit_owner_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--simulate-exit-owner", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["real_trading_enabled"] and not result["afternoon_plan_loaded"]
    assert result["restart_paused"] and result["no_order_state_restored"]
    assert result["time_check_not_repeated"] and result["target_tightened_while_order_active"]
    assert result["stale_quote_cancelled"]
    assert (
        result["first_child_quantity"],
        result["first_fill"],
        result["residual_child_quantity"],
        result["final_remaining"],
    ) == (50, 20, 80, 0)


def test_configured_cost_exit_reaches_mock_broker_once(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    baseline = fixture_context(tmp_path, opening)
    context = baseline.model_copy(
        update={
            "rule_id": "COST_EXIT_V1",
            "sell_rules": SellRules(
                take_profit_pct=Decimal("5"),
                stop_loss_pct=Decimal("3"),
                sell_by=time(14, 30),
            ),
        }
    )
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_exit_owner(tmp_path, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        publish(owner, clock, "9.70", 1)
        pump(owner, lambda: bool(broker.query_orders()) and owner.snapshot().state == "READY")
        assert owner.snapshot().targets == ((context.lots[0].lot_id, 0),)
        assert len(broker.query_orders()) == 1
        assert runtime.kernel.exit_inputs()[2][0].target.rule_id == "COST_EXIT_V1"
        clock.advance(1)
        publish(owner, clock, "9.70", 2)
        owner.poll()
        assert len(broker.query_orders()) == 1


def test_operator_liquidation_tightens_strategy_lot_then_submits_sell(tmp_path: Path) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        owner.request_liquidation()
        publish(owner, clock, "10.00", 1)
        pump(owner, lambda: bool(runtime.broker.query_orders()))

        latest = owner.gate.latest[owner.context.lots[0].lot_id]
        order = runtime.broker.query_orders()[0]
        assert latest.state.target_remaining == 0
        assert "EMERGENCY_LIQUIDATION" in latest.state.fired_rules
        assert order.side == "SELL" and order.order_quantity.value == 100


def test_operator_liquidation_immediately_cancels_existing_order(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    baseline = fixture_context(tmp_path, opening)
    context = baseline.model_copy(
        update={
            "rule_id": "COST_EXIT_V1",
            "sell_rules": SellRules(
                take_profit_pct=Decimal("5"),
                stop_loss_pct=Decimal("3"),
                sell_by=time(14, 30),
            ),
        }
    )
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_exit_owner(tmp_path, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        publish(owner, clock, "9.70", 1)
        pump(owner, lambda: bool(broker.query_orders()) and runtime.kernel.critical_idle)
        active = broker.query_orders()[0]
        assert not active.is_terminal

        owner.request_liquidation()
        pump(owner, lambda: broker.query_orders()[0].is_terminal and runtime.kernel.critical_idle)

        assert broker.query_orders()[0].status.value == "CANCELLED"


def test_configured_rule_is_signed_and_cannot_change_silently(context: ExitSessionContext) -> None:
    rules = SellRules(
        take_profit_pct=Decimal("5"), stop_loss_pct=Decimal("3"), sell_by=time(14, 30)
    )
    configured = ExitSessionContext.model_validate(
        {**context.model_dump(), "rule_id": "COST_EXIT_V1", "sell_rules": rules}
    )
    key = Ed25519PrivateKey.generate()
    signed = sign_exit_context(configured, "test-key", key)
    body = canonical_bytes(signed.model_dump(mode="json"))
    assert verify_exit_context(body, {"test-key": key.public_key()}, now=D1_NOW) == configured
    altered = signed.model_copy(
        update={
            "payload": configured.model_copy(
                update={
                    "sell_rules": SellRules(
                        take_profit_pct=Decimal("6"),
                        stop_loss_pct=Decimal("3"),
                        sell_by=time(14, 30),
                    )
                }
            )
        }
    )
    with pytest.raises(InvalidSignature):
        verify_exit_context(
            canonical_bytes(altered.model_dump(mode="json")),
            {"test-key": key.public_key()},
            now=D1_NOW,
        )


@pytest.mark.parametrize(
    "case", ["valid", "unknown-key", "wrong-key", "tamper", "future", "expired", "naive"]
)
def test_signed_context_provenance(context: ExitSessionContext, case: str) -> None:
    key = Ed25519PrivateKey.generate()
    signed = sign_exit_context(context, "test-key", key)
    keys = {"test-key": key.public_key()}
    now = D1_NOW
    if case == "unknown-key":
        keys = {}
    elif case == "wrong-key":
        keys = {"test-key": Ed25519PrivateKey.generate().public_key()}
    elif case == "tamper":
        signed = signed.model_copy(
            update={"payload": context.model_copy(update={"context_id": "changed"})}
        )
    elif case == "future":
        now = context.issued_at - timedelta(seconds=1)
    elif case == "expired":
        now = context.valid_until
    elif case == "naive":
        now = D1_NOW.replace(tzinfo=None)
    body = canonical_bytes(signed.model_dump(mode="json"))
    if case == "valid":
        assert verify_exit_context(body, keys, now=now) == context
    else:
        with pytest.raises((ValueError, InvalidSignature)):
            verify_exit_context(body, keys, now=now)


@pytest.mark.parametrize(
    "case",
    [
        "utc",
        "wrong-day",
        "window",
        "too-early",
        "too-late",
        "zero-budget",
        "duplicate-lot",
        "zero-price",
        "bounds",
    ],
)
def test_context_rejects_invalid_source_scope(context: ExitSessionContext, case: str) -> None:
    body = context.model_dump(mode="json")
    if case == "utc":
        body["issued_at"] = context.issued_at.astimezone(UTC).isoformat()
    elif case == "wrong-day":
        body["trade_date"] = (D1_NOW + timedelta(days=1)).date().isoformat()
    elif case == "window":
        body["valid_from"] = body["valid_until"]
    elif case == "too-early":
        body["valid_from"] = body["issued_at"] = D1_NOW.replace(hour=9, minute=29).isoformat()
    elif case == "too-late":
        body["stop_new_at"] = context.cancel_by.isoformat()
    elif case == "zero-budget":
        body["max_order_value"] = "0"
    elif case == "duplicate-lot":
        body["lots"] *= 2
    elif case == "zero-price":
        body["lots"][0]["price_tick"] = "0"
    else:
        body["lots"][0]["lower_price"] = "13"
    with pytest.raises(ValidationError):
        ExitSessionContext.model_validate_json(canonical_bytes(body))


def test_all_persisted_fields_are_documented() -> None:
    for model in (
        ExitLotContext,
        ExitSessionContext,
        SignedExitContext,
        DurableExitState,
        ExitTransition,
        ExitQuote,
    ):
        assert all(field.description for field in model.model_fields.values())


def test_state_is_immutable_bounded_and_context_cannot_reset(
    context: ExitSessionContext, tmp_path: Path
) -> None:
    store = APSWCriticalStore(tmp_path / "states.db", context.account_id)
    store.open()
    try:
        assert load_exit_state(store, context) == ()
        first = transition(context)
        assert append_exit_transition(store, first)
        assert not append_exit_transition(store, first)
        second = first.model_copy(
            update={
                "revision": 2,
                "previous_hash": first.payload_hash,
                "state": first.state.model_copy(update={"target_remaining": 50}),
            }
        )
        assert append_exit_transition(store, second)
        assert load_exit_state(store, context) == (first, second)
        with pytest.raises(CriticalStoreError):
            append_exit_transition(store, first.model_copy(update={"boot_id": "conflict"}))
        with pytest.raises(CriticalStoreError):
            load_exit_state(store, context.model_copy(update={"context_id": "attempted-reset"}))
        with pytest.raises(ValidationError):
            append_exit_transition(store, first.model_copy(update={"revision": CAPACITY + 1}))
    finally:
        store.close()


@pytest.mark.parametrize(
    "case",
    ["hole", "wrong-context", "broken-link", "bad-first", "no-predecessor", "unsorted-rules"],
)
def test_corrupt_state_fails_closed(context: ExitSessionContext, tmp_path: Path, case: str) -> None:
    store = APSWCriticalStore(tmp_path / "invalid.db", context.account_id)
    store.open()
    first = transition(context)
    try:
        if case == "bad-first":
            with pytest.raises(ValueError):
                append_exit_transition(
                    store, first.model_copy(update={"previous_hash": first.payload_hash})
                )
        elif case == "no-predecessor":
            with pytest.raises(ValueError):
                append_exit_transition(
                    store,
                    first.model_copy(update={"revision": 2, "previous_hash": first.payload_hash}),
                )
        elif case == "unsorted-rules":
            with pytest.raises(ValidationError):
                DurableExitState(
                    target_remaining=50,
                    fired_rules=("B", "A"),
                    profit_armed=False,
                    path_uncertain=True,
                )
        else:
            record = first.model_copy(
                update={
                    "revision": 2 if case == "hole" else 1,
                    "context_hash": "sha256:" + "a" * 64
                    if case == "wrong-context"
                    else first.context_hash,
                    "previous_hash": first.payload_hash if case == "broken-link" else None,
                }
            )
            store.append_evidence(
                "recovery_checkpoints",
                state_key(first.context_hash, record.revision),
                canonical_bytes(record.model_dump(mode="json")),
            )
            with pytest.raises(ValueError):
                read_exit_history(store, first.context_hash)
    finally:
        store.close()


@pytest.mark.parametrize(
    "case", ["stale", "lunch", "cutoff", "not-tradable", "future-clock", "out-of-order"]
)
def test_quote_boundaries_do_not_create_orders(tmp_path: Path, case: str) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        quote = ExitQuote(
            symbol="000001.SZ",
            sequence=1,
            observed_at=clock.instant,
            received_ns=clock.nanoseconds,
            last_price="10.7",
            bid_price="10",
            tradable=True,
        )
        if case == "lunch":
            clock.instant = clock.instant.replace(hour=12)
            quote = quote.model_copy(update={"observed_at": clock.instant})
        elif case == "cutoff":
            clock.instant = owner.context.stop_new_at
            quote = quote.model_copy(update={"observed_at": clock.instant})
        elif case == "not-tradable":
            quote = quote.model_copy(update={"tradable": False})
        elif case == "future-clock":
            quote = quote.model_copy(update={"received_ns": clock.nanoseconds + 1})
        owner.quote(quote)
        if case == "stale":
            clock.advance(3)
        elif case == "out-of-order":
            owner.quote(quote)
        for _ in range(10):
            owner.poll()
            sleep(0.001)
        assert not runtime.broker.query_orders()
        assert owner.snapshot().revision == 0
        if case in ("future-clock", "out-of-order"):
            assert owner.snapshot().state == "FAILED" and runtime.kernel.snapshot().halted


def test_quote_cache_is_bounded_and_owner_is_single_thread(tmp_path: Path) -> None:
    with armed(tmp_path) as (_, owner, clock):
        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            pytest.raises(RuntimeError, match="single owner"),
        ):
            pool.submit(owner.poll).result()
        unknown = ExitQuote(
            symbol="600000.SH",
            sequence=1,
            observed_at=clock.instant,
            received_ns=clock.nanoseconds,
            last_price="10",
            bid_price="10",
            tradable=True,
        )
        with pytest.raises(ValueError, match="outside"):
            owner.quote(unknown)
        for n in range(20):
            clock.advance(1)
            publish(owner, clock, "10", n + 1)
            pump(owner, lambda: owner.snapshot().state == "READY")
        assert len(owner.gate.quotes) == 1 and owner.snapshot().revision == 1


@pytest.mark.parametrize("case", ["target", "fired", "scope", "profit", "path"])
def test_restored_transitions_never_weaken(
    context: ExitSessionContext, tmp_path: Path, case: str
) -> None:
    # Feed structurally valid but semantically invalid history through the public
    # restoration path, not private reducer internals.
    from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference

    store = APSWCriticalStore(tmp_path / "critical/journal-v2.db", context.account_id)
    store.open()
    first = transition(context, 50).model_copy(
        update={
            "state": DurableExitState(
                target_remaining=50,
                fired_rules=("OPEN_OBSERVED",),
                profit_armed=True,
                path_uncertain=True,
            )
        }
    )
    second = first.model_copy(update={"revision": 2, "previous_hash": first.payload_hash})
    if case == "scope":
        second = second.model_copy(update={"account_id": "another"})
    else:
        cases: dict[str, dict[str, object]] = {
            "target": {"target_remaining": 51},
            "fired": {"fired_rules": ()},
            "profit": {"profit_armed": False},
            "path": {"path_uncertain": False},
        }
        second = second.model_copy(update={"state": first.state.model_copy(update=cases[case])})
    try:
        append_exit_transition(store, first)
        append_exit_transition(store, second)
    finally:
        store.close()
    opening = OpeningReference(
        trade_date=D1_NOW.date() - timedelta(days=1), payload_hash=context.opening_hash
    )
    gate = ExitStateGate(context, clock=lambda: D1_NOW)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        opening_reference=opening,
        exit_context=context,
        exit_state_guard=gate.allows,
        broker_opening_quantity=100,
    ) as runtime:
        owner = D1StrategyOwner(runtime.kernel, gate)
        until = monotonic() + 2
        while owner.poll().state != "FAILED" and monotonic() < until:
            sleep(0.001)
        assert owner.snapshot().state == "FAILED" and runtime.kernel.snapshot().halted


@pytest.mark.parametrize("case", ["write-failure", "timeout", "halt"])
def test_state_write_failure_late_success_or_halt_cannot_send(tmp_path: Path, case: str) -> None:
    opening = close_buy_day(tmp_path)
    context = fixture_context(tmp_path, opening, open_price="10.3")
    started, release = Event(), Event()

    class DelayedStore(APSWCriticalStore):
        def append_evidence(self, table: EvidenceTable, key: str, body: bytes) -> bool:
            if key.startswith("exit-state:"):
                started.set()
                if case == "write-failure":
                    raise OSError("injected disk fault")
                if not release.wait(3):
                    raise TimeoutError("test release was not signalled")
            return super().append_evidence(table, key, body)

    clock = ExitScenarioClock()
    gate = ExitStateGate(context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds)
    with open_offline_engine(
        tmp_path,
        clock=lambda: clock.instant,
        opening_reference=opening,
        exit_context=context,
        exit_state_guard=gate.allows,
        broker_opening_quantity=100,
        journal_factory=lambda path, account: CriticalJournalOwner(
            lambda: DelayedStore(path, account)
        ),
    ) as runtime:
        try:
            owner = D1StrategyOwner(runtime.kernel, gate)
            runtime.kernel.start_recovery()
            wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
            pump(owner, lambda: owner.snapshot().state == "READY")
            runtime.confirmed_arm(directions=("SELL",))
            publish(owner, clock, "10", 1)
            owner.poll()
            assert started.wait(1)
            if case == "halt":
                runtime.kernel.emergency_halt("TEST_HALT_DURING_STATE")
            until = monotonic() + 2
            while owner.poll().state != "FAILED" and monotonic() < until:
                sleep(0.005)
            assert owner.snapshot().state == "FAILED" and runtime.kernel.snapshot().halted
            release.set()
            assert not runtime.broker.query_orders()
            for _ in range(10):
                owner.poll()
                sleep(0.001)
            assert not runtime.broker.query_orders() and owner.gate.blocked
        finally:
            release.set()


def test_exit_grant_is_sell_only_and_has_no_buy_budget(tmp_path: Path) -> None:
    with armed(tmp_path) as (runtime, owner, _):
        assert runtime.kernel.plans.plans.loaded() is None
        assert runtime.kernel.limits.max_buy_value == "0"
        with pytest.raises(ValueError):
            runtime.kernel.submit(runtime.buy(), valid_until=monotonic() + 1)
        assert owner.context.rule_id == "T1_EXIT_BASELINE_V3"


def test_new_state_binding_retains_legacy_j1_serialization(submission: Submission) -> None:
    child = submission.model_copy(
        update={
            "side": "SELL",
            "sell_binding": SellBinding(
                strategy_id="test-strategy",
                opening_hash="sha256:" + "b" * 64,
                allocations=(SellAllocation(lot_id="test-lot", quantity=submission.quantity),),
            ),
        }
    )
    for value in (submission, child):
        body = value.model_dump(mode="json")
        assert "context_type" not in body
        if value.sell_binding:
            assert "state_hash" not in body["sell_binding"]
        assert Submission.model_validate_json(canonical_bytes(body)).model_dump(mode="json") == body
    with pytest.raises(ValidationError):
        Submission.model_validate_json(
            child.model_copy(update={"context_type": "EXIT_SESSION"}).model_dump_json()
        )


def test_active_state_binding_is_exact_and_cannot_be_replayed(tmp_path: Path) -> None:
    with armed(tmp_path, open_price="10.3") as (runtime, owner, clock):
        runtime.broker.queue_submit(fill_scenario(50, full=True))
        publish(owner, clock, "10", 1)
        pump(
            owner,
            lambda: runtime.kernel.snapshot().lots[0].remaining == 50
            and runtime.kernel.critical_idle,
        )
        item = runtime.kernel.exit_inputs()[2][0]
        assert (
            item.sell_binding
            and item.sell_binding.state_hash
            == owner.gate.latest[owner.context.lots[0].lot_id].payload_hash
        )
        assert owner.gate.allows(item)
        altered = item.model_copy(
            update={
                "sell_binding": item.sell_binding.model_copy(
                    update={"state_hash": "sha256:" + "f" * 64}
                )
            }
        )
        assert not owner.gate.allows(altered)
        altered = item.model_copy(update={"limit_price": "10.001"})
        assert not owner.gate.allows(altered)
        with pytest.raises(ValueError):
            runtime.kernel.submit(item, valid_until=monotonic() + 1)


@pytest.mark.parametrize(
    "case",
    [
        "fresh",
        "stale-wall",
        "stale-monotonic",
        "future",
        "lunch",
        "cutoff",
        "closed",
        "utc",
        "zero",
    ],
)
def test_quote_freshness_without_grant_expiry_masking(
    context: ExitSessionContext, case: str
) -> None:
    clock = ExitScenarioClock()
    gate = ExitStateGate(context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds)
    quote = ExitQuote(
        symbol="000001.SZ",
        sequence=1,
        observed_at=clock.instant,
        received_ns=clock.nanoseconds,
        last_price="10",
        bid_price="10",
        tradable=True,
    )
    if case == "stale-wall":
        quote = quote.model_copy(update={"observed_at": clock.instant - timedelta(seconds=3)})
    elif case == "stale-monotonic":
        clock.nanoseconds += 3_000_000_000
    elif case == "future":
        quote = quote.model_copy(update={"received_ns": clock.nanoseconds + 1})
    elif case in ("lunch", "cutoff"):
        clock.instant = clock.instant.replace(hour=12) if case == "lunch" else context.stop_new_at
        quote = quote.model_copy(update={"observed_at": clock.instant})
    elif case == "closed":
        quote = quote.model_copy(update={"tradable": False})
    elif case in ("utc", "zero"):
        quote = (
            quote.model_copy(update={"observed_at": clock.instant.astimezone(UTC)})
            if case == "utc"
            else quote.model_copy(update={"bid_price": "0"})
        )
        with pytest.raises(ValidationError):
            ExitQuote.model_validate_json(quote.model_dump_json())
        return
    assert gate.fresh(quote) == (case == "fresh")


@pytest.mark.parametrize("case", ["opening", "calendar", "guard", "account"])
def test_context_must_bind_installed_engine(
    context: ExitSessionContext, tmp_path: Path, case: str
) -> None:
    from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference

    opening = OpeningReference(
        trade_date=D1_NOW.date() - timedelta(days=1), payload_hash=context.opening_hash
    )
    bad = context
    if case in ("opening", "calendar"):
        bad = context.model_copy(update={case + "_hash": "sha256:" + "f" * 64})
    elif case == "account":
        bad = context.model_copy(update={"account_id": "another-account"})
    gate = ExitStateGate(bad, clock=lambda: D1_NOW)
    with (
        pytest.raises(ValueError),
        open_offline_engine(
            tmp_path,
            clock=lambda: D1_NOW,
            opening_reference=opening,
            exit_context=bad,
            exit_state_guard=None if case == "guard" else gate.allows,
        ),
    ):
        pytest.fail("unbound exit context was accepted")


def test_missing_state_referenced_by_recovered_j1_halts_before_new_authorization(
    tmp_path: Path,
) -> None:
    opening = close_buy_day(tmp_path)
    context = fixture_context(tmp_path, opening, open_price="10.3")
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_exit_owner(tmp_path, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        broker.queue_submit(fill_scenario(50, full=True))
        publish(owner, clock, "10", 1)
        pump(
            owner,
            lambda: runtime.kernel.snapshot().lots[0].remaining == 50
            and runtime.kernel.critical_idle,
        )

    class HiddenStateStore(APSWCriticalStore):
        def read_evidence(self, table: EvidenceTable, key: str) -> bytes | None:
            if key.startswith("exit-state:"):
                return None  # Inject missing read evidence; never delete stored data.
            return super().read_evidence(table, key)

    gate = ExitStateGate(context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds)
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        opening_reference=opening,
        exit_context=context,
        exit_state_guard=gate.allows,
        broker_opening_quantity=100,
        journal_factory=lambda path, account: CriticalJournalOwner(
            lambda: HiddenStateStore(path, account)
        ),
    ) as runtime:
        owner = D1StrategyOwner(runtime.kernel, gate)
        runtime.kernel.start_recovery()
        until = monotonic() + 2
        while owner.poll().state != "FAILED" and monotonic() < until:
            sleep(0.001)
        assert owner.snapshot().state == "FAILED"
        assert runtime.kernel.snapshot().halted and runtime.kernel.snapshot().grant_id is None
        assert len(broker.query_orders()) == 1


def test_quantity_rounding_and_odd_lot_requires_actual_full_liquidation(tmp_path: Path) -> None:
    from auto_trading_system.domain import Price
    from auto_trading_system.strategies.exit_execution import exit_child
    from auto_trading_system.strategies.exit_v3 import ExitDecision, ExitState

    opening = close_buy_day(tmp_path)
    context = fixture_context(tmp_path, opening)
    context = context.model_copy(
        update={
            "max_order_quantity": 30,
            "lots": (context.lots[0].model_copy(update={"sell_step": 100}),),
        }
    )
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_exit_owner(tmp_path, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        publish(owner, clock, "10.7", 1)
        pump(owner, lambda: owner.snapshot().revision == 1 and owner.snapshot().state == "READY")
        assert not broker.query_orders()  # 30 shares is neither a full lot nor a full liquidation.
        item = exit_child(
            lot=runtime.kernel.snapshot().lots[0],
            decision=ExitDecision(
                ExitState(0, frozenset({"OPEN_OBSERVED", "TAKE_PROFIT"})), (), True
            ),
            account_id=context.account_id,
            trade_date=context.trade_date,
            plan_hash=context.payload_hash,
            opening_hash=context.opening_hash,
            boot_id=runtime.kernel.boot_id,
            permission_epoch=runtime.kernel.snapshot().permission_epoch,
            limit_price=Price.parse("10"),
            quantity_limit=30,
            broker_sellable=100,
        )
        assert item and item.sell_binding
        item = item.model_copy(
            update={
                "context_type": "EXIT_SESSION",
                "sell_binding": item.sell_binding.model_copy(
                    update={"state_hash": owner.gate.latest[context.lots[0].lot_id].payload_hash}
                ),
            }
        )
        # Even bypassing the driver's rounding, the engine independently checks
        # actual remaining holdings before allowing an odd-lot liquidation.
        with pytest.raises(ValueError, match="gate rejected"):
            runtime.kernel.submit(item, valid_until=monotonic() + 1)
        assert not broker.query_orders()


def test_cutoff_cancels_even_with_no_fresh_quote(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    context = fixture_context(tmp_path, opening, open_price="10.3")
    clock = ExitScenarioClock(D1_NOW.replace(hour=14, minute=55, second=29))
    broker = MockBrokerGateway(clock)
    with open_exit_owner(tmp_path, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        publish(owner, clock, "10", 1)
        pump(owner, lambda: bool(broker.query_orders()) and runtime.kernel.critical_idle)
        clock.advance(1)
        pump(owner, lambda: broker.query_orders()[0].is_terminal and runtime.kernel.critical_idle)
        assert len(broker.query_orders()) == 1
