"""Normal CLI, actual child process, authenticated approvals and durable Mock facts."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_node_authorization import access as access_fixture
from test_node_authorization import confirm, load_plan, login, preview
from test_node_processes import eventually, recovered, rpc
from test_node_processes import node as node_fixture

from auto_trading_system.apps.local_console.node_app import _grant_for
from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.apps.trade_runtime.entry_simulation import entry_fixture
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.config.paper_session import PaperSession
from auto_trading_system.gateways.broker.paper_state import PaperStateFile
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope, sign_plan
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.exit_context import (
    ExitLotContext,
    ExitSessionContext,
    sign_exit_context,
)
from auto_trading_system.strategies.sell_rules import SellRules

access = access_fixture
node_fixture = node_fixture


@contextmanager
def normal_process(path: Path, component: str = "engine") -> Iterator[subprocess.Popen[str]]:
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "auto_trading_system.apps.node_process",
            "--config",
            str(path),
            component,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout
        with ThreadPoolExecutor(1) as worker:
            try:
                line = worker.submit(child.stdout.readline).result(timeout=12)
            except TimeoutError:
                child.terminate()
                child.wait(timeout=10)
                raise
        result = json.loads(line)
        assert result.get("component") == component, result
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        if sys.exc_info()[0] is not None:
            print(
                "child",
                child.returncode,
                child.stdout.read() if child.stdout else "",
                child.stderr.read() if child.stderr else "",
            )
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()


def configure(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> tuple[NodeProcessConfig, PlanEnvelope, Ed25519PrivateKey]:
    base, path, _, _ = access
    payload, policy = entry_fixture()
    private = Ed25519PrivateKey.generate()
    plan = sign_plan(
        payload.model_copy(update={"account_id": base.account_id}), "paper-key", private
    )
    session = PaperSession(
        label="FICTIONAL_ACCEPTANCE_ONLY",
        as_of=DEMO_NOW,
        phase="ENTRY",
        limits=EngineLimits(
            max_buy_value="1020",
            max_order_value="1005",
            max_positions=3,
            max_order_quantity=100,
        ),
        prices={"000001.SZ": "10"},
        entry_policy=policy,
    )
    raw = base.model_dump(mode="json")
    raw.update(
        {
            "paper_session": session.model_dump(mode="json"),
            "policies": [p.model_dump(mode="json") for p in plan.payload.policy_bundle],
            "trusted_plan_keys": {"paper-key": private.public_key().public_bytes_raw().hex()},
        }
    )
    config = NodeProcessConfig.model_validate_json(canonical_bytes(raw))
    path.write_bytes(canonical_bytes(raw))
    return config, plan, private


def test_normal_process_buys_and_restart_reconciles_without_rebuy(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    exercise_normal_process(access)


def exercise_normal_process(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    *,
    prepare: Callable[[NodeProcessConfig, PlanEnvelope], NodeProcessConfig] = lambda c, _: c,
    plan_loader: Callable[[NodeProcessConfig, PlanEnvelope, Any], None] = load_plan,
    upload_daily: Callable[[Path], None] = lambda _: None,
) -> dict[str, Any]:
    """Reusable actual-process acceptance, with optional real HTTPS peer hooks."""
    config, plan, private = configure(access)
    _, path, _, password = access
    config = prepare(config, plan)
    path.write_bytes(canonical_bytes(config.model_dump(mode="json")))
    with normal_process(path):
        agent = rpc(config, "agent", "engine")
        recovered(agent)
        plan_loader(config, plan, agent)
        auth = rpc(config, "operator", "authorization")
        session = login(auth, password)
        confirm(auth, session, preview(auth, session))
        confirm(
            auth,
            session,
            preview(
                auth,
                session,
                "ARM",
                max_buy_value="1020",
                max_order_value="1005",
                max_positions=3,
            ),
        )
        eventually(lambda: agent.invoke("STATUS")["positions"] != [])
        assert agent.invoke("STATUS")["order_count"] == 1
        rpc(config, "operator", "control").invoke("HALT")
        eventually(lambda: agent.invoke("STATUS")["halted"] is True)
        rpc(config, "operator", "control").invoke("STOP")
    state_file = PaperStateFile(
        config.data_root / "critical/paper-broker.json", config.account_id, config.mock_cash
    )
    state = state_file.load()
    assert state and len(state.trades) == 1
    with normal_process(path):
        agent = rpc(config, "agent", "engine")
        recovered(agent)
        plan_loader(config, plan, agent)
        status = agent.invoke("STATUS")
        assert status["halted"] and status["recovery"] == "CLEAR"
        assert status["positions"] and status["order_count"] == 1
        lot_id = str(cast(list[dict[str, Any]], status["positions"])[0]["lot_id"])
        auth = rpc(config, "operator", "authorization")
        session = login(auth, password)
        confirm(auth, session, preview(auth, session))
        confirm(
            auth,
            session,
            preview(
                auth,
                session,
                "ARM",
                max_buy_value="1020",
                max_order_value="1005",
                max_positions=3,
            ),
        )
        assert state_file.load() == state
        rpc(config, "operator", "control").invoke("STOP")
    raw = config.model_dump(mode="json")
    raw["paper_session"]["as_of"] = DEMO_NOW.replace(hour=15, minute=30).isoformat()
    path.write_bytes(canonical_bytes(raw))
    with normal_process(path):
        agent = rpc(config, "agent", "engine")
        recovered(agent)
        exported = agent.invoke("EXPORT_DAILY")
        assert exported["batch_id"]
        closing = rpc(config, "operator", "control").invoke("SEAL_DAY")
        assert closing["trade_date"] == DEMO_NOW.date().isoformat()
        upload_daily(path)
        rpc(config, "operator", "control").invoke("STOP")
    day = DEMO_NOW.replace(day=8, hour=9, minute=35)
    context = ExitSessionContext(
        context_id="paper-exit",
        account_id=config.account_id,
        strategy_id=config.strategy_id,
        trade_date=day.date(),
        opening_hash=str(closing["payload_hash"]),
        calendar_hash=digest(canonical_bytes(config.calendar.model_dump(mode="json"))),
        rule_id="COST_EXIT_V1",
        sell_rules=SellRules(
            take_profit_pct=Decimal("5.00"),
            stop_loss_pct=Decimal("3.00"),
            sell_by=time(14, 30),
        ),
        issued_at=day.replace(minute=30),
        valid_from=day.replace(minute=30),
        stop_new_at=day.replace(hour=14, minute=55),
        cancel_by=day.replace(hour=14, minute=56),
        valid_until=day.replace(hour=15, minute=0),
        max_order_value="2000",
        max_order_quantity=100,
        lots=(
            ExitLotContext(
                lot_id=lot_id,
                symbol="000001.SZ",
                cost="10",
                previous_close="10",
                open_price="10.1",
                price_tick="0.01",
                lower_price="9",
                upper_price="11",
                sell_step=10,
                allow_odd_liquidation=True,
                source_hash=digest(b"fictional context"),
            ),
        ),
    )
    raw["paper_session"].update(
        {
            "phase": "EXIT",
            "as_of": day.isoformat(),
            "entry_policy": None,
            "opening": closing,
            "exit_context": sign_exit_context(context, "paper-key", private).model_dump(
                mode="json"
            ),
            "prices": {"000001.SZ": "10.7"},
            "limits": {
                "mode": "PAPER",
                "max_buy_value": "0",
                "max_order_value": "2000",
                "max_positions": 3,
                "max_order_quantity": 100,
            },
        }
    )
    exit_config = NodeProcessConfig.model_validate_json(canonical_bytes(raw))
    assert _grant_for(exit_config) == {
        "directions": ["SELL"],
        "max_buy_value": "0",
        "max_order_value": "2000",
        "max_positions": 3,
        "duration_seconds": 60,
    }
    path.write_bytes(canonical_bytes(raw))
    with normal_process(path):
        agent = rpc(config, "agent", "engine")
        recovered(agent)
        paper_status = cast(dict[str, Any], agent.invoke("STATUS")["paper_session"])
        assert paper_status["rule_id"] == "COST_EXIT_V1"
        assert context.sell_rules is not None
        assert paper_status["sell_rules_hash"] == digest(
            canonical_bytes(context.sell_rules.model_dump(mode="json"))
        )
        assert (
            cast(list[dict[str, Any]], agent.invoke("STATUS")["positions"])[0]["remaining"] == 100
        )
        auth = rpc(config, "operator", "authorization")
        session = login(auth, password)
        confirm(auth, session, preview(auth, session))
        confirm(
            auth,
            session,
            preview(
                auth,
                session,
                "ARM",
                directions=["SELL"],
                max_buy_value="0",
                max_order_value="2000",
                max_positions=3,
            ),
        )
        eventually(
            lambda: cast(list[dict[str, Any]], agent.invoke("STATUS")["positions"])[0]["remaining"]
            == 0
        )
        assert agent.invoke("STATUS")["order_count"] == 1
        rpc(config, "operator", "control").invoke("STOP")
    state = state_file.load()
    assert state and len(state.trades) == 2
    assert [t.side.value for t in state.trades] == ["BUY", "SELL"]
    raw["paper_session"]["as_of"] = day.replace(hour=15, minute=30).isoformat()
    path.write_bytes(canonical_bytes(raw))
    with normal_process(path):
        agent = rpc(config, "agent", "engine")
        recovered(agent)
        assert agent.invoke("STATUS")["halted"]
        next_export = agent.invoke("EXPORT_DAILY")
        assert next_export["batch_id"] != exported["batch_id"]
        assert rpc(config, "operator", "control").invoke("SEAL_DAY")["trade_date"] == "2026-09-08"
        upload_daily(path)
        rpc(config, "operator", "control").invoke("STOP")
    return {
        "account_id": config.account_id,
        "batch_ids": [exported["batch_id"], next_export["batch_id"]],
        "buy_trades": 1,
        "sell_trades": 1,
        "remaining": 0,
        "restart_paused": True,
        "halt_verified": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "real-account"),
        ("operator_access", None),
        ("broker", "QMT"),
    ],
)
def test_paper_config_cannot_select_live_or_unauthenticated_execution(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    field: str,
    value: object,
) -> None:
    config, _, _ = configure(access)
    raw = config.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValueError):
        NodeProcessConfig.model_validate_json(canonical_bytes(raw))
