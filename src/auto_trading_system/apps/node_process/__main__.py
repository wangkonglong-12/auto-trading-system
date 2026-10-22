"""Explicit process commands and prompt-only PAPER authorization; no order endpoint."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from time import monotonic, sleep

import pywintypes
from pydantic import JsonValue

from auto_trading_system.apps.node_process.authorization_cli import authorize, query_result
from auto_trading_system.config.node_v2 import load_node_config, role_keys
from auto_trading_system.control.ipc_protocol import IPCError, RPCClient
from auto_trading_system.control.node_authorization import GrantProposal, PreviewProposal
from auto_trading_system.control.windows_pipe import exchange
from auto_trading_system.runtime.node_engine import run_engine
from auto_trading_system.runtime.node_layout import initialize_layout, validate_layout
from auto_trading_system.runtime.node_supervisor import reset_supervision, run_supervisor
from auto_trading_system.runtime.node_tasks import task_plan
from auto_trading_system.sync.node_agent import JOB_KINDS, run_agent
from auto_trading_system.version import __version__


def emit(value: dict[str, JsonValue]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-trading-node")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "check", "engine", "agent", "task-plan", "supervise", "reset-supervision"):
        commands.add_parser(name)
    job = commands.add_parser("job")
    job.add_argument("kind", choices=sorted(JOB_KINDS))
    for name in ("status", "stop"):
        command = commands.add_parser(name)
        command.add_argument("component", choices=("engine", "agent", "supervisor"))
    commands.add_parser("halt")
    commands.add_parser("recover")
    commands.add_parser("seal-day")
    console = commands.add_parser("console")
    console.add_argument("--port", type=int, default=8780)
    approval = commands.add_parser("authorize")
    approval.add_argument("action", choices=("confirm-recovery", "arm", "resume"))
    approval.add_argument("--directions", nargs="+", choices=("BUY", "SELL"), default=["BUY"])
    approval.add_argument("--max-buy-value", default="0.00")
    approval.add_argument("--max-order-value", default="0.00")
    approval.add_argument("--max-positions", type=int, default=0)
    approval.add_argument("--duration-seconds", type=int, default=60)
    result = commands.add_parser("authorization-result")
    result.add_argument("request_id")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", required=True, type=Path)
    for name in ("verify-backup", "restore-backup"):
        maintenance = commands.add_parser(name)
        maintenance.add_argument("--bundle", required=True, type=Path)
        maintenance.add_argument("--manifest-hash", required=True)
        if name == "restore-backup":
            maintenance.add_argument("--destination", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_node_config(arguments.config)
        if arguments.command in ("backup", "verify-backup", "restore-backup"):
            from auto_trading_system.runtime.node_backup import (
                create_backup,
                restore_candidate,
                verify_backup,
            )

            if arguments.command == "backup":
                manifest_hash = create_backup(config, arguments.destination)
                emit(
                    {
                        "status": "BACKUP_VERIFIED",
                        "manifest_hash": manifest_hash,
                        "real_trading_enabled": False,
                    }
                )
            elif arguments.command == "verify-backup":
                manifest = verify_backup(config, arguments.bundle, arguments.manifest_hash)
                emit(
                    {
                        "status": "BACKUP_VERIFIED",
                        "files": len(manifest.files),
                        "last_record_seq": manifest.journal.last_record_seq,
                        "real_trading_enabled": False,
                    }
                )
            else:
                if not sys.stdin.isatty():
                    raise IPCError("INTERACTIVE_OPERATOR_REQUIRED")
                phrase = "RESTORE CANDIDATE " + config.account_id
                if input("在新目录恢复待核验副本。请输入 " + phrase + ": ") != phrase:
                    raise IPCError("CONFIRMATION_MISMATCH")
                restore_candidate(
                    config, arguments.bundle, arguments.manifest_hash, arguments.destination
                )
                emit(
                    {
                        "status": "CANDIDATE_VERIFIED",
                        "startup_blocked": True,
                        "real_trading_enabled": False,
                    }
                )
            return 0
        if arguments.command == "init":
            initialize_layout(config)
            emit({"initialized": True, "broker": config.broker})
            return 0
        validate_layout(config)
        if arguments.command == "check":
            emit(
                {
                    "configuration_valid": True,
                    "broker": config.broker,
                    "real_trading_enabled": False,
                }
            )
        elif arguments.command == "task-plan":
            emit(task_plan(config, arguments.config.resolve(), Path(sys.executable).resolve()))
        elif arguments.command == "engine":
            run_engine(config, ready=emit)
        elif arguments.command == "supervise":
            run_supervisor(config, arguments.config, ready=emit)
        elif arguments.command == "reset-supervision":
            if not sys.stdin.isatty():
                raise IPCError("INTERACTIVE_OPERATOR_REQUIRED")
            phrase = "RESET SUPERVISION " + config.account_id
            if input("清除监护熔断，不授权交易。请输入 " + phrase + ": ") != phrase:
                raise IPCError("CONFIRMATION_MISMATCH")
            reset_supervision(config)
            emit({"supervision_reset": True, "real_trading_enabled": False})
        elif arguments.command == "agent":
            run_agent(config, ready=emit)
        elif arguments.command == "console":
            # Load the web composition only for its own process. A UI import or
            # asset problem must not prevent engine/supervisor CLI startup.
            from auto_trading_system.apps.local_console.node_app import run_node_console

            run_node_console(config, port=arguments.port)
        elif arguments.command in ("authorize", "authorization-result"):
            if config.operator_access is None or not sys.stdin.isatty():
                raise IPCError("INTERACTIVE_OPERATOR_REQUIRED")
            key = role_keys(config, ("operator",))["operator"]
            client = RPCClient(
                lambda body: exchange(config.pipe_name("authorization"), body), "operator", key
            )
            if arguments.command == "authorization-result":
                return query_result(
                    client, arguments.request_id, password_reader=getpass.getpass, output=emit
                )
            action = arguments.action.upper().replace("-", "_")
            proposal = PreviewProposal.model_validate(
                {
                    "action": action,
                    "grant": None
                    if action == "CONFIRM_RECOVERY"
                    else GrantProposal(
                        directions=tuple(arguments.directions),
                        max_buy_value=arguments.max_buy_value,
                        max_order_value=arguments.max_order_value,
                        max_positions=arguments.max_positions,
                        duration_seconds=arguments.duration_seconds,
                    ),
                }
            )
            return authorize(
                client,
                proposal,
                password_reader=getpass.getpass,
                confirmation_reader=input,
                output=emit,
            )
        elif arguments.command == "job":
            key = role_keys(config, ("jobrunner",))["jobrunner"]
            client = RPCClient(
                lambda body: exchange(config.pipe_name("agent"), body), "jobrunner", key
            )
            accepted = client.invoke("RUN_JOB", {"kind": arguments.kind})
            until = monotonic() + 480
            while monotonic() < until:
                result = client.invoke("STATUS")
                if result.get("invocation_id") != accepted["invocation_id"]:
                    raise IPCError("INVOCATION_RESULT_UNCERTAIN")
                if result["status"] != "RUNNING":
                    emit(result)
                    last = result["last_result"]
                    return (
                        0
                        if isinstance(last, dict) and last.get("status") in ("SYNCED", "SKIPPED")
                        else 1
                    )
                sleep(0.25)
            raise IPCError("JOB_WAIT_EXPIRED")
        else:
            component = getattr(arguments, "component", "engine")
            channel = "control" if component == "engine" else component
            key = role_keys(config, ("operator",))["operator"]
            client = RPCClient(
                lambda body: exchange(config.pipe_name(channel), body), "operator", key
            )
            emit(
                client.invoke(
                    "REQUEST_RECOVERY"
                    if arguments.command == "recover"
                    else arguments.command.upper().replace("-", "_")
                )
            )
        return 0
    except (KeyboardInterrupt, EOFError):
        return 130
    except IPCError as exc:
        # IPCError is restricted to protocol error codes, never raw OS/input text.
        emit({"ok": False, "code": str(exc), "real_trading_enabled": False})
        return 2
    except (ValueError, RuntimeError, OSError, pywintypes.error) as exc:
        # Validation errors can contain request input. Only safe codes cross CLI.
        if os.environ.get("ATS_DESKTOP_LOG_DIRECTORY"):
            frames = traceback.extract_tb(exc.__traceback__)
            emit(
                {
                    "diagnostic_type": type(exc).__name__,
                    "diagnostic_frames": [
                        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                        for frame in frames[-8:]
                    ],
                }
            )
        emit({"ok": False, "code": "NODE_COMMAND_FAILED", "real_trading_enabled": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
