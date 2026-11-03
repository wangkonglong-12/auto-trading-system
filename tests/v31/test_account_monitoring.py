"""Whole-account collection, durable minute delivery and restart regressions."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from test_tail_feed import node_config

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.gateways.broker.account_monitoring import read_observation
from auto_trading_system.protocol.account_monitoring import (
    Asset,
    Batch,
    Observation,
    Position,
    Receipt,
    Snapshot,
    Trade,
    fingerprint,
)
from auto_trading_system.sync.account_store import AccountStore, publish_observation
from auto_trading_system.sync.account_upload import AccountHttp, AccountUploader

NOW = datetime.fromisoformat("2026-11-03T10:01:00+08:00")


def trade(identity="buy-1", side="BUY", day=NOW):
    return Trade(
        trade_id=identity,
        symbol="600000.SH",
        side=side,
        quantity=100,
        price="10.00000000",
        amount="1000.00",
        traded_at=day,
    )


def observation(positions=True, trades=(), now=NOW):
    return Observation(
        asset_observed_at=now,
        positions_observed_at=now,
        captured_at=now,
        asset=Asset(
            total_asset="2000.00",
            available_cash="1000.00",
            frozen_cash="0.00",
            market_value="1000.00" if positions else "0.00",
        ),
        positions=(
            Position(
                symbol="600000.SH",
                quantity=100,
                sellable_quantity=100,
                frozen_quantity=0,
                market_value="1000.00",
            ),
        )
        if positions
        else (),
        trades=trades,
    )


def source(count=1):
    identity = {"account_id": "private-account", "account_type": 2}
    asset = SimpleNamespace(
        **identity, total_asset=2000.0, cash=1000.0, frozen_cash=0.0, market_value=1000.0
    )
    positions = [
        SimpleNamespace(
            **identity,
            stock_code=f"{600000 + i:06d}.SH",
            volume=100,
            can_use_volume=100,
            frozen_volume=0,
            market_value=1000.0,
        )
        for i in range(count)
    ]
    fill = SimpleNamespace(
        **identity,
        stock_code="600000.SH",
        traded_id="manual-fill",
        order_type=23,
        traded_volume=100,
        traded_price=10.0,
        traded_amount=1000.0,
        traded_time=int(NOW.timestamp()),
    )
    return {
        "query_stock_asset": asset,
        "query_stock_positions": positions,
        "query_stock_trades": [fill],
    }


def collect(data):
    return read_observation(data.__getitem__, "private-account", lambda: NOW, buy=23, sell=24)


def test_complete_source_includes_manual_fills_and_more_than_100_positions():
    data = source(150)
    result = collect(data)
    assert len(result.positions) == 150
    assert result.trades[0].trade_id == "manual-fill"
    assert "private-account" not in result.model_dump_json()
    data["query_stock_positions"] = []
    assert collect(data).positions == ()


@pytest.mark.parametrize("failure", ["none", "identity", "duplicate", "quantity", "old-trade"])
def test_failed_or_ambiguous_source_never_becomes_empty(failure):
    data = source()
    if failure == "none":
        data["query_stock_positions"] = None
    elif failure == "identity":
        data["query_stock_positions"][0].account_id = "another-account"
    elif failure == "duplicate":
        data["query_stock_positions"] *= 2
    elif failure == "quantity":
        data["query_stock_positions"][0].volume = -1
    else:
        data["query_stock_trades"][0].traded_time -= 86400
    with pytest.raises(ValueError):
        collect(data)


def test_restart_clear_and_rebuy_preserve_fills(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    store = AccountStore(path, "node", "account")
    store.capture(observation(trades=(trade(),)))
    first = store.batch()
    store.close()
    store = AccountStore(path, "node", "account")
    assert store.batch() == first
    store.capture(observation(False, (trade(), trade("sell-1", "SELL"))))
    assert not store.batch().snapshot.observation.positions
    store.capture(observation(True, (trade(), trade("sell-1", "SELL"), trade("buy-2"))))
    batch = store.batch()
    assert batch.snapshot.sequence == 3
    assert len(batch.trades) == 3
    store.acknowledge(batch, Receipt(batch_hash=fingerprint(batch), received_at=NOW))
    store.capture(observation(trades=(trade(), trade("sell-1", "SELL"), trade("buy-2"))))
    assert store.batch().trades == ()
    store.close()


def test_conflicting_fills_roll_back_and_wrong_ack_does_not_drop_pending(tmp_path):
    store = AccountStore(tmp_path / "monitor.sqlite3", "node", "account")
    store.capture(observation(trades=(trade(),)))
    batch = store.batch()
    with pytest.raises(ValueError, match="conflict"):
        store.capture(observation(trades=(trade().model_copy(update={"quantity": 200}),)))
    assert store.batch() == batch
    with pytest.raises(ValueError, match="not confirmed"):
        store.acknowledge(batch, Receipt(batch_hash="0" * 64, received_at=NOW))
    assert store.batch() == batch
    store.close()


def test_trade_identity_includes_market_and_day():
    initial = trade()
    assert initial.key != initial.model_copy(update={"symbol": "000001.SZ"}).key
    assert initial.key != trade(day=NOW + timedelta(days=1)).key


def test_upload_every_minute_auto_retries_and_resumes_after_restart(tmp_path):
    (tmp_path / "jobs").mkdir()
    hashed = publish_observation(
        tmp_path / "jobs/.account-observation.json", observation(trades=(trade(),))
    )
    cfg = SimpleNamespace(data_root=tmp_path, node_id="node", account_id="account")
    engine = Mock()
    engine.invoke.return_value = {"observation_hash": hashed}
    seconds = [0.0]
    send = Mock(side_effect=OSError("offline"))
    worker = AccountUploader(cfg, engine, seconds=lambda: seconds[0], send=send)
    assert worker.run()["status"] == "PENDING"
    assert worker.run()["status"] == "SKIPPED"
    seconds[0] = 59
    assert not worker.due()
    # New process needs no resume command. Even with QMT unavailable, durable fills can be sent.
    engine.invoke.side_effect = RuntimeError("qmt offline")
    delivered = []

    def accept(batch):
        delivered.append(batch)
        return Receipt(batch_hash=fingerprint(batch), received_at=NOW)

    resumed = AccountUploader(cfg, engine, seconds=lambda: seconds[0], send=accept)
    assert resumed.run()["status"] == "RETAINED_DATA_UPLOADED"
    assert len(delivered[0].trades) == 1
    seconds[0] = 119
    engine.invoke.side_effect = None
    assert resumed.run()["status"] == "UPLOADED"
    assert delivered[-1].trades == ()


@pytest.mark.parametrize("case", ["valid", "wrong-hash", "redirect", "error"])
def test_https_receipt_verification(case):
    batch = Batch(
        node_id="node",
        account_id="account",
        trades=(),
        snapshot=Snapshot(sequence=1, observation=observation()),
    )

    def handle(request):
        assert request.url.path == "/api/auto-trading/account-snapshots"
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://other.test"})
        if case == "error":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json=Receipt(
                batch_hash=fingerprint(batch) if case == "valid" else "0" * 64, received_at=NOW
            ).model_dump(mode="json"),
        )

    client = AccountHttp("https://quant.test", b"a" * 43, transport=httpx.MockTransport(handle))
    try:
        if case == "valid":
            assert client.send(batch).batch_hash == fingerprint(batch)
        else:
            with pytest.raises(ValueError):
                client.send(batch)
    finally:
        client.close()
    with pytest.raises(ValueError):
        AccountHttp("http://quant.test", b"a" * 43)


def test_monitoring_config_survives_roundtrip_and_requires_https(tmp_path):
    config = node_config(tmp_path).model_dump(mode="json")
    config["account_reporting"] = {
        "token": {"source": "ENVIRONMENT", "variable": "ATS_SECRET_ACCOUNT_MONITORING"}
    }
    config["quant_origin"] = "https://quant.test"
    first = NodeProcessConfig.model_validate_json(json.dumps(config))
    assert (
        NodeProcessConfig.model_validate_json(first.model_dump_json()).account_reporting is not None
    )
    config["quant_origin"] = "http://quant.test"
    with pytest.raises(ValueError, match="HTTPS"):
        NodeProcessConfig.model_validate_json(json.dumps(config))
    config["account_reporting"]["origin"] = "https://monitor.test"
    separate = NodeProcessConfig.model_validate_json(json.dumps(config))
    assert separate.quant_origin == "http://quant.test"
    assert separate.account_reporting.origin == "https://monitor.test"


def test_live_gateway_monitoring_uses_existing_session_and_never_sends_orders(tmp_path):
    from test_qmt_trading_gateway import make_gateway

    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        result = gateway.read_monitoring()
        assert result.positions[0].quantity == 100
        assert trader.calls == []
    finally:
        gateway.stop()


def test_readonly_facts_full_monitoring_without_recovery_or_display_mapping(tmp_path):
    from test_qmt_real_facts import ACCOUNT, client

    reader, sdk = client(tmp_path)
    rows = source(150)
    for p in rows["query_stock_positions"]:
        p.account_id = ACCOUNT
    rows["query_stock_asset"].account_id = ACCOUNT
    rows["query_stock_trades"][0].account_id = ACCOUNT
    sdk.positions = rows["query_stock_positions"]
    sdk.asset = rows["query_stock_asset"]
    sdk.trades = rows["query_stock_trades"]
    result = reader.read_monitoring(lambda: NOW, buy=23, sell=24)
    assert len(result.positions) == 150
    assert len(result.trades) == 1


def test_agent_starts_minute_reporting_without_login_or_selection_job(tmp_path):
    from threading import Event

    from auto_trading_system.sync.node_agent import AgentEndpoint, AgentWorker

    (tmp_path / "jobs").mkdir()
    config = node_config(tmp_path).model_dump(mode="json")
    config["quant_origin"] = "https://quant.test"
    config["account_reporting"] = {
        "token": {"source": "ENVIRONMENT", "variable": "ATS_SECRET_ACCOUNT_MONITORING"}
    }
    config = NodeProcessConfig.model_validate_json(json.dumps(config))
    hashed = publish_observation(tmp_path / "jobs/.account-observation.json", observation())
    engine = Mock()
    engine.invoke.return_value = {"observation_hash": hashed}
    worker = AgentWorker(config, engine, clock=lambda: NOW)
    uploaded = Event()

    def send(batch):
        uploaded.set()
        return Receipt(batch_hash=fingerprint(batch), received_at=NOW)

    worker.account_upload.send = send
    endpoint = AgentEndpoint(worker, Event(), "boot")
    try:
        endpoint.pump_account_reports()
        assert uploaded.wait(3)
        endpoint._account_active.result(timeout=3)
        endpoint.pump_account_reports()
        assert endpoint._account_last["status"] == "UPLOADED"
    finally:
        endpoint.close()


def test_engine_monitoring_endpoint_is_readonly_and_rejects_busy_or_disabled(tmp_path):
    from concurrent.futures import Future
    from time import time

    from auto_trading_system.control.ipc_protocol import IPCError, Request
    from auto_trading_system.runtime.node_engine import EngineEndpoint
    from auto_trading_system.sync.account_store import read_observation as read_handoff

    (tmp_path / "jobs").mkdir()
    endpoint = EngineEndpoint.__new__(EngineEndpoint)
    endpoint.config = SimpleNamespace(account_reporting=object(), data_root=tmp_path)
    endpoint.engine = Mock()
    endpoint.engine.dispatcher.idle = True
    future = Future()
    future.set_result(observation())
    endpoint.engine.dispatcher.observe.return_value = future
    endpoint.mailbox = Mock()
    endpoint.mailbox.call.side_effect = lambda operation, **_: operation()
    request = Request(
        request_id="capture",
        engine_boot_id="boot",
        sender_role="agent",
        deadline_ms=int(time() * 1000) + 8000,
        action="GET_ACCOUNT_OBSERVATION",
    )
    result = endpoint.handle(request)
    assert (
        read_handoff(tmp_path / "jobs/.account-observation.json", result["observation_hash"])
        == observation()
    )
    endpoint.engine.emergency_halt.assert_not_called()
    endpoint.engine.dispatcher.submit.assert_not_called()
    endpoint.engine.dispatcher.idle = False
    with pytest.raises(IPCError, match="BUSY"):
        endpoint.handle(request)
    endpoint.config.account_reporting = None
    with pytest.raises(IPCError, match="NOT_CONFIGURED"):
        endpoint.handle(request)


def test_sdk_busy_is_retried_inside_minute_without_waiting_for_next_upload(tmp_path, monkeypatch):
    from auto_trading_system.control.ipc_protocol import IPCError

    (tmp_path / "jobs").mkdir()
    hashed = publish_observation(tmp_path / "jobs/.account-observation.json", observation())
    engine = Mock()
    engine.invoke.side_effect = [IPCError("ACCOUNT_QUERY_BUSY"), {"observation_hash": hashed}]
    monkeypatch.setattr("auto_trading_system.sync.account_upload.sleep", lambda _: None)
    cfg = SimpleNamespace(data_root=tmp_path, node_id="node", account_id="account")
    upload = AccountUploader(
        cfg, engine, send=lambda batch: Receipt(batch_hash=fingerprint(batch), received_at=NOW)
    )
    assert upload.run()["status"] == "UPLOADED"
    assert engine.invoke.call_count == 2
