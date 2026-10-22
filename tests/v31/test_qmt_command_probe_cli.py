"""QMT command CLI stays explicit and writes immutable results without a real SDK."""

from __future__ import annotations

import json
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from auto_trading_system.apps.qmt_command_probe import __main__ as cli
from auto_trading_system.config import EnvironmentProfile
from auto_trading_system.gateways.broker.qmt_command_transport import QmtConstants
from auto_trading_system.monitoring.qmt_command_probe import QmtCommandProbeResult
from auto_trading_system.monitoring.qmt_readonly import QmtProbeOutcome


def args(output: Path, *, execute: bool = True) -> list[str]:
    result = [
        "--config",
        str(output.parent / "runtime.json"),
        "--approval",
        str(output.parent / "approval.json"),
        "--approval-sha256",
        "a" * 64,
        "--host-fingerprint",
        "0123456789abcdef",
        "--output",
        str(output),
    ]
    if execute:
        result.append("--execute")
    return result


def test_execute_flag_is_mandatory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(args((tmp_path / "result.json").resolve(), execute=False)) == 2
    assert capsys.readouterr().err.strip() == "QMT_COMMAND_EXPLICIT_EXECUTE_REQUIRED"


def test_success_writes_new_result_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = (tmp_path / "result.json").resolve()
    userdata = (tmp_path / "userdata_mini").resolve()
    userdata.mkdir()
    (userdata / "up_queue_xtquant").touch()
    settings = SimpleNamespace(
        profile=EnvironmentProfile.ASSISTED,
        security=SimpleNamespace(broker_account_id=object()),
        qmt=SimpleNamespace(userdata_mini=userdata, session_id=10, account_type="STOCK"),
        hard_safety=SimpleNamespace(max_single_order_notional=Decimal("5000.00")),
        storage=SimpleNamespace(root=tmp_path.resolve()),
    )
    approval = SimpleNamespace(approval_id="QMT004-20261020-0001")
    report = SimpleNamespace(
        outcome=QmtProbeOutcome.PASS,
        approval_id=approval.approval_id,
        execution_binding_sha256="sha256:" + "b" * 64,
    )
    patch = monkeypatch.setattr
    patch(cli, "load_runtime_settings", lambda _: settings)
    patch(cli, "calculate_config_hash", lambda _: "sha256:" + "c" * 64)
    patch(cli, "resolve_secret_reference", lambda _: SecretStr("A123456789"))
    patch(cli, "load_qmt_order_poc_approval", lambda *_args, **_kwargs: (approval, "a" * 64))
    patch(cli, "evaluate_qmt_order_preflight", lambda *_args, **_kwargs: report)
    patch(QmtConstants, "from_sdk", lambda: SimpleNamespace(stock_buy=23, stock_sell=24))
    patch(cli, "QmtCommandTransport", lambda *_args, **_kwargs: object())
    patch(cli, "create_account_instance_guard", lambda *_: nullcontext())
    patch(
        cli,
        "execute_qmt_command_probe",
        lambda *_args, **_kwargs: QmtCommandProbeResult(
            "ORDER_TERMINAL", "RECONCILE", 1, 2, 3, 54, 0
        ),
    )

    assert cli.run(args(output)) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "qmt-command-probe-result/v1"
    assert payload["result"]["broker_order_id"] == 2
    before = output.read_bytes()
    assert cli.run(args(output)) == 3
    assert output.read_bytes() == before


def test_non_assisted_profile_stops_before_secret_or_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = (tmp_path / "result.json").resolve()
    monkeypatch.setattr(
        cli,
        "load_runtime_settings",
        lambda _: SimpleNamespace(profile=EnvironmentProfile.READ_ONLY),
    )
    assert cli.run(args(output)) == 3
    assert not output.exists()
