"""Fictional process input, broker durability and plan recovery safety boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_node_authorization import access as access_fixture
from test_node_processes import node as node_fixture
from test_paper_process import configure

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.domain import Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.gateways.broker.models import OrderSide, SubmitOrder
from auto_trading_system.gateways.broker.paper_state import PaperBrokerState, PaperStateFile
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.runtime.node_engine import open_node_engine
from auto_trading_system.runtime.node_plan_recovery import (
    PlanRecoveryReference,
    restore_reference,
    save_reference,
)
from auto_trading_system.runtime.paper_runtime import PaperRuntime
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.plan_files import PlanFiles

access = access_fixture
node_fixture = node_fixture


@dataclass
class Clock:
    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(DEMO_NOW)


def command() -> SubmitOrder:
    return SubmitOrder(
        "ord_paper_0001",
        SecurityCode.parse("000001.SZ"),
        OrderSide.BUY,
        Quantity(100),
        Price.parse("10"),
        "TESTPAPER",
    )


def broker(path: Path) -> tuple[MockBrokerGateway, PaperStateFile]:
    state = PaperStateFile(path, "paper-test", "10000")
    gateway = MockBrokerGateway(Clock(), paper_state=state)
    gateway.start(lambda _: None)
    gateway.submit_order(command())
    return gateway, state


def test_paper_facts_committed_before_callbacks_and_restored(tmp_path: Path) -> None:
    state = PaperStateFile(tmp_path / "paper.json", "paper-test", "10000")
    gateway = MockBrokerGateway(Clock(), paper_state=state)
    checked = []

    def callback(_: object) -> None:
        saved = state.load()
        assert saved and len(saved.trades) == 1 and saved.orders[0].filled_quantity.value == 100
        checked.append(True)

    gateway.start(callback)
    gateway.submit_order(command())
    assert checked
    restored = MockBrokerGateway(Clock(), paper_state=state)
    restored.start(lambda _: None)
    assert restored.query_orders() == gateway.query_orders()
    assert restored.query_trades() == gateway.query_trades()


@pytest.mark.parametrize("mutation", ["sequence", "request", "identity", "orphan", "total"])
def test_paper_corruption_rejected(tmp_path: Path, mutation: str) -> None:
    _, file = broker(tmp_path / "paper.json")
    value = file.load()
    assert value
    changes: dict[str, Any] = {}
    if mutation == "sequence":
        changes["order_sequence"] = 1
    elif mutation == "request":
        changes["request_sequence"] = 1
    elif mutation == "identity":
        changes["orders"] = (replace(value.orders[0], broker_order_id="MOCK0000000000009999"),)
    elif mutation == "orphan":
        changes["trades"] = (replace(value.trades[0], client_order_id="ord_missing1"),)
    else:
        changes["trades"] = ()
    raw = value.model_copy(update=changes).model_dump(mode="json")
    with pytest.raises(ValueError):
        PaperBrokerState.model_validate_json(canonical_bytes(raw))


def test_paper_identity_links_and_failed_replace_keep_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, file = broker(tmp_path / "paper.json")
    value = file.load()
    assert value
    with pytest.raises(ValueError, match="identity"):
        PaperStateFile(file.path, "paper-other", "10000").load()
    with pytest.raises(ValueError, match="identity"):
        file.save(value.model_copy(update={"initial_cash": "999"}))
    alias = tmp_path / "hardlink.json"
    alias.hardlink_to(file.path)
    with pytest.raises(ValueError, match="regular"):
        file.load()
    alias.unlink()  # Only the link created by this test, never the original file.
    before = file.path.read_bytes()

    def failed_replace(*_: object) -> None:
        raise OSError("fictional disk failure")

    monkeypatch.setattr(
        "auto_trading_system.gateways.broker.paper_state.os.replace", failed_replace
    )
    with pytest.raises(OSError):
        file.save(value)
    assert file.path.read_bytes() == before


@pytest.mark.parametrize(
    "update",
    [
        {"as_of": "2026-09-07T06:41:00Z"},
        {"prices": {"000001.SZ": "0"}},
        {"prices": {"000002.SZ": "10"}},
        {"entry_policy": None},
        {"phase": "EXIT"},
        {
            "limits": {
                "mode": "LIVE",
                "max_buy_value": "0",
                "max_order_value": "0",
                "max_positions": 0,
            }
        },
        {
            "limits": {
                "mode": "PAPER",
                "max_buy_value": "100001",
                "max_order_value": "1",
                "max_positions": 1,
            }
        },
    ],
)
def test_session_rejects_implicit_or_excessive_inputs(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    update: dict[str, Any],
) -> None:
    config, _, _ = configure(access)
    raw = config.model_dump(mode="json")
    raw["paper_session"].update(update)
    with pytest.raises(ValueError):
        NodeProcessConfig.model_validate_json(canonical_bytes(raw))


def test_paper_account_t_plus_one_and_default_requires_composition(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    tmp_path: Path,
) -> None:
    config, _, _ = configure(access)
    runtime = PaperRuntime(config, lambda: DEMO_NOW)
    assert not runtime.targets() and runtime.status()["driver_state"] == "WAITING_PLAN"
    gateway, _ = broker(tmp_path / "paper.json")
    today = runtime.account(gateway)
    assert today.cash == "9000.00" and today.positions[0].sellable == 0
    tomorrow = PaperRuntime(config, lambda: DEMO_NOW + timedelta(days=1)).account(gateway)
    assert tomorrow.positions[0].sellable == 100
    with pytest.raises(ValueError, match="future"):
        PaperRuntime(config, lambda: DEMO_NOW - timedelta(days=1)).account(gateway)
    with pytest.raises(ValueError, match="composition"), open_node_engine(config, lambda: DEMO_NOW):
        pass


def test_restored_plan_is_ownership_only_and_scope_bound(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    config, plan, _ = configure(access)
    plans = PlanFiles(ArtifactFiles(config.data_root / "artifacts"), config.public_keys())
    body = canonical_bytes(plan.model_dump(mode="json"))
    arguments = {
        "account_id": config.account_id,
        "node_generation": 1,
        "now": DEMO_NOW.replace(hour=15, minute=30),
    }
    assert plans.restore_for_recovery(body, **arguments) == plan  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="frozen"):
        plans.clear_unarmed()
    for updates in (
        {"account_id": "paper-other"},
        {"node_generation": 2},
        {"now": DEMO_NOW + timedelta(days=1)},
    ):
        with pytest.raises(ValueError, match="scope"):
            plans.restore_for_recovery(body, **(arguments | updates))  # type: ignore[arg-type]
    # The ordinary activation path still enforces cutoff on the same signed plan.
    with pytest.raises(ValueError):
        plans.load(body, **arguments)  # type: ignore[arg-type]


def test_recovery_reference_limits_and_prior_day_ignored(tmp_path: Path) -> None:
    from unittest.mock import Mock

    path, plans = tmp_path / "reference.json", Mock()
    restore_reference(path, plans, DEMO_NOW.date())
    reference = PlanRecoveryReference(
        lookup_hash=digest(b"lookup"), artifact_hash=None, trade_date=DEMO_NOW.date()
    )
    save_reference(path, reference)
    restore_reference(path, plans, DEMO_NOW.date())
    restore_reference(path, plans, (DEMO_NOW + timedelta(days=1)).date())
    plans.restore_for_recovery.assert_not_called()
    with pytest.raises(ValueError, match="future"):
        restore_reference(path, plans, (DEMO_NOW - timedelta(days=1)).date())
    path.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError, match="capacity"):
        restore_reference(path, plans, DEMO_NOW.date())
