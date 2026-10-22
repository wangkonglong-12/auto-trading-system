"""Explicit, single-use QMT submit/cancel field diagnostic; never runs on import."""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from auto_trading_system.config import (
    ConfigurationError,
    EnvironmentProfile,
    validate_broker_account_id,
)
from auto_trading_system.config.loader import (
    calculate_config_hash,
    load_runtime_settings,
    resolve_secret_reference,
)
from auto_trading_system.domain import Money, ShanghaiTimestamp
from auto_trading_system.gateways.broker.qmt_command_transport import (
    QmtCommandError,
    QmtCommandTransport,
    QmtConstants,
)
from auto_trading_system.monitoring.qmt_command_probe import execute_qmt_command_probe
from auto_trading_system.monitoring.qmt_order_preflight import (
    QmtOrderPreflightError,
    evaluate_qmt_order_preflight,
    load_qmt_order_poc_approval,
)
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeConfig,
    QmtProbeOutcome,
    QmtProbeOutputError,
    QmtReadOnlyAdapterError,
    validate_qmt_probe_destination,
    write_immutable_probe_payload,
)
from auto_trading_system.runtime.single_instance import (
    InstanceGuardError,
    create_account_instance_guard,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-trading-qmt-command-probe")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--host-fingerprint", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--execute", action="store_true", help="send the single approved live order"
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print("QMT_COMMAND_EXPLICIT_EXECUTE_REQUIRED", file=sys.stderr)
        return 2
    try:
        validate_qmt_probe_destination(args.output)
        settings = load_runtime_settings(args.config)
        if settings.profile is not EnvironmentProfile.ASSISTED:
            raise QmtCommandError("QMT_COMMAND_ASSISTED_PROFILE_REQUIRED")
        account_secret = resolve_secret_reference(settings.security.broker_account_id)
        account_id = validate_broker_account_id(account_secret.get_secret_value())
        approval, approval_sha256 = load_qmt_order_poc_approval(
            args.approval, expected_sha256=args.approval_sha256
        )
        report = evaluate_qmt_order_preflight(
            approval,
            approval_sha256=approval_sha256,
            config_hash=calculate_config_hash(settings),
            broker_account_id=account_id,
            host_fingerprint=args.host_fingerprint,
            runtime_maximum_notional=Money(settings.hard_safety.max_single_order_notional),
        )
        if report.outcome is not QmtProbeOutcome.PASS:
            raise QmtCommandError(report.failure_code or "QMT_PREFLIGHT_NOT_PASSED")
        constants = QmtConstants.from_sdk()
        config = QmtProbeConfig(
            settings.qmt.userdata_mini, settings.qmt.session_id, settings.qmt.account_type
        )
        install_key = (
            "QmtReadonly"
            + hashlib.sha256(str(config.userdata_mini.resolve()).casefold().encode()).hexdigest()[
                :16
            ]
        )
        with (
            create_account_instance_guard(install_key),
            create_account_instance_guard(account_id),
        ):
            transport = QmtCommandTransport(
                config, account_id, lambda: datetime.now(UTC), constants=constants
            )
            result = execute_qmt_command_probe(
                transport,
                approval,
                report,
                settings.storage.root / "operator" / "qmt-probe-nonces",
                buy_type=constants.stock_buy,
                sell_type=constants.stock_sell,
                clock=lambda: ShanghaiTimestamp(datetime.now(UTC)),
            )
        payload = {
            "schema": "qmt-command-probe-result/v1",
            "approval_id": approval.approval_id,
            "execution_binding_sha256": report.execution_binding_sha256,
            "result": asdict(result),
            "manual_reconciliation_required": True,
        }
        write_immutable_probe_payload(args.output, payload)
        print(f"{result.outcome}:{result.reason}")
        return 0 if result.outcome == "ORDER_TERMINAL" else 4
    except (
        QmtCommandError,
        QmtOrderPreflightError,
        QmtProbeOutputError,
        QmtReadOnlyAdapterError,
        ConfigurationError,
        InstanceGuardError,
    ) as exc:
        print(getattr(exc, "code", "QMT_COMMAND_FAILED"), file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("QMT_COMMAND_SETUP_FAILED", file=sys.stderr)
        return 3


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
