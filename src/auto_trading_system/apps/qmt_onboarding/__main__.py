"""发现账户并复用三类只读探针，SDK 始终运行在可回收的自有子进程。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

from auto_trading_system.control.windows_pipe import current_sid
from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.broker.cicc_qmt import (
    CiccQmtReadOnlyClient,
    discover_qmt_accounts,
)
from auto_trading_system.gateways.broker.cicc_qmt_facts import CiccQmtFactsClient
from auto_trading_system.gateways.broker.cicc_qmt_snapshot import CiccQmtSnapshotClient
from auto_trading_system.gateways.market.qmt import CiccQmtTickProbeClient
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeConfig,
    QmtReadOnlyAdapterError,
    QmtSignal,
    QmtSignalKind,
    run_qmt_readonly_probe,
    write_immutable_probe_payload,
    write_qmt_probe_report,
)
from auto_trading_system.monitoring.qmt_snapshot import (
    run_qmt_snapshot_probe,
    write_qmt_snapshot_report,
)
from auto_trading_system.monitoring.qmt_tick import (
    QmtTickProbeConfig,
    QmtTickScenario,
    run_qmt_tick_probe,
    write_qmt_tick_report,
)
from auto_trading_system.runtime.node_layout import create_private_directory
from auto_trading_system.runtime.owned_process import OwnedProcess
from auto_trading_system.runtime.single_instance import create_account_instance_guard


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def run_checks(args: argparse.Namespace) -> int:
    """安装锁串行化本工具；账户锁与已有正式探针完全相同。"""
    config = QmtProbeConfig(args.userdata_mini, args.session_id, "STOCK", args.seconds)
    scope_hash = hashlib.sha256(
        json.dumps(
            {
                "path": str(config.userdata_mini.resolve()),
                "session": config.session_id,
                "scenario": args.scenario,
                "symbols": args.symbol,
                "seconds": args.seconds,
                "market_only": args.market_only,
                "facts": args.facts,
                "account_fingerprint": args.account,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    summary: dict[str, object] = {
        "schema": "qmt-onboarding/v1",
        "mode": "READ_ONLY",
        "status": "BLOCKED",
        "real_trading_enabled": False,
        "stages": {},
    }
    install_guard = create_account_instance_guard(
        "QmtReadonly" + fingerprint(str(config.userdata_mini.resolve()).casefold())
    )
    install_guard.acquire()
    accounts: tuple[str, ...] = ()
    try:
        if args.market_only:
            tick = run_qmt_tick_probe(
                QmtTickProbeConfig(
                    tuple(SecurityCode.parse(item) for item in args.symbol),
                    QmtTickScenario(args.scenario),
                    args.seconds,
                ),
                config_hash=scope_hash,
                client_factory=CiccQmtTickProbeClient,
            )
            write_qmt_tick_report(args.output / "tick.json", tick)
            summary.update(
                status=tick.outcome.value,
                failure_code=tick.failure_code,
                stages={"tick": tick.outcome.value},
                account_checks="NOT_REQUESTED",
            )
            return tick.exit_code
        discovered = discover_qmt_accounts(config)
        summary["discovery_connection"] = "PASS"
        summary["discovered_account_count"] = len(discovered)
        summary["discovered_account_types"] = sorted({item.account_type for item in discovered})
        summary["discovered_account_sources"] = sorted({item.source for item in discovered})
        accounts = tuple(item.account_id for item in discovered)
        summary["account_fingerprints"] = [fingerprint(item) for item in accounts]
        selected = [
            item.account_id
            for item in discovered
            if item.account_type == 2
            and (not args.account or fingerprint(item.account_id) == args.account)
        ]
        if len(selected) != 1:
            summary["failure_code"] = (
                "QMT_ACCOUNT_SELECTION_REQUIRED"
                if len(selected) > 1 or args.account
                else "QMT_NO_STOCK_ACCOUNT"
            )
            return 2
        account = selected[0]
        summary["selected_account_fingerprint"] = fingerprint(account)
        guard = create_account_instance_guard(account)
        guard.acquire()
        try:
            # 同一稳定 session 的两次 connect 间隔必须超过三秒。
            sleep(3.1)
            first = run_qmt_readonly_probe(
                replace(config, session_id=config.session_id + 1),
                account_id=account,
                config_hash=scope_hash,
                client_factory=CiccQmtReadOnlyClient,
            )
            write_qmt_probe_report(
                args.output / "connection.json", first, forbidden_values=accounts
            )
            stages = {"connection": first.outcome.value}
            summary["stages"] = stages
            if first.exit_code:
                summary["status"] = first.outcome.value
                summary["failure_code"] = first.failure_code
                return first.exit_code
            sleep(3.1)
            snapshot = run_qmt_snapshot_probe(
                replace(config, session_id=config.session_id + 2),
                account_id=account,
                config_hash=scope_hash,
                client_factory=CiccQmtSnapshotClient,
            )
            write_qmt_snapshot_report(
                args.output / "snapshot.json", snapshot, forbidden_values=accounts
            )
            stages["snapshot"] = snapshot.outcome.value
            if snapshot.exit_code:
                summary["status"] = snapshot.outcome.value
                summary["failure_code"] = snapshot.failure_code
                return snapshot.exit_code
            tick_config = QmtTickProbeConfig(
                tuple(SecurityCode.parse(item) for item in args.symbol),
                QmtTickScenario(args.scenario),
                args.seconds,
            )
            tick = run_qmt_tick_probe(
                tick_config, config_hash=scope_hash, client_factory=CiccQmtTickProbeClient
            )
            write_qmt_tick_report(args.output / "tick.json", tick, forbidden_values=accounts)
            stages["tick"] = tick.outcome.value
            summary["status"] = tick.outcome.value
            summary["failure_code"] = tick.failure_code
            if tick.exit_code or not args.facts:
                return tick.exit_code
            # 第四个独立 session 读取真实数值；结果只在内存中存在。
            sleep(3.1)
            facts_config = replace(config, session_id=config.session_id + 3)
            signals: list[QmtSignal] = []

            def accept_signal(signal: QmtSignal) -> None:
                if len(signals) < 33:
                    signals.append(signal)

            stages["facts"] = "ERROR"
            facts_client = CiccQmtFactsClient(facts_config, account)
            subscribed = False
            try:
                facts_client.register_signal_sink(accept_signal)
                facts_client.start()
                if facts_client.connect() != 0:
                    raise QmtReadOnlyAdapterError("QMT_FACT_CONNECT_FAILED", "connection failed")
                if facts_client.subscribe() != 0:
                    raise QmtReadOnlyAdapterError(
                        "QMT_FACT_SUBSCRIBE_FAILED", "subscription failed"
                    )
                subscribed = True
                statuses = facts_client.query_account_statuses().target_statuses
                if len(statuses) != 1 or statuses[0].status != 0:
                    raise QmtReadOnlyAdapterError("QMT_FACT_ACCOUNT_NOT_OK", "account unavailable")
                observed = facts_client.read_facts("qmt-" + fingerprint(account), datetime.now(UTC))
                if len(signals) > 32 or any(
                    signal.kind is not QmtSignalKind.ACCOUNT_STATUS or signal.status != 0
                    for signal in signals
                ):
                    raise QmtReadOnlyAdapterError("QMT_FACT_SIGNAL_INVALID", "connection changed")
                if facts_client.unsubscribe() != 0:
                    raise QmtReadOnlyAdapterError("QMT_FACT_UNSUBSCRIBE_FAILED", "cleanup failed")
                subscribed = False
                if len(signals) > 32 or any(
                    signal.kind is not QmtSignalKind.ACCOUNT_STATUS or signal.status != 0
                    for signal in signals
                ):
                    raise QmtReadOnlyAdapterError("QMT_FACT_SIGNAL_INVALID", "connection changed")
            finally:
                try:
                    if subscribed and facts_client.unsubscribe() != 0:
                        raise QmtReadOnlyAdapterError(
                            "QMT_FACT_UNSUBSCRIBE_FAILED", "cleanup failed"
                        )
                finally:
                    facts_client.stop()
            summary["facts"] = {
                "source": "QMT",
                "position_count": len(observed.account.positions),
                "order_count": observed.order_count,
                "trade_count": observed.trade_count,
                "broker_cash_mapped": True,
                "coherent_for_order_recovery": observed.account.coherent,
                "existing_positions_unattributed": len(observed.account.positions) > 0,
            }
            stages["facts"] = "PASS"
            return 0
        finally:
            guard.release()
    except QmtReadOnlyAdapterError as exc:
        summary.update(status="ERROR", failure_code=exc.code)
        return 3
    except Exception:
        summary.update(status="ERROR", failure_code="QMT_ONBOARDING_FAILED")
        return 3
    finally:
        # 无法确认锁释放则拒绝发布看起来成功的汇总。
        install_guard.release()
        write_immutable_probe_payload(
            args.output / "summary.json", summary, forbidden_values=accounts
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="中金 QMT 只读接入验收，不具有报撤单入口")
    parser.add_argument("--userdata-mini", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="父目录已存在的全新绝对目录")
    parser.add_argument("--session-id", type=int, default=6101901)
    parser.add_argument("--seconds", type=int, choices=range(1, 31), default=5)
    parser.add_argument("--scenario", choices=("ACTIVE", "CLOSED"), required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--account", help="多账户时明确选择发现报告中的16位账户指纹")
    parser.add_argument("--market-only", action="store_true", help="独立验证行情，不查询交易账户")
    parser.add_argument(
        "--facts", action="store_true", help="验证真实资金与持仓模型映射，报告不包含金额"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        QmtProbeConfig(args.userdata_mini, args.session_id, "STOCK", args.seconds)
        max_session = 2_147_483_644 if args.facts else 2_147_483_645
        if args.session_id > max_session:
            raise ValueError("session base must reserve stable probe session ids")
        if args.market_only and args.facts:
            raise ValueError("facts require an account probe")
        QmtTickProbeConfig(
            tuple(SecurityCode.parse(s) for s in args.symbol),
            QmtTickScenario(args.scenario),
            args.seconds,
        )
        if not args.output.is_absolute() or args.output.resolve() != args.output.absolute():
            raise ValueError("direct absolute output required")
        if args.worker:
            if os.environ.get("ATS_QMT_READONLY_OUTPUT") != str(args.output):
                raise ValueError("internal worker requires its parent's output binding")
            # SDK 的日志可能包含账号，统一丢弃；只有脱敏后的类型化报告可落盘。
            with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                return run_checks(args)
        create_private_directory(args.output, current_sid())
        environment = dict(os.environ)
        environment["ATS_QMT_READONLY_OUTPUT"] = str(args.output)
        child = OwnedProcess(
            [
                str(Path(sys.executable).absolute()),
                "-m",
                "auto_trading_system.apps.qmt_onboarding",
                "--worker",
                *argv,
            ],
            environment=environment,
        )
        try:
            try:
                code = child.wait(60 + args.seconds * 3)
            except TimeoutError:
                child.terminate()
                child.wait(5)
                code = 3
        finally:
            child.close()
        result_path = args.output / "summary.json"
        if not result_path.is_file():
            code = 3
            write_immutable_probe_payload(
                result_path,
                {
                    "schema": "qmt-onboarding/v1",
                    "mode": "READ_ONLY",
                    "status": "ERROR",
                    "failure_code": "QMT_WORKER_FAILED_OR_TIMEOUT",
                    "real_trading_enabled": False,
                },
            )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if code != 0 and result.get("status") == "PASS":
            result.update(status="ERROR", failure_code="QMT_WORKER_EXIT_FAILED")
        result["worker_exit_code"] = code
        # 父进程的最终结果在自有 Job 关闭后生成，不能以子进程中间报告替代。
        write_immutable_probe_payload(args.output / "execution.json", result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return code
    except Exception:
        print("QMT_ONBOARDING_INPUT_OR_OWNERSHIP_FAILED", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
