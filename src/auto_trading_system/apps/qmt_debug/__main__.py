"""启动受控的真实QMT节点和本机页面，不注册系统任务。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from auto_trading_system.apps.desktop.session import PreviewSession
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.monitoring.qmt_readonly import write_immutable_probe_payload
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.security.operator_password import OperatorPasswordHasher

_SDK_ARCHIVE_HASH = "F096D36E0682D1BABEE90148A0124ED2FB98280CBC9FFAEE2631A547D9824205"


class QmtDebugSession(PreviewSession):
    """复用自有Job/本机HTTP编排；明确选择只读档或报撤单调试档。"""

    def __init__(
        self,
        directory: Path,
        environment_root: Path,
        qmt_root: Path,
        fingerprint: str,
        session_base: int,
        *,
        trading: bool = False,
        operator_password: str | None = None,
    ) -> None:
        super().__init__(directory)
        sdk_root = environment_root / "sdk"
        expected_python = environment_root / "env/Scripts/python.exe"
        manifest = environment_root / "sdk-manifest.json"
        if (
            not environment_root.is_absolute()
            or environment_root.resolve() != environment_root.absolute()
            or not expected_python.is_file()
            or Path(sys.executable).resolve() != expected_python.resolve()
            or not (sdk_root / "xtquant/xttrader.py").is_file()
            or not manifest.is_file()
        ):
            raise ValueError("prepared QMT environment is invalid")
        declared = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            declared.get("schema") != "qmt-sdk-environment/v1"
            or declared.get("archive_sha256") != _SDK_ARCHIVE_HASH
            or declared.get("python") != "3.11 x64"
        ):
            raise ValueError("prepared QMT SDK manifest is incompatible")
        userdata = qmt_root / "userdata_mini"
        if (
            not qmt_root.is_absolute()
            or qmt_root.resolve() != qmt_root.absolute()
            or not (userdata / "up_queue_xtquant").exists()
        ):
            raise ValueError("QMT trading queue is unavailable")
        values = self.config.model_dump(mode="json")
        values.update(
            broker="QMT_TRADING" if trading else "QMT_READ_ONLY",
            account_id="qmt-" + fingerprint,
            qmt_read_only={
                "userdata_mini": str(userdata),
                "account_fingerprint": fingerprint,
                "session_base": session_base,
            },
        )
        if trading:
            if operator_password is None:
                raise ValueError("QMT trading debug requires an operator password")
            verifier_variable = "ATS_SECRET_DESKTOP_QMT_VERIFIER"
            values["operator_access"] = {
                "actor_id": "local-qmt-operator",
                "password_verifier": {
                    "source": "ENVIRONMENT",
                    "variable": verifier_variable,
                },
            }
            self.environment[verifier_variable] = OperatorPasswordHasher().hash_password(
                operator_password
            )
        else:
            values["operator_access"] = None
            self.environment.pop("ATS_SECRET_DESKTOP_VERIFIER", None)
        self.config = NodeProcessConfig.model_validate_json(canonical_bytes(values))
        self.trading = trading
        source_root = Path(__file__).resolve().parents[3]
        self.environment["PYTHONPATH"] = os.pathsep.join((str(source_root), str(sdk_root)))
        self.environment.pop("ATS_QMT_READONLY_OUTPUT", None)
        self.environment.pop("ATS_QMT_OPERATOR_PASSWORD", None)

    def _validate_ready_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("real_trading_enabled") is not self.trading:
            raise ValueError("QMT debug profile does not match requested mode")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="中金QMT真实账户节点调试")
    parser.add_argument("--environment-root", type=Path, required=True)
    parser.add_argument("--qmt-root", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True, help="父目录已存在的全新绝对目录")
    parser.add_argument("--account-fingerprint", required=True)
    parser.add_argument("--session-base", type=int, default=6102301)
    parser.add_argument("--self-test", action="store_true", help="有界启动、验收并停止")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--trading",
        action="store_true",
        help="启动独立QMT报撤单调试档；页面保持零额度，不会自动发出新委托",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session: QmtDebugSession | None = None
    result: dict[str, Any] | None = None
    failed = False
    try:
        if not args.directory.is_absolute() or args.directory.exists():
            raise ValueError("debug directory must be a new absolute path")
        password = os.environ.get("ATS_QMT_OPERATOR_PASSWORD") if args.trading else None
        session = QmtDebugSession(
            args.directory,
            args.environment_root,
            args.qmt_root,
            args.account_fingerprint,
            args.session_base,
            trading=args.trading,
            operator_password=password,
        )
        session.start()
        until = monotonic() + 30
        status: dict[str, Any] = {}
        while monotonic() < until:
            status = session.worker_status()
            if status.get("broker_account") is not None:
                break
            sleep(0.2)
        expected_broker = "QMT_TRADING" if args.trading else "QMT_READ_ONLY"
        if status.get("broker") != expected_broker or status.get("broker_account") is None:
            raise RuntimeError("real QMT account facts are unavailable")
        result = {
            "schema": "qmt-node-debug/v1",
            "status": "PASS",
            "broker": expected_broker,
            "real_trading_enabled": args.trading,
            "engine_state": status.get("state"),
            "account": status["broker_account"],
            "url": session.url,
        }
        if not args.self_test:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            if not args.no_browser:
                webbrowser.open(session.url)
            while True:
                sleep(0.25)
    except KeyboardInterrupt:
        pass
    except Exception:
        failed = True
    finally:
        if session is not None:
            try:
                session.stop()
                if not session.stopped_cleanly:
                    failed = True
            except Exception:
                failed = True
    if failed or result is None or session is None:
        print("QMT_NODE_DEBUG_FAILED", file=sys.stderr)
        return 2
    if args.self_test:
        result["owned_processes_closed"] = True
        write_immutable_probe_payload(session.directory / "execution.json", result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
