"""Runtime diagnostics and explicit offline acceptance entry points."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from auto_trading_system.config import ConfigurationError, load_runtime_configuration
from auto_trading_system.config.schema import runtime_config_schema
from auto_trading_system.version import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser without starting trading infrastructure."""
    parser = argparse.ArgumentParser(prog="auto-trading-runtime")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--simulate-entry-owner",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run Mock-only entry target, partial cancel and restart replacement acceptance",
    )
    actions.add_argument(
        "--check-config",
        metavar="PATH",
        type=Path,
        help="validate a runtime config and resolve its secret references",
    )
    actions.add_argument(
        "--print-config-schema",
        action="store_true",
        help="print the RuntimeConfig v1.0 JSON Schema",
    )
    actions.add_argument(
        "--simulate",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run the V3.1 offline acceptance flow using only a Mock Broker",
    )
    actions.add_argument(
        "--simulate-business-flow",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run a shared two-day plan/buy/exit/daily-upload/query acceptance with Mock only",
    )
    actions.add_argument(
        "--simulate-engine",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run the V2 engine durable-authorization and restart acceptance with Mock only",
    )
    actions.add_argument(
        "--simulate-exit",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run Mock-only cross-day lot sealing and restart-safe D1 exit acceptance",
    )
    actions.add_argument(
        "--simulate-exit-owner",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run Mock-only continuous D1 rules, independent state and opening-context acceptance",
    )
    actions.add_argument(
        "--simulate-journal",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run synthetic historical scan and large checkpoint recovery with Mock only",
    )
    actions.add_argument(
        "--simulate-portfolio",
        metavar="OUTPUT_DIRECTORY",
        type=Path,
        help="run Mock-only continuous account observation and T+1 lot acceptance",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected diagnostic or hard-wired Mock simulation and exit."""
    arguments = build_parser().parse_args(argv)
    if arguments.simulate_entry_owner is not None:
        from auto_trading_system.apps.trade_runtime.entry_simulation import run_entry_simulation

        print(json.dumps(run_entry_simulation(arguments.simulate_entry_owner), sort_keys=True))
        return 0
    if arguments.simulate_business_flow is not None:
        from auto_trading_system.apps.trade_runtime.business_flow import run_business_flow

        print(json.dumps(run_business_flow(arguments.simulate_business_flow), sort_keys=True))
        return 0
    if arguments.simulate_journal is not None:
        from auto_trading_system.apps.trade_runtime.journal_simulation import run_journal_simulation

        print(json.dumps(run_journal_simulation(arguments.simulate_journal), sort_keys=True))
        return 0
    if arguments.simulate_exit_owner is not None:
        from auto_trading_system.apps.trade_runtime.exit_owner_simulation import (
            run_exit_owner_simulation,
        )

        print(json.dumps(run_exit_owner_simulation(arguments.simulate_exit_owner), sort_keys=True))
        return 0
    if arguments.simulate_exit is not None:
        from auto_trading_system.apps.trade_runtime.exit_simulation import run_exit_simulation

        print(json.dumps(run_exit_simulation(arguments.simulate_exit), sort_keys=True))
        return 0
    if arguments.simulate_portfolio is not None:
        from auto_trading_system.apps.trade_runtime.portfolio_simulation import (
            run_portfolio_simulation,
        )

        print(json.dumps(run_portfolio_simulation(arguments.simulate_portfolio), sort_keys=True))
        return 0
    if arguments.simulate_engine is not None:
        from auto_trading_system.apps.trade_runtime.engine_simulation import run_engine_simulation

        print(json.dumps(run_engine_simulation(arguments.simulate_engine), sort_keys=True))
        return 0
    if arguments.simulate is not None:
        import asyncio

        from auto_trading_system.apps.trade_runtime.simulation import run_simulation

        result = asyncio.run(run_simulation(arguments.simulate))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if arguments.print_config_schema:
        print(json.dumps(runtime_config_schema(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if arguments.check_config is not None:
        try:
            loaded = load_runtime_configuration(arguments.check_config)
        except ConfigurationError as exc:
            print(f"configuration invalid: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(loaded.safe_summary(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
