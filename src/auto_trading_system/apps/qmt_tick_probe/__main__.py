"""Command-line composition root for QMT-003 tick evidence."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

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
from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.market.qmt import CiccQmtTickProbeClient
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeClock,
    QmtProbeOutputError,
    validate_qmt_probe_destination,
)
from auto_trading_system.monitoring.qmt_tick import (
    QmtTickClientFactory,
    QmtTickProbeConfig,
    QmtTickScenario,
    run_qmt_tick_probe,
    write_qmt_tick_report,
)
from auto_trading_system.runtime import (
    InstanceAlreadyRunningError,
    InstanceGuardError,
    create_account_instance_guard,
)
from auto_trading_system.version import __version__


class AccountProbeGuard(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


AccountProbeGuardFactory = Callable[[str], AccountProbeGuard]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-trading-qmt-tick-probe")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", required=True, type=Path, help="absolute READ_ONLY config path")
    parser.add_argument("--output", required=True, type=Path, help="new absolute JSON report path")
    parser.add_argument(
        "--symbol",
        required=True,
        action="append",
        help="one canonical security code; repeat for at most 15 symbols",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=[scenario.value for scenario in QmtTickScenario],
        help="declared target-machine observation scenario",
    )
    parser.add_argument("--observation-seconds", type=int, default=30)
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    providers: Mapping[SecretSource, SecretProvider] | None = None,
    client_factory: QmtTickClientFactory | None = None,
    instance_guard_factory: AccountProbeGuardFactory | None = None,
    clock: QmtProbeClock | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.config.is_absolute():
        print("QMT_TICK_CONFIG_PATH_INVALID", file=sys.stderr)
        return 2
    try:
        validate_qmt_probe_destination(arguments.output)
    except QmtProbeOutputError as exc:
        print(exc.code, file=sys.stderr)
        return 3

    try:
        settings = load_runtime_settings(arguments.config)
        if settings.profile is not EnvironmentProfile.READ_ONLY:
            print("QMT_TICK_PROFILE_NOT_READ_ONLY", file=sys.stderr)
            return 2
        account_secret = resolve_secret_reference(
            settings.security.broker_account_id,
            environment=environment,
            providers=providers,
        )
        account_id = validate_broker_account_id(account_secret.get_secret_value())
        symbols = tuple(SecurityCode.parse(item) for item in arguments.symbol)
        probe_config = QmtTickProbeConfig(
            symbols=symbols,
            scenario=QmtTickScenario(arguments.scenario),
            observation_seconds=arguments.observation_seconds,
        )
    except ConfigurationError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except (TypeError, ValueError):
        print("QMT_TICK_INPUT_INVALID", file=sys.stderr)
        return 2

    effective_client_factory = (
        (lambda: CiccQmtTickProbeClient()) if client_factory is None else client_factory
    )
    effective_guard_factory = (
        create_account_instance_guard if instance_guard_factory is None else instance_guard_factory
    )
    try:
        instance_guard = effective_guard_factory(account_id)
        instance_guard.acquire()
    except InstanceAlreadyRunningError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except InstanceGuardError as exc:
        print(exc.code, file=sys.stderr)
        return 3
    except Exception:
        print("QMT_TICK_INSTANCE_GUARD_FAILED", file=sys.stderr)
        return 3

    report = None
    probe_failure_code: str | None = None
    try:
        report = run_qmt_tick_probe(
            probe_config,
            config_hash=calculate_config_hash(settings),
            client_factory=effective_client_factory,
            clock=clock,
        )
    except Exception:
        probe_failure_code = "QMT_TICK_UNEXPECTED_FAILURE"
    finally:
        try:
            instance_guard.release()
        except InstanceGuardError as exc:
            probe_failure_code = exc.code
        except Exception:
            probe_failure_code = "QMT_TICK_INSTANCE_GUARD_RELEASE_FAILED"

    if probe_failure_code is not None or report is None:
        print(probe_failure_code or "QMT_TICK_UNEXPECTED_FAILURE", file=sys.stderr)
        return 3

    try:
        report_sha256 = write_qmt_tick_report(
            arguments.output,
            report,
            forbidden_values=(account_id, *(symbol.to_wire() for symbol in symbols)),
        )
    except QmtProbeOutputError as exc:
        print(exc.code, file=sys.stderr)
        return 3

    print(
        json.dumps(
            {
                "schema": "qmt-tick-probe-result/v1",
                "outcome": report.outcome.value,
                "failure_code": report.failure_code,
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
