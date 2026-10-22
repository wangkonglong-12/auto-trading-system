"""Command-line entry point for the independent Windows Runtime watchdog."""

from __future__ import annotations

import argparse
import hashlib
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from auto_trading_system.apps.watchdog.supervisor import (
    SubprocessRuntimeLauncher,
    WatchdogPolicy,
    WatchdogSupervisor,
    WatchdogSupervisorError,
)
from auto_trading_system.config import ConfigurationError, load_runtime_settings
from auto_trading_system.domain.clock import SystemClock
from auto_trading_system.runtime import create_account_instance_guard
from auto_trading_system.runtime.single_instance import InstanceGuardError
from auto_trading_system.runtime.watchdog import (
    ExternalHaltMarkerStore,
    RuntimeHeartbeatStore,
    WatchdogArtifactError,
)
from auto_trading_system.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-trading-watchdog",
        description="monitor one Runtime heartbeat and relaunch its exact argv safely",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="validated Runtime configuration path (credentials are not resolved)",
    )
    parser.add_argument(
        "runtime_command",
        nargs=argparse.REMAINDER,
        metavar="RUNTIME_ARG",
        help="exact Runtime argv after --; no command shell is used",
    )
    return parser


def _watchdog_ownership_key(node_id: str, account_id: str) -> str:
    return hashlib.sha256(f"watchdog\x00{node_id}\x00{account_id}".encode()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    command = tuple(arguments.runtime_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("watchdog invalid: runtime command is required after --", file=sys.stderr)
        return 2

    try:
        settings = load_runtime_settings(arguments.config)
        launcher = SubprocessRuntimeLauncher(command)
        heartbeat_store = RuntimeHeartbeatStore.from_storage_root(
            settings.storage.root,
            node_id=settings.node.node_id,
            account_id=settings.node.account_id,
        )
        marker_store = ExternalHaltMarkerStore.from_storage_root(
            settings.storage.root,
            node_id=settings.node.node_id,
            account_id=settings.node.account_id,
        )
        clock = SystemClock()
        supervisor = WatchdogSupervisor(
            heartbeat_store=heartbeat_store,
            marker_store=marker_store,
            launcher=launcher,
            clock=clock,
            monotonic_clock=clock,
            policy=WatchdogPolicy.from_settings(settings.watchdog),
            storage_root=settings.storage.root,
            minimum_free_bytes=settings.storage.minimum_free_space_mb * 1024 * 1024,
        )
        ownership = create_account_instance_guard(
            _watchdog_ownership_key(settings.node.node_id, settings.node.account_id)
        )
        stop_event = threading.Event()

        def request_stop(_signal_number: int, _frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        with ownership:
            supervisor.run(stop_event.is_set)
    except (
        ConfigurationError,
        InstanceGuardError,
        OSError,
        ValueError,
        WatchdogArtifactError,
        WatchdogSupervisorError,
    ) as exc:
        print(f"watchdog failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
