"""Command-line composition root for QMT-004 offline approval validation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from auto_trading_system.config import (
    ConfigurationError,
    EnvironmentProfile,
    validate_broker_account_id,
)
from auto_trading_system.config.credentials import SecretProvider
from auto_trading_system.config.loader import (
    calculate_config_hash,
    load_runtime_settings,
    resolve_secret_reference,
)
from auto_trading_system.config.models import SecretSource
from auto_trading_system.domain import Money
from auto_trading_system.monitoring.qmt_order_preflight import (
    QmtOrderPreflightClock,
    QmtOrderPreflightError,
    evaluate_qmt_order_preflight,
    load_qmt_order_poc_approval,
    write_qmt_order_preflight_report,
)
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeOutputError,
    validate_qmt_probe_destination,
)
from auto_trading_system.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-trading-qmt-order-preflight")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", required=True, type=Path, help="absolute ASSISTED config path")
    parser.add_argument("--approval", required=True, type=Path, help="absolute approval JSON path")
    parser.add_argument("--approval-sha256", required=True, help="expected lowercase SHA-256")
    parser.add_argument("--host-fingerprint", required=True, help="target host fingerprint")
    parser.add_argument("--output", required=True, type=Path, help="new absolute JSON report path")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    providers: Mapping[SecretSource, SecretProvider] | None = None,
    clock: QmtOrderPreflightClock | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.config.is_absolute() or not arguments.approval.is_absolute():
        print("QMT_ORDER_PREFLIGHT_PATH_INVALID", file=sys.stderr)
        return 2
    try:
        validate_qmt_probe_destination(arguments.output)
    except QmtProbeOutputError as exc:
        print(exc.code, file=sys.stderr)
        return 3

    try:
        settings = load_runtime_settings(arguments.config)
        if settings.profile is not EnvironmentProfile.ASSISTED:
            print("QMT_ORDER_PREFLIGHT_PROFILE_NOT_ASSISTED", file=sys.stderr)
            return 2
        account_secret = resolve_secret_reference(
            settings.security.broker_account_id,
            environment=environment,
            providers=providers,
        )
        account_id = validate_broker_account_id(account_secret.get_secret_value())
        approval, approval_sha256 = load_qmt_order_poc_approval(
            arguments.approval,
            expected_sha256=arguments.approval_sha256,
        )
        report = evaluate_qmt_order_preflight(
            approval,
            approval_sha256=approval_sha256,
            config_hash=calculate_config_hash(settings),
            broker_account_id=account_id,
            host_fingerprint=arguments.host_fingerprint,
            runtime_maximum_notional=Money(settings.hard_safety.max_single_order_notional),
            clock=clock,
        )
    except ConfigurationError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except QmtOrderPreflightError as exc:
        print(exc.code, file=sys.stderr)
        return 3
    except (TypeError, ValueError):
        print("QMT_ORDER_PREFLIGHT_INPUT_INVALID", file=sys.stderr)
        return 2

    try:
        report_sha256 = write_qmt_order_preflight_report(
            arguments.output,
            report,
            forbidden_values=(
                account_id,
                approval.symbol.to_wire(),
                approval.single_use_nonce,
                approval.order_remark,
            ),
        )
    except QmtProbeOutputError as exc:
        print(exc.code, file=sys.stderr)
        return 3

    print(
        json.dumps(
            {
                "schema": "qmt-order-preflight-result/v1",
                "outcome": report.outcome.value,
                "failure_code": report.failure_code,
                "execution_binding_sha256": report.execution_binding_sha256,
                "report_sha256": report_sha256,
                "exit_code": report.exit_code,
            },
            sort_keys=True,
        )
    )
    return report.exit_code


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
