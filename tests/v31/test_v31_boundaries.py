from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import monotonic

import pytest

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.domain import Money, Price
from auto_trading_system.execution.dispatch_boundary import DispatchPermission
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.authentication import (
    RequestIdentity,
    canonical_target,
    response_preimage,
)
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.protocol.v2.plans import PlanPayload, check_plan_binding
from auto_trading_system.runtime.memory_state import (
    AccountObservation,
    MarketWindow,
    QuotePoint,
    ReservationBook,
)
from auto_trading_system.strategies.entry_sizing import BuyRule, EntryCapacity, size_entry
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.plan_files import PlanFiles
from auto_trading_system.testing.v31_scenarios import example_plan


def test_simulation_composition_is_measured_in_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--simulate", str(tmp_path)]) == 0
    assert '"restart_paused": true' in capsys.readouterr().out


def test_resolving_terminal_keeps_the_no_replay_tombstone() -> None:
    permission = DispatchPermission()
    epoch = permission.allow_after_durable_grant(1)
    assert permission.claim("ord_example001", epoch, monotonic() + 1)
    with pytest.raises(ValueError):
        permission.resolve_terminal("ord_example001", confirmed=False)
    permission.resolve_terminal("ord_example001", confirmed=True)
    assert permission.snapshot().in_flight == ()
    assert not permission.claim("ord_example001", epoch, monotonic() + 1)


def test_memory_owner_bounds_and_missing_state(submission: Submission) -> None:
    with pytest.raises(ValueError):
        MarketWindow(capacity=0)
    with pytest.raises(ValueError):
        ReservationBook(capacity=0)
    window = MarketWindow()
    book = ReservationBook(capacity=1)
    point = QuotePoint(1, 1, Price.parse("10"))
    with ThreadPoolExecutor(max_workers=1) as executor:
        for callback in (window.snapshot, book.snapshot, lambda: window.add(point)):
            with pytest.raises(RuntimeError, match="owner"):
                executor.submit(callback).result()
    with pytest.raises(ValueError):
        window.add(replace(point, sequence=0))
    observation = AccountObservation("snapshot-1", Money.parse("10000"), frozenset(), (), True)
    with pytest.raises(ValueError):
        book.available_cash(replace(observation, coherent=False))
    with pytest.raises(ValueError, match="cancellation"):
        book.reserve(submission.model_copy(update={"action_type": "CANCEL"}), observation)
    book.reserve(submission, observation)
    with pytest.raises(ValueError, match="capacity"):
        book.reserve(
            submission.model_copy(
                update={"client_order_id": "ord_different1", "symbol": "000002.SZ"}
            ),
            observation,
        )


@pytest.mark.parametrize(
    "target", ["/api/v2/trading/a\n", "/api/v2/trading/a?x=%ZZ", "/api/v2/trading/a?x=%FF"]
)
def test_auth_rejects_control_and_encoding_ambiguity(target: str) -> None:
    with pytest.raises(ValueError):
        canonical_target(target)


def test_invalid_auth_and_nested_json() -> None:
    with pytest.raises(ValueError):
        RequestIdentity("n", "k", -1, "nonce", "request")
    with pytest.raises(ValueError):
        response_preimage("request", 999, 1, b"")
    with pytest.raises(ValueError):
        parse_bytes(b"\xff")
    with pytest.raises(ValueError, match="nesting"):
        parse_bytes(b"[" * 34 + b"0" + b"]" * 34)


@pytest.mark.parametrize(
    "mutation",
    [
        "window",
        "allocation_amount",
        "policy",
        "duplicate_symbol",
        "duplicate_allocation",
        "duplicate_rank",
        "timezone",
        "authorization",
        "directions",
    ],
)
def test_plan_rejects_inconsistent_budget_identity_and_window(mutation: str) -> None:
    raw = example_plan().model_dump(mode="json")
    if mutation == "window":
        raw["execution_window"]["stop_new_buy_at"] = raw["execution_window"]["entry_not_before"]
    elif mutation == "allocation_amount":
        raw["allocations"][0]["target_value"] = "999999"
    elif mutation == "policy":
        raw["policy_bundle"][0] = raw["policy_bundle"][1]
    elif mutation.startswith("duplicate"):
        second = dict(raw["allocations"][0])
        if mutation != "duplicate_symbol":
            second["symbol"] = "000002.SZ"
        if mutation == "duplicate_rank":
            second["allocation_id"] = "allocation-2"
        raw["allocations"].append(second)
    elif mutation == "timezone":
        raw["execution_window"]["entry_not_before"] = "2026-09-07T06:40:00+00:00"
    elif mutation == "authorization":
        raw["offline_authorization"]["valid_until"] = raw["execution_window"]["entry_not_before"]
    elif mutation == "directions":
        raw["offline_authorization"]["directions"] = ["BUY", "BUY"]
    with pytest.raises(ValueError):
        PlanPayload.model_validate_json(canonical_bytes(raw))


def test_plan_expiry_and_arming_without_load(tmp_path: Path) -> None:
    plan = example_plan()
    with pytest.raises(ValueError, match="aware"):
        check_plan_binding(
            plan,
            account_id=plan.account_id,
            node_generation=1,
            now=datetime.fromisoformat("2026-09-07T00:00:00"),
        )
    with pytest.raises(ValueError, match="expired"):
        check_plan_binding(
            plan,
            account_id=plan.account_id,
            node_generation=1,
            now=plan.offline_authorization.valid_until,
        )
    with pytest.raises(ValueError, match="loaded"):
        PlanFiles(ArtifactFiles(tmp_path), {}).mark_armed()


def test_buy_rule_and_capacity_invalid_values() -> None:
    with pytest.raises(ValueError):
        BuyRule(0, 100, 1000, Price.parse("0.01"))
    rule = BuyRule(100, 100, 1000, Price.parse("0.01"))
    capacity = EntryCapacity(Money.parse("1000"), Money.parse("5"), 100, 10000, 10000, True)
    with pytest.raises(ValueError):
        size_entry(Price.parse("10"), Price.parse("10"), rule, capacity, participation_bps=10001)
    with pytest.raises(ValueError):
        size_entry(
            Price.parse("10"), Price.parse("10"), rule, replace(capacity, remaining_target=-1)
        )
