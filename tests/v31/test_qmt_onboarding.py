"""真实接口接入的身份隐私、失败关闭、锁归属和子进程超时边界。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auto_trading_system.apps.qmt_onboarding import __main__ as app
from auto_trading_system.gateways.broker.cicc_qmt import (
    DiscoveredQmtAccount,
    XtquantBindings,
    discover_qmt_accounts,
)
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeConfig,
    QmtProbeOutcome,
    QmtReadOnlyAdapterError,
    write_immutable_probe_payload,
)
from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact


@pytest.fixture
def probe_config(tmp_path: Path) -> QmtProbeConfig:
    data = tmp_path / "userdata_mini"
    data.mkdir()
    (data / "up_queue_xtquant").touch()
    return QmtProbeConfig(data, 6101901, "STOCK", 1)


def bindings(trader: Any) -> XtquantBindings:
    return XtquantBindings(lambda *_: trader, type("Callback", (), {}), lambda *_: None, "test")


class Trader:
    def __init__(self) -> None:
        self.stopped = False
        self.result: object = [SimpleNamespace(account_id="testacct0001", account_type=2)]
        self.connected: object = 0

    def register_callback(self, callback: object) -> None:
        pass

    def start(self) -> None:
        pass

    def connect(self) -> object:
        return self.connected

    def query_account_infos(self) -> object:
        return self.result

    def query_account_status(self) -> object:
        return []

    def stop(self) -> None:
        self.stopped = True


def test_discovery_filters_stock_and_never_exposes_repr(probe_config: QmtProbeConfig) -> None:
    trader = Trader()
    assert discover_qmt_accounts(probe_config, bindings=bindings(trader)) == (
        DiscoveredQmtAccount("testacct0001", 2),
    )
    assert "testacct0001" not in repr(DiscoveredQmtAccount("testacct0001", 2))
    assert trader.stopped
    trader.result = [SimpleNamespace(account_id="testcredit", account_type=3)]
    assert discover_qmt_accounts(probe_config, bindings=bindings(trader)) == (
        DiscoveredQmtAccount("testcredit", 3),
    )


def test_same_fund_id_across_stock_and_connect_markets_is_valid(
    probe_config: QmtProbeConfig,
) -> None:
    trader = Trader()
    trader.result = [
        SimpleNamespace(account_id="testacct0001", account_type=kind) for kind in (2, 7, 11)
    ]
    assert len(discover_qmt_accounts(probe_config, bindings=bindings(trader))) == 3
    assert trader.stopped


@pytest.mark.parametrize(
    "result",
    [
        None,
        (),
        [object()],
        [SimpleNamespace(account_id="bad/value", account_type=2)],
        [SimpleNamespace(account_id="testacct0001", account_type=True)],
        [SimpleNamespace(account_id="testacct0001", account_type=2)] * 2,
        [None] * 33,
    ],
)
def test_discovery_invalid_result_stops(probe_config: QmtProbeConfig, result: object) -> None:
    trader = Trader()
    trader.result = result
    with pytest.raises(QmtReadOnlyAdapterError):
        discover_qmt_accounts(probe_config, bindings=bindings(trader))
    assert trader.stopped


@pytest.mark.parametrize("connected", [-1, None, False])
def test_discovery_failed_connection_stops(probe_config: QmtProbeConfig, connected: object) -> None:
    trader = Trader()
    trader.connected = connected
    with pytest.raises(QmtReadOnlyAdapterError, match="DISCOVERY_CONNECT_FAILED"):
        discover_qmt_accounts(probe_config, bindings=bindings(trader))
    assert trader.stopped


def args_for(config: QmtProbeConfig, output: Path) -> list[str]:
    return [
        "--userdata-mini",
        str(config.userdata_mini),
        "--output",
        str(output),
        "--seconds",
        "1",
        "--scenario",
        "CLOSED",
        "--symbol",
        "600000.SH",
    ]


class Guard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def acquire(self) -> None:
        self.events.append("acquire")

    def release(self) -> None:
        self.events.append("release")


def guard_for(key: str, events: list[str]) -> Guard:
    assert key.isascii() and key.isalnum() and 4 <= len(key) <= 64
    return Guard(events)


@pytest.mark.parametrize("failure", [None, "connection", "snapshot", "tick", "discovery"])
def test_workflow_reuses_probes_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe_config: QmtProbeConfig,
    failure: str | None,
) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    events: list[str] = []
    monkeypatch.setattr(app, "create_account_instance_guard", lambda key: guard_for(key, events))
    monkeypatch.setattr(app, "sleep", lambda delay: events.append(f"wait:{delay}"))

    def discover(_: object) -> tuple[DiscoveredQmtAccount, ...]:
        if failure == "discovery":
            raise QmtReadOnlyAdapterError("SDK_TEST_FAILURE", "no source values")
        return (DiscoveredQmtAccount("testacct0001", 2),)

    monkeypatch.setattr(app, "discover_qmt_accounts", discover)

    def probe(name: str) -> Any:
        def call(*_: object, **__: object) -> SimpleNamespace:
            if name in {"connection", "snapshot"}:
                config = _[0]
                assert isinstance(config, QmtProbeConfig)
                offset = 1 if name == "connection" else 2
                assert config.session_id == probe_config.session_id + offset
            events.append(name)
            failed = failure == name
            return SimpleNamespace(
                outcome=QmtProbeOutcome.ERROR if failed else QmtProbeOutcome.PASS,
                exit_code=3 if failed else 0,
                failure_code="SDK_TEST_FAILURE" if failed else None,
            )

        return call

    for name, method, writer in (
        ("connection", "run_qmt_readonly_probe", "write_qmt_probe_report"),
        ("snapshot", "run_qmt_snapshot_probe", "write_qmt_snapshot_report"),
        ("tick", "run_qmt_tick_probe", "write_qmt_tick_report"),
    ):
        monkeypatch.setattr(app, method, probe(name))
        monkeypatch.setattr(app, writer, lambda *_, **__: "hash")
    args = app.build_parser().parse_args(args_for(probe_config, output))
    assert app.run_checks(args) == (3 if failure else 0)
    summary_text = (output / "summary.json").read_text(encoding="utf-8")
    assert "testacct0001" not in summary_text
    summary = json.loads(summary_text)
    assert summary["status"] == ("ERROR" if failure else "PASS")
    assert summary["real_trading_enabled"] is False
    assert events.count("acquire") == events.count("release")
    if failure in {"connection", "discovery"}:
        assert "snapshot" not in events and "tick" not in events
    if not failure:
        assert events.count("wait:3.1") == 2


@pytest.mark.parametrize("broken", [False, True])
def test_facts_stage_keeps_values_private_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe_config: QmtProbeConfig,
    broken: bool,
) -> None:
    output = tmp_path / "facts"
    output.mkdir()
    events: list[str] = []
    monkeypatch.setattr(app, "create_account_instance_guard", lambda key: guard_for(key, events))
    monkeypatch.setattr(app, "sleep", lambda _: None)
    monkeypatch.setattr(
        app, "discover_qmt_accounts", lambda _: (DiscoveredQmtAccount("testacct0001", 2),)
    )
    successful = SimpleNamespace(outcome=QmtProbeOutcome.PASS, exit_code=0, failure_code=None)
    for name in ("run_qmt_readonly_probe", "run_qmt_snapshot_probe", "run_qmt_tick_probe"):
        monkeypatch.setattr(app, name, lambda *_, **__: successful)
    for name in ("write_qmt_probe_report", "write_qmt_snapshot_report", "write_qmt_tick_report"):
        monkeypatch.setattr(app, name, lambda *_, **__: "hash")

    class FakeFacts:
        def __init__(self, config: QmtProbeConfig, account_id: str) -> None:
            assert config.session_id == probe_config.session_id + 3
            assert account_id == "testacct0001"

        def register_signal_sink(self, _: object) -> None:
            pass

        def start(self) -> None:
            events.append("facts-start")

        def connect(self) -> int:
            return 0

        def subscribe(self) -> int:
            return 0

        def query_account_statuses(self) -> SimpleNamespace:
            return SimpleNamespace(target_statuses=(SimpleNamespace(status=0),))

        def read_facts(self, alias: str, now: datetime) -> SimpleNamespace:
            assert alias.startswith("qmt-") and now.tzinfo is not None
            if broken:
                raise QmtReadOnlyAdapterError("QMT_FACT_ASSET_NONE", "asset missing")
            account = AccountFact(
                account_id=alias,
                snapshot_id="qmt-testfacts",
                observed_at=datetime.now(UTC),
                cash="12345.67",
                positions=(PositionFact(symbol="000001.SZ", quantity=100, sellable=100),),
                observed_order_ids=(),
                coherent=False,
            )
            return SimpleNamespace(account=account, order_count=0, trade_count=0)

        def unsubscribe(self) -> int:
            events.append("facts-unsubscribe")
            return 0

        def stop(self) -> None:
            events.append("facts-stop")

    monkeypatch.setattr(app, "CiccQmtFactsClient", FakeFacts)
    args = app.build_parser().parse_args([*args_for(probe_config, output), "--facts"])
    assert app.run_checks(args) == (3 if broken else 0)
    payload = (output / "summary.json").read_text(encoding="utf-8")
    assert "testacct0001" not in payload and "12345.67" not in payload
    assert "facts-stop" in events
    assert events.count("acquire") == events.count("release")
    if not broken:
        assert '"facts": "PASS"' in payload
        assert '"coherent_for_order_recovery": false' in payload


@pytest.mark.parametrize("account_count", [0, 2])
def test_account_selection_never_guesses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe_config: QmtProbeConfig,
    account_count: int,
) -> None:
    output = tmp_path / "selection"
    output.mkdir()
    events: list[str] = []
    monkeypatch.setattr(app, "create_account_instance_guard", lambda key: guard_for(key, events))
    monkeypatch.setattr(
        app,
        "discover_qmt_accounts",
        lambda _: tuple(DiscoveredQmtAccount(f"testacct000{i}", 2) for i in range(account_count)),
    )
    assert app.run_checks(app.build_parser().parse_args(args_for(probe_config, output))) == 2
    assert events == ["acquire", "release"]
    expected = "SELECTION_REQUIRED" if account_count else "NO_STOCK_ACCOUNT"
    assert expected in (output / "summary.json").read_text()


def test_existing_output_and_direct_worker_refused(
    probe_config: QmtProbeConfig, tmp_path: Path
) -> None:
    argv = args_for(probe_config, tmp_path)
    sentinel = tmp_path / "preserve.txt"
    sentinel.write_text("keep")
    assert app.main(argv) == 3
    assert app.main(["--worker", *argv]) == 3
    assert app.main([*argv, "--session-id", "2147483647"]) == 3
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("case", ["pass", "no-report", "timeout", "late-pass"])
def test_parent_owns_cleanup_and_publishes_final_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe_config: QmtProbeConfig, case: str
) -> None:
    output = tmp_path / "parent"
    events: list[str] = []

    class Child:
        def __init__(self, command: object, *, environment: dict[str, str]) -> None:
            assert environment["ATS_QMT_READONLY_OUTPUT"] == str(output)
            if case in {"pass", "late-pass"}:
                write_immutable_probe_payload(
                    output / "summary.json", {"status": "PASS", "real_trading_enabled": False}
                )

        def wait(self, seconds: int) -> int:
            if seconds > 5 and case in {"timeout", "late-pass"}:
                raise TimeoutError()
            return 0

        def terminate(self) -> None:
            events.append("terminate")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(app, "OwnedProcess", Child)
    code = app.main(args_for(probe_config, output))
    assert code == (0 if case == "pass" else 3)
    result = json.loads((output / "execution.json").read_text())
    assert result["status"] == ("PASS" if case == "pass" else "ERROR")
    assert events[-1] == "close"
    if case in {"timeout", "late-pass"}:
        assert events == ["terminate", "close"]


def test_discovery_cleanup_failure_cannot_publish_accounts(probe_config: QmtProbeConfig) -> None:
    class BrokenCleanup(Trader):
        def stop(self) -> None:
            raise RuntimeError("private SDK text")

    with pytest.raises(QmtReadOnlyAdapterError, match="DISCOVERY_CLEANUP_FAILED") as caught:
        discover_qmt_accounts(probe_config, bindings=bindings(BrokenCleanup()))
    assert "private SDK text" not in str(caught.value)


@pytest.mark.parametrize(
    "status_result",
    [[], None, [SimpleNamespace(account_id="testacct0001", account_type=2, status=0)]],
)
def test_empty_info_list_cross_checks_status_without_guessing(
    probe_config: QmtProbeConfig, status_result: object
) -> None:
    class StatusTrader(Trader):
        def query_account_status(self) -> object:
            return status_result

    trader = StatusTrader()
    trader.result = []
    if status_result is None:
        with pytest.raises(QmtReadOnlyAdapterError, match="DISCOVERY_RESULT_INVALID"):
            discover_qmt_accounts(probe_config, bindings=bindings(trader))
    else:
        result = discover_qmt_accounts(probe_config, bindings=bindings(trader))
        assert result == (
            (DiscoveredQmtAccount("testacct0001", 2, "ACCOUNT_STATUS"),) if status_result else ()
        )
    assert trader.stopped


def test_market_only_does_not_discover_or_query_accounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe_config: QmtProbeConfig
) -> None:
    output = tmp_path / "market"
    output.mkdir()
    events: list[str] = []
    monkeypatch.setattr(app, "create_account_instance_guard", lambda key: guard_for(key, events))
    monkeypatch.setattr(app, "discover_qmt_accounts", lambda _: pytest.fail("account access"))
    monkeypatch.setattr(
        app,
        "run_qmt_tick_probe",
        lambda *_, **__: SimpleNamespace(
            outcome=QmtProbeOutcome.PASS, exit_code=0, failure_code=None
        ),
    )
    monkeypatch.setattr(app, "write_qmt_tick_report", lambda *_, **__: "hash")
    args = app.build_parser().parse_args([*args_for(probe_config, output), "--market-only"])
    assert app.run_checks(args) == 0
    result = json.loads((output / "summary.json").read_text())
    assert result["account_checks"] == "NOT_REQUESTED"
    assert result["stages"] == {"tick": "PASS"}
    assert events == ["acquire", "release"]


def test_installation_lock_failure_never_opens_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe_config: QmtProbeConfig
) -> None:
    class Locked(Guard):
        def acquire(self) -> None:
            raise RuntimeError("locked")

    monkeypatch.setattr(app, "create_account_instance_guard", lambda _: Locked([]))
    monkeypatch.setattr(app, "discover_qmt_accounts", lambda _: pytest.fail("SDK opened"))
    with pytest.raises(RuntimeError, match="locked"):
        app.run_checks(app.build_parser().parse_args(args_for(probe_config, tmp_path)))
    assert not (tmp_path / "summary.json").exists()


def test_release_failure_refuses_successful_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe_config: QmtProbeConfig
) -> None:
    class BrokenRelease(Guard):
        def release(self) -> None:
            raise RuntimeError("cannot release")

    monkeypatch.setattr(app, "create_account_instance_guard", lambda _: BrokenRelease([]))
    monkeypatch.setattr(app, "discover_qmt_accounts", lambda _: ())
    with pytest.raises(RuntimeError, match="cannot release"):
        app.run_checks(app.build_parser().parse_args(args_for(probe_config, tmp_path)))
    assert not (tmp_path / "summary.json").exists()
