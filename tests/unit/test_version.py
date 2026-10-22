from __future__ import annotations

import json
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from auto_trading_system.apps.qmt_order_preflight.__main__ import (
    build_parser as build_qmt_order_preflight_parser,
)
from auto_trading_system.apps.qmt_readonly_probe.__main__ import (
    build_parser as build_qmt_probe_parser,
)
from auto_trading_system.apps.qmt_snapshot_probe.__main__ import (
    build_parser as build_qmt_snapshot_probe_parser,
)
from auto_trading_system.apps.qmt_tick_probe.__main__ import (
    build_parser as build_qmt_tick_probe_parser,
)
from auto_trading_system.apps.trade_runtime.__main__ import build_parser, main
from auto_trading_system.version import __version__


def test_version_is_exposed() -> None:
    project_root = Path(__file__).resolve().parents[2]
    with (project_root / "pyproject.toml").open("rb") as source:
        configured = tomllib.load(source)["project"]["version"]
    assert __version__ == configured == version("auto-trading-system")


def test_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"auto-trading-runtime {__version__}"


def test_qmt_probe_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_qmt_probe_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"auto-trading-qmt-probe {__version__}"


def test_qmt_snapshot_probe_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_qmt_snapshot_probe_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == (f"auto-trading-qmt-snapshot-probe {__version__}")


def test_qmt_tick_probe_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_qmt_tick_probe_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"auto-trading-qmt-tick-probe {__version__}"


def test_qmt_order_preflight_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_qmt_order_preflight_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == (f"auto-trading-qmt-order-preflight {__version__}")


def test_cli_without_arguments_exits_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["auto-trading-runtime"])
    assert main() == 0


def test_cli_prints_configuration_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--print-config-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["$id"].endswith("runtime-config.schema.json")


def test_cli_checks_configuration_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "config" / "v1" / "runtime.read-only.example.json"
    hmac_secret = "0123456789abcdef0123456789abcdef"
    broker_account = "A123456789"
    monkeypatch.setenv("ATS_SECRET_NODE_HMAC", hmac_secret)
    monkeypatch.setenv("ATS_SECRET_BROKER_ACCOUNT", broker_account)
    monkeypatch.setenv(
        "ATS_SECRET_OPERATOR_PASSWORD_VERIFIER",
        "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$"
        "f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4",
    )

    assert main(["--check-config", str(config_path)]) == 0

    output = capsys.readouterr().out
    assert '"profile": "READ_ONLY"' in output
    assert hmac_secret not in output
    assert broker_account not in output


def test_cli_returns_error_for_missing_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_path = tmp_path / "missing.json"

    assert main(["--check-config", str(missing_path)]) == 2
    assert "CONFIG_READ_FAILED" in capsys.readouterr().err
