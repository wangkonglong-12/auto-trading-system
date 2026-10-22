"""正式节点的 QMT 只读券商边界和配置隔离。"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_node_processes as node_helpers
from test_qmt_real_facts import ACCOUNT, Trader

from auto_trading_system.apps.qmt_debug import __main__ as debug_app
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.cicc_qmt_readonly_gateway import CiccQmtReadOnlyGateway
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig, QmtReadOnlyAdapterError
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.reconcile.portfolio_checkpoint import PortfolioCheckpoint, prefix_hash
from auto_trading_system.reconcile.position_lots import PositionLot
from auto_trading_system.runtime.node_engine import _live_opening_context
from auto_trading_system.runtime.opening_reference_file import save_opening_reference
from auto_trading_system.strategies.trading_settings import TradingSettings

node_fixture = node_helpers.node


class GatewayTrader(Trader):
    def __init__(self) -> None:
        super().__init__()
        self.callback: object | None = None
        self.status = [SimpleNamespace(account_id=ACCOUNT, account_type=2, status=0)]

    def register_callback(self, callback: object) -> None:
        self.callback = callback
        self.called.append("callback")

    def query_account_status(self) -> object:
        return self.status

    def unsubscribe(self, _: object) -> int:
        self.called.append("unsubscribe")
        return 0

    def stop(self) -> None:
        self.called.append("stop")


def gateway(
    tmp_path: Path, unavailable: list[str] | None = None
) -> tuple[CiccQmtReadOnlyGateway, GatewayTrader]:
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    (userdata / "up_queue_xtquant").touch()
    trader = GatewayTrader()
    bindings = XtquantBindings(lambda *_: trader, type("Callback", (), {}), lambda *_: None, "test")
    result = CiccQmtReadOnlyGateway(
        QmtProbeConfig(userdata, 6102302, "STOCK"),
        ACCOUNT,
        "qmt-testalias",
        lambda: datetime.now(UTC),
        bindings=bindings,
        unavailable_sink=unavailable.append if unavailable is not None else None,
    )
    return result, trader


def test_real_gateway_reads_account_but_refuses_all_orders(tmp_path: Path) -> None:
    broker, trader = gateway(tmp_path)
    broker.start(lambda _: pytest.fail("unexpected mapped broker event"))
    assert broker.query_orders() == ()
    assert broker.query_trades() == ()
    account = broker.read_account()
    assert account.cash == "123.46"
    assert account.positions[0].quantity == 100
    assert account.coherent is False
    assert broker.find_order_by_client_id("ord_unknown11") is None
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_GATEWAY_READ_ONLY"):
        broker.submit_order(None)  # type: ignore[arg-type]
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_GATEWAY_READ_ONLY"):
        broker.cancel_order(None)  # type: ignore[arg-type]
    broker.stop()
    assert trader.called[-2:] == ["unsubscribe", "stop"]


def test_real_activity_or_disconnect_blocks_recovery(tmp_path: Path) -> None:
    unavailable: list[str] = []
    broker, trader = gateway(tmp_path, unavailable)
    broker.start(lambda _: None)
    trader.orders = [SimpleNamespace(account_id=ACCOUNT, account_type=2, order_id=123)]
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_GATEWAY_ACTIVITY_UNMAPPED"):
        broker.query_orders()
    trader.orders = []
    assert broker.query_orders() == ()
    assert trader.callback is not None
    cast(Any, trader.callback).on_disconnected()
    assert unavailable == ["QMT_SDK_DISCONNECTED"]
    cast(Any, trader.callback).on_disconnected()
    assert unavailable == ["QMT_SDK_DISCONNECTED"]
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_GATEWAY_UNAVAILABLE"):
        broker.query_orders()
    broker.stop()


def test_stock_account_status_is_required_at_start(tmp_path: Path) -> None:
    broker, trader = gateway(tmp_path)
    trader.status = [SimpleNamespace(account_id=ACCOUNT, account_type=2, status=1)]
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_GATEWAY_ACCOUNT_NOT_OK"):
        broker.start(lambda _: None)
    assert trader.called[-2:] == ["unsubscribe", "stop"]


def test_nonready_account_callback_halts_once_with_specific_reason(tmp_path: Path) -> None:
    unavailable: list[str] = []
    broker, trader = gateway(tmp_path, unavailable)
    broker.start(lambda _: None)
    assert trader.callback is not None
    status = SimpleNamespace(account_id=ACCOUNT, account_type=2, status=1)
    cast(Any, trader.callback).on_account_status(status)
    cast(Any, trader.callback).on_disconnected()
    assert unavailable == ["QMT_ACCOUNT_STATUS_NOT_READY"]
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_GATEWAY_UNAVAILABLE"):
        broker.read_account()
    broker.stop()


def test_qmt_configuration_binds_fingerprint_and_keeps_mock_scope(
    node_fixture: tuple[NodeProcessConfig, Path, object], tmp_path: Path
) -> None:
    original = node_fixture[0]
    original_scope = original.scope()
    fingerprint = "0123456789abcdef"
    values = original.model_dump(mode="json")
    values.update(
        broker="QMT_READ_ONLY",
        account_id="qmt-" + fingerprint,
        qmt_read_only={
            "userdata_mini": str(tmp_path / "userdata_mini"),
            "account_fingerprint": fingerprint,
            "session_base": 6102301,
        },
    )
    real = NodeProcessConfig.model_validate_json(canonical_bytes(values))
    assert real.broker == "QMT_READ_ONLY"
    assert real.scope() != original_scope
    assert original.scope() == original_scope
    values["account_id"] = "qmt-wrongaccount"
    with pytest.raises(ValueError, match="fingerprint"):
        NodeProcessConfig.model_validate_json(canonical_bytes(values))
    values["account_id"] = "qmt-" + fingerprint
    values["operator_access"] = {
        "actor_id": "someone",
        "password_verifier": {"source": "ENVIRONMENT", "variable": "ATS_SECRET_FAKE_VERIFIER"},
    }
    authenticated = NodeProcessConfig.model_validate_json(canonical_bytes(values))
    assert authenticated.operator_access is not None
    assert authenticated.broker == "QMT_READ_ONLY"
    assert authenticated.qmt_read_only == real.qmt_read_only


def test_qmt_trading_configuration_requires_local_operator(
    node_fixture: tuple[NodeProcessConfig, Path, object], tmp_path: Path
) -> None:
    original = node_fixture[0]
    fingerprint = "0123456789abcdef"
    values = original.model_dump(mode="json")
    values.update(
        broker="QMT_TRADING",
        account_id="qmt-" + fingerprint,
        qmt_read_only={
            "userdata_mini": str(tmp_path / "userdata_mini"),
            "account_fingerprint": fingerprint,
            "session_base": 6102301,
        },
    )
    with pytest.raises(ValueError, match="local operator authentication"):
        NodeProcessConfig.model_validate_json(canonical_bytes(values))

    values["operator_access"] = {
        "actor_id": "local-operator",
        "password_verifier": {
            "source": "ENVIRONMENT",
            "variable": "ATS_SECRET_FAKE_VERIFIER",
        },
    }
    trading = NodeProcessConfig.model_validate_json(canonical_bytes(values))
    assert trading.broker == "QMT_TRADING"
    assert trading.account_id == "qmt-" + fingerprint
    assert trading.operator_access is not None


def test_qmt_live_opening_context_uses_only_owned_filled_lots(
    node_fixture: tuple[NodeProcessConfig, Path, object], tmp_path: Path
) -> None:
    original = node_fixture[0]
    fingerprint = "0123456789abcdef"
    values = original.model_dump(mode="json")
    values.update(
        broker="QMT_TRADING",
        account_id="qmt-" + fingerprint,
        qmt_read_only={
            "userdata_mini": str(tmp_path / "userdata_mini"),
            "account_fingerprint": fingerprint,
            "session_base": 6102301,
        },
        operator_access={
            "actor_id": "local-operator",
            "password_verifier": {
                "source": "ENVIRONMENT",
                "variable": "ATS_SECRET_FAKE_VERIFIER",
            },
        },
    )
    config = NodeProcessConfig.model_validate_json(canonical_bytes(values))
    settings = TradingSettings.model_validate_json(
        canonical_bytes(
            {
                "buy": {
                    "max_daily_buy_value": "50000",
                    "max_symbol_buy_value": "10000",
                    "max_order_value": "5000",
                    "cancel_after_seconds": 10,
                    "max_retries": 2,
                },
                "sell": {"take_profit_pct": "5", "stop_loss_pct": "3", "sell_by": "14:30"},
                "safety": {
                    "max_daily_loss": "2000",
                    "max_positions": 5,
                    "emergency_liquidation_enabled": True,
                },
                "advanced": {
                    "max_order_quantity": 10000,
                    "max_chase_bps": 30,
                    "max_spread_bps": 20,
                    "quote_max_age_ms": 1000,
                },
            }
        )
    )
    owned = PositionLot(
        lot_id="owned-lot",
        strategy_id=config.strategy_id,
        symbol="000001.SZ",
        trade_date_acquired=date(2026, 9, 7),
        eligible_sell_date=date(2026, 9, 8),
        calendar_version=config.calendar.version,
        bought_quantity=100,
        cost_price="10.00",
    )
    manual = owned.model_copy(
        update={"lot_id": "other-lot", "strategy_id": "other-strategy", "symbol": "600000.SH"}
    )
    checkpoint = PortfolioCheckpoint(
        account_id=config.account_id,
        trade_date=date(2026, 9, 7),
        calendar_version=config.calendar.version,
        recovery_report_hash="sha256:" + "a" * 64,
        submission_high_watermark=0,
        submission_prefix_hash=prefix_hash(()),
        lots=(owned, manual),
    )
    reference = checkpoint.reference()
    save_opening_reference(config.data_root / "critical/opening-reference.json", reference)

    class Result:
        def result(self, timeout: float) -> bytes:
            assert timeout == 3
            return checkpoint.body()

    class Journal:
        def request(self, operation: object, *, deadline: float) -> Result:
            assert callable(operation) and deadline > 0
            return Result()

    def morning() -> datetime:
        return datetime(2026, 9, 8, 2, 0, tzinfo=UTC)

    loaded, context = _live_opening_context(config, cast(Any, Journal()), morning, settings)

    assert loaded == reference and context is not None
    assert [(lot.lot_id, lot.symbol, lot.cost) for lot in context.lots] == [
        ("owned-lot", "000001.SZ", "10.00")
    ]
    assert context.sell_rules is not None
    assert str(context.sell_rules.take_profit_pct) == "5"
    assert context.max_order_value == "5000"

    _, late = _live_opening_context(
        config,
        cast(Any, Journal()),
        lambda: datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
        settings,
    )
    assert late is None
    assert _live_opening_context(original, cast(Any, Journal()), morning, settings) == (None, None)


def test_debug_session_builds_real_profile_from_prepared_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_root = tmp_path / "prepared"
    python = environment_root / "env/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    sdk = environment_root / "sdk/xtquant/xttrader.py"
    sdk.parent.mkdir(parents=True)
    sdk.touch()
    (environment_root / "sdk-manifest.json").write_text(
        json.dumps(
            {
                "schema": "qmt-sdk-environment/v1",
                "archive_sha256": debug_app._SDK_ARCHIVE_HASH,
                "python": "3.11 x64",
            }
        ),
        encoding="utf-8",
    )
    terminal = tmp_path / "terminal"
    userdata = terminal / "userdata_mini"
    userdata.mkdir(parents=True)
    (userdata / "up_queue_xtquant").touch()
    monkeypatch.setattr(sys, "executable", str(python))
    session = debug_app.QmtDebugSession(
        tmp_path / "new-session",
        environment_root,
        terminal,
        "0123456789abcdef",
        6102301,
    )
    assert session.config.broker == "QMT_READ_ONLY"
    assert session.config.account_id == "qmt-0123456789abcdef"
    assert session.config.qmt_read_only is not None
    assert session.config.qmt_read_only.userdata_mini == userdata
    assert str(environment_root / "sdk") in session.environment["PYTHONPATH"]
    assert not session.directory.exists()


def test_debug_session_builds_separate_qmt_trading_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_root = tmp_path / "prepared"
    python = environment_root / "env/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    sdk = environment_root / "sdk/xtquant/xttrader.py"
    sdk.parent.mkdir(parents=True)
    sdk.touch()
    (environment_root / "sdk-manifest.json").write_text(
        json.dumps(
            {
                "schema": "qmt-sdk-environment/v1",
                "archive_sha256": debug_app._SDK_ARCHIVE_HASH,
                "python": "3.11 x64",
            }
        ),
        encoding="utf-8",
    )
    terminal = tmp_path / "terminal"
    userdata = terminal / "userdata_mini"
    userdata.mkdir(parents=True)
    (userdata / "up_queue_xtquant").touch()
    monkeypatch.setattr(sys, "executable", str(python))
    session = debug_app.QmtDebugSession(
        tmp_path / "trading-session",
        environment_root,
        terminal,
        "0123456789abcdef",
        6102301,
        trading=True,
        operator_password="local-password-123",
    )
    assert session.config.broker == "QMT_TRADING"
    assert session.config.operator_access is not None
    assert session.environment["ATS_SECRET_DESKTOP_QMT_VERIFIER"].startswith(
        "ats-scrypt-v1$32768$8$1$"
    )
    assert "ATS_QMT_OPERATOR_PASSWORD" not in session.environment


def test_qmt_debug_main_self_test_closes_owned_session_and_writes_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = (tmp_path / "debug-run").resolve()
    writes: list[tuple[Path, object]] = []

    class Session:
        def __init__(
            self,
            selected_directory: Path,
            environment_root: Path,
            qmt_root: Path,
            fingerprint: str,
            session_base: int,
            *,
            trading: bool,
            operator_password: str | None,
        ) -> None:
            assert environment_root == tmp_path / "environment"
            assert qmt_root == tmp_path / "terminal"
            assert fingerprint == "0123456789abcdef" and session_base == 6102301
            assert trading and operator_password == "operator-secret"
            self.directory = selected_directory
            self.url = "http://127.0.0.1:31000/"
            self.stopped_cleanly = False

        def start(self) -> None:
            self.directory.mkdir(parents=True)

        def worker_status(self) -> dict[str, object]:
            return {
                "broker": "QMT_TRADING",
                "broker_account": {"account_id": "masked", "positions": []},
                "state": "HALTED",
            }

        def stop(self) -> None:
            self.stopped_cleanly = True

    monkeypatch.setattr(debug_app, "QmtDebugSession", Session)
    monkeypatch.setattr(
        debug_app,
        "write_immutable_probe_payload",
        lambda path, payload: writes.append((path, payload)),
    )
    monkeypatch.setenv("ATS_QMT_OPERATOR_PASSWORD", "operator-secret")

    result = debug_app.main(
        [
            "--environment-root",
            str(tmp_path / "environment"),
            "--qmt-root",
            str(tmp_path / "terminal"),
            "--directory",
            str(directory),
            "--account-fingerprint",
            "0123456789abcdef",
            "--trading",
            "--self-test",
            "--no-browser",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS" and output["owned_processes_closed"] is True
    assert writes[0][0] == directory / "execution.json"


def test_qmt_debug_main_rejects_existing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        debug_app.main(
            [
                "--environment-root",
                str(tmp_path / "environment"),
                "--qmt-root",
                str(tmp_path / "terminal"),
                "--directory",
                str(tmp_path),
                "--account-fingerprint",
                "0123456789abcdef",
                "--self-test",
            ]
        )
        == 2
    )
    assert "QMT_NODE_DEBUG_FAILED" in capsys.readouterr().err
