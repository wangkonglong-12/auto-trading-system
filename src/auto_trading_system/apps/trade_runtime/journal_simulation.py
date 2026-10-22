"""Private synthetic archive + Mock-only segmented recovery acceptance.

Fixture seeding is NOT production closing/reconciliation. It creates synthetic
historical records in a newly allocated directory and uses rejected Mock orders
for today's independently queried facts. Real accounts and gateways are absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_simulation import D1_NOW, next_day_plan
from auto_trading_system.execution.dispatch_boundary import DispatchPermission, SDKDispatcher
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.checkpoint_blobs import (
    CheckpointManifest,
    read_checkpoint,
    write_checkpoint,
)
from auto_trading_system.journal.critical_models import ActionTarget, RecoveryRecord, Submission
from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.journal_scan import PrefixDigest
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference, PortfolioCheckpoint
from auto_trading_system.testing.v31_scenarios import ScenarioClock


@dataclass(frozen=True)
class CapacityFixture:
    opening: OpeningReference
    broker: MockBrokerGateway
    prefix_hash: str
    history_count: int
    current_count: int


def _manifest(store: CriticalStorePort, key: str) -> CheckpointManifest:
    root = store.read_evidence("recovery_checkpoints", key)
    if root is None:
        raise ValueError("Mock recovery root was not published")
    return CheckpointManifest.model_validate_json(root)


def seed_capacity_fixture(
    directory: Path, *, history_count: int = 3200, current_count: int = 120
) -> CapacityFixture:
    if (
        type(history_count) is not int
        or type(current_count) is not int
        or not 0 <= history_count <= 100_000
        or not 0 <= current_count <= 2048
    ):
        raise ValueError("fixture count exceeds installed test budget")
    if (directory / "critical/journal-v2.db").exists():
        raise ValueError("capacity fixture requires a fresh private Journal")
    plan = next_day_plan()
    plan_hash = digest(canonical_bytes(plan.model_dump(mode="json")))
    broker = MockBrokerGateway(ScenarioClock(D1_NOW))
    broker.start(lambda _: None)
    store = APSWCriticalStore(directory / "critical/journal-v2.db", plan.account_id)
    store.open()
    prefix = PrefixDigest()
    permission = DispatchPermission()
    dispatcher = SDKDispatcher(broker, permission)
    try:
        # 仅为私有 Mock 样本写入合成许可；运行时授权仍由正式 engine 流程产生。
        # 即使构造测试数据，也先保存证据，再经统一 SDK 边界提交。
        store.append_evidence(
            "control_records",
            "synthetic-capacity-grant",
            canonical_bytes({"mode": "SYNTHETIC_MOCK_ONLY", "permission_epoch": 2}),
        )
        permission.allow_after_durable_grant(permission.snapshot().epoch)
        for index in range(history_count + current_count):
            historical = index < history_count
            submission = Submission(
                client_order_id=f"ord_capacity_{index:08d}",
                account_id=plan.account_id,
                trade_date=DEMO_NOW.date() - timedelta(days=(history_count - index - 1) // 100)
                if historical
                else D1_NOW.date(),
                action_type="NEW",
                plan_hash=plan_hash,
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                limit_price="10",
                maximum_amount="1005",
                remark_token=f"CAP{index:08d}",
                engine_boot_id="synthetic-archive-boot",
                permission_epoch=2,
                target=ActionTarget(
                    action_id=f"archive-action-{index}",
                    target_remaining=100,
                    rule_id="simulation-v1",
                ),
            )
            saved = store.commit_submission(submission)
            if historical:
                prefix.add(
                    RecoveryRecord(
                        record_seq=saved.record_seq,
                        payload_hash=saved.payload_hash,
                        submission=submission,
                    )
                )
            else:
                assert saved.fresh and saved.payload_hash == digest(
                    canonical_bytes(submission.model_dump(mode="json"))
                )
                broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.REJECT))
                outcome = dispatcher.dispatch(submission, deadline=monotonic() + 2).result(3)
                assert outcome.status == "SDK_ACCEPTED"
        closing = PortfolioCheckpoint(
            account_id=plan.account_id,
            trade_date=DEMO_NOW.date(),
            calendar_version="offline-calendar-v1",
            recovery_report_hash=digest(b"SYNTHETIC TEST ONLY"),
            submission_high_watermark=prefix.high_watermark,
            submission_prefix_hash=prefix.payload_hash,
            lots=(),
        )
        opening = closing.reference()
        write_checkpoint(store, opening.key, closing.body())
    finally:
        dispatcher.close()
        store.close()
        broker.stop()
    return CapacityFixture(opening, broker, prefix.payload_hash, history_count, current_count)


def run_journal_simulation(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root.resolve() / ("journal-capacity-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    fixture = seed_capacity_fixture(directory)
    first_boot: str | None = None
    result: dict[str, JsonValue] = {}
    for _ in range(2):
        with open_offline_engine(
            directory,
            broker=fixture.broker,
            clock=lambda: D1_NOW,
            plan_payload=next_day_plan(),
            opening_reference=fixture.opening,
        ) as runtime:
            kernel = runtime.kernel
            kernel.start_recovery()
            state = wait_for(kernel, lambda s: s.state == "REVIEW_REQUIRED")
            assert state.recovery and state.recovery.status == "CLEAR"
            assert state.halted and state.grant_id is None
            report_id = state.recovery.report_id
            manifest = runtime.journal.request(
                partial(_manifest, key=report_id),
                deadline=monotonic() + 2,
            ).result(3)
            body = runtime.journal.request(
                partial(read_checkpoint, key=report_id), deadline=monotonic() + 2
            ).result(3)
            assert body is not None and digest(body) == manifest.payload_hash
            assert state.recovery_scanned_records == fixture.history_count + fixture.current_count
            assert state.retained_recovery_records == fixture.current_count
            if first_boot is not None:
                assert kernel.boot_id != first_boot
            first_boot = kernel.boot_id
            result = {
                "mode": "OFFLINE_JOURNAL_CAPACITY",
                "real_trading_enabled": False,
                "synthetic_archive_fixture": True,
                "restart_paused": state.halted,
                "historical_records": fixture.history_count,
                "today_records": fixture.current_count,
                "scanned_records": state.recovery_scanned_records,
                "retained_records": state.retained_recovery_records,
                "checkpoint_bytes": len(body),
                "checkpoint_fragments": len(manifest.chunks),
                "payload_verified": True,
                "recovery_status": state.recovery.status,
                "historical_rows_deleted": 0,
                "output_directory": str(directory),
            }
    (directory / "result.json").write_bytes(canonical_bytes(result))
    return result
