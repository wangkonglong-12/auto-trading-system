"""One-command V3.1 offline acceptance flow with a hard-wired Mock Broker.

This composition root cannot load QMT, production configuration or credentials.
All artifacts are written into a newly created child of the explicit output
directory. Existing user databases and the quant repository are never opened.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.domain import Money, Price, Quantity
from auto_trading_system.execution.dispatch_boundary import DispatchPermission, SDKDispatcher
from auto_trading_system.execution.submission_coordinator import SubmissionCoordinator
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.critical_models import ActionTarget, Submission
from auto_trading_system.journal.critical_writer import CriticalStorePort, open_critical_journal
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import DATASETS, DailyRecord, Dataset, DatasetStatus
from auto_trading_system.protocol.v2.plans import sign_plan
from auto_trading_system.protocol.v2.records import (
    AssetRecord,
    PlanResultRecord,
    PositionRecord,
    ReconciliationRecord,
)
from auto_trading_system.runtime.memory_state import (
    AccountObservation,
    MarketWindow,
    QuotePoint,
    ReservationBook,
)
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_export import (
    daily_record,
    export_order,
    export_submission,
    export_trade,
)
from auto_trading_system.sync.daily_files import DailyIdentity, freeze_daily
from auto_trading_system.sync.daily_upload import upload_once
from auto_trading_system.sync.plan_files import PlanFiles
from auto_trading_system.testing.daily_receiver import InMemoryDailyReceiver
from auto_trading_system.testing.v31_scenarios import ScenarioClock, example_plan


async def run_simulation(output_root: Path) -> dict[str, JsonValue]:
    run_dir = output_root.resolve() / ("simulation-" + uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    plan = example_plan()
    account_id = plan.account_id
    private = Ed25519PrivateKey.generate()
    signed = sign_plan(plan, "ephemeral-simulation-key", private)
    loader = PlanFiles(
        ArtifactFiles(run_dir / "plans"), {signed.envelope.key_id: private.public_key()}
    )
    plan_file_hash = loader.load(
        canonical_bytes(signed.model_dump(mode="json")),
        account_id=account_id,
        node_generation=1,
        now=plan.source.completed_at,
    )
    permission = DispatchPermission()
    journal = open_critical_journal(run_dir / "critical" / "journal-v2.db", account_id)
    broker = MockBrokerGateway(ScenarioClock(plan.source.completed_at))
    broker.start(lambda _: None)  # Offline demo queries the deterministic broker after each step.
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(50),
            fill_price=Price.parse("10"),
            fill_notional=Money.parse("500"),
        )
    )
    dispatcher = SDKDispatcher(broker, permission)
    market = MarketWindow()
    sequence = 0
    try:
        # This is a simulation-only grant, saved before permissions expand. A real
        # operator authorization has a separate authenticated local entry point.
        grant = canonical_bytes({"mode": "SIMULATION", "boot_id": "simulation-boot", "epoch": 2})
        journal.request(
            lambda store: store.append_evidence("control_records", "simulation-grant", grant),
            deadline=monotonic() + 2,
        ).result(timeout=3)
        permission.allow_after_durable_grant(1)
        loader.mark_armed()
        order = Submission(
            client_order_id="ord_simulation_buy1",
            account_id=account_id,
            trade_date=plan.trade_date,
            action_type="NEW",
            plan_hash=signed.envelope.payload_hash,
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            limit_price="10",
            maximum_amount="1005",
            remark_token="SIMULATION1",
            engine_boot_id="simulation-boot",
            permission_epoch=2,
            target=ActionTarget(
                action_id="simulation-action", target_remaining=100, rule_id="entry-v1"
            ),
        )
        reservations = ReservationBook()
        reservations.reserve(
            order, AccountObservation("initial", Money.parse("10000"), frozenset(), (), True)
        )
        coordinator = SubmissionCoordinator(journal, dispatcher, permission, lambda _: True)

        async def wait_for_status(key: str) -> str:
            nonlocal sequence
            deadline = monotonic() + 3
            while monotonic() < deadline:
                sequence += 1
                market.add(QuotePoint(sequence, sequence, Price.parse("10")))
                state = {item.client_order_id: item.status for item in coordinator.poll()}[key]
                if state not in ("PERSISTING", "DISPATCHING"):
                    return state
                await asyncio.sleep(0.001)
            raise RuntimeError("simulation dispatch did not settle")

        coordinator.begin(order, valid_until=monotonic() + 2)
        if await wait_for_status(order.client_order_id) != "SDK_ACCEPTED":
            raise RuntimeError("simulated buy did not reach the broker")
        broker_order = broker.query_orders()[0]
        assert broker_order.broker_order_id is not None
        cancellation = order.model_copy(
            update={
                "client_order_id": "ord_simulation_cancel1",
                "action_type": "CANCEL",
                "original_order_id": broker_order.broker_order_id,
                "original_client_order_id": order.client_order_id,
            }
        )
        coordinator.begin(cancellation, valid_until=monotonic() + 2)
        if await wait_for_status(cancellation.client_order_id) != "SDK_ACCEPTED":
            raise RuntimeError("simulated cancellation did not reach the broker")
        orders = broker.query_orders()
        trades = broker.query_trades()
        assert orders[0].is_terminal and orders[0].filled_quantity.value == 50
        reservations.resolve_terminal(order.client_order_id, reconciled=True)
        recovery_records = journal.recovery_page().result(timeout=2)
        now = datetime.fromisoformat("2026-09-07T15:30:00+08:00")
        records: dict[Dataset, tuple[DailyRecord, ...]] = dict.fromkeys(DATASETS, ())
        records["plan_results"] = (
            daily_record(
                plan.plan_id,
                PlanResultRecord(
                    account_id=account_id,
                    trade_date=plan.trade_date,
                    observed_at=now,
                    plan_id=plan.plan_id,
                    plan_hash=signed.envelope.payload_hash,
                    allocation_id=plan.allocations[0].allocation_id,
                    symbol=order.symbol,
                    target_quantity=100,
                    filled_quantity=50,
                    status="PARTIAL",
                    reason_code="MOCK_CANCEL_REMAINDER",
                ),
            ),
        )
        records["submission_records"] = tuple(
            export_submission(item, observed_at=now, status="RESOLVED") for item in recovery_records
        )
        records["broker_orders"] = tuple(
            export_order(item, account_id=account_id, trade_date=plan.trade_date, observed_at=now)
            for item in orders
        )
        records["broker_trades"] = tuple(
            export_trade(
                item,
                account_id=account_id,
                trade_date=plan.trade_date,
                observed_at=now,
                origin="STRATEGY",
            )
            for item in trades
        )
        for item in (*records["broker_orders"], *records["broker_trades"]):
            body = canonical_bytes(item.model_dump(mode="json"))

            def write_fact(
                store: CriticalStorePort, key: str = item.record_id, body: bytes = body
            ) -> bool:
                return store.append_evidence("broker_facts", key, body)

            journal.request(
                write_fact,
                deadline=monotonic() + 2,
            ).result(timeout=3)
        records["positions"] = (
            daily_record(
                "000001.SZ",
                PositionRecord(
                    account_id=account_id,
                    trade_date=plan.trade_date,
                    observed_at=now,
                    snapshot_id="mock-close",
                    symbol="000001.SZ",
                    total_quantity=50,
                    available_quantity=0,
                    frozen_quantity=0,
                    strategy_quantity=50,
                    market_value="500",
                    cost_amount=None,
                    attribution_status="MATCHED",
                ),
            ),
        )
        records["assets"] = (
            daily_record(
                "closing",
                AssetRecord(
                    account_id=account_id,
                    trade_date=plan.trade_date,
                    observed_at=now,
                    snapshot_id="mock-close",
                    cash_available="9500",
                    cash_frozen="0",
                    market_value="500",
                    total_equity="10000",
                    quality="CONFIRMED",
                ),
            ),
        )
        records["reconciliations"] = (
            daily_record(
                "closing",
                ReconciliationRecord(
                    account_id=account_id,
                    trade_date=plan.trade_date,
                    observed_at=now,
                    reconciliation_id="mock-close",
                    scope="POSITIONS",
                    symbol="000001.SZ",
                    status="MATCHED",
                    expected_quantity=50,
                    observed_quantity=50,
                    reason_code="MOCK_SHARES_MATCH",
                ),
            ),
        )
        files = ArtifactFiles(run_dir / "daily")
        frozen = freeze_daily(
            DailyIdentity(
                batch_id="simulation-batch-1",
                node_id="simulation-node",
                node_generation=1,
                account_id=account_id,
                trade_date=plan.trade_date,
                revision=1,
                generated_at=now,
                data_as_of=now,
                journal_generation="simulation-journal",
                last_record_seq=2,
            ),
            records,
            tuple(
                DatasetStatus(
                    dataset=dataset,
                    status="COMPLETE",
                    record_count=len(records[dataset]),
                )
                for dataset in DATASETS
            ),
            files,
        )
        uploaded = upload_once(frozen, files, InMemoryDailyReceiver())
        if uploaded.status != "SYNCED":
            raise RuntimeError("simulated daily upload failed")
    finally:
        dispatcher.close()
        broker.stop()
        if not journal.close():
            raise RuntimeError("journal owner did not stop; replacement is unsafe")
    reopened = open_critical_journal(run_dir / "critical" / "journal-v2.db", account_id)
    try:
        recovered_count = len(reopened.recovery_page().result(timeout=2))
        restart_paused = DispatchPermission().snapshot().halted
    finally:
        reopened.close()
    result: dict[str, JsonValue] = {
        "mode": "SIMULATION",
        "real_trading_enabled": False,
        "output_directory": str(run_dir),
        "plan_artifact_hash": plan_file_hash,
        "submission_records": recovered_count,
        "filled_quantity": 50,
        "cancelled_remaining_quantity": 50,
        "market_events_processed": sequence,
        "restart_paused": restart_paused,
        "daily_upload_status": uploaded.status,
        "daily_manifest_artifact_hash": frozen.artifact_hash,
        "financial_status": "PROVISIONAL",
        "remote": "IN_MEMORY_TEST_RECEIVER",
    }
    with (run_dir / "result.json").open("xb") as output:
        output.write(canonical_bytes(result))
    return result
