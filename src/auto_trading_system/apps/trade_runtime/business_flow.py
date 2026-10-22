"""Two-day business acceptance through real local ports and a signed test peer.

Mock broker, market/calendar/cost fixtures and in-memory HTTP peer are explicit.
No production configuration, task registration, external server or live account
can be selected. Unlike isolated demos, both days share one Journal and upload
the exact engine-captured results to the same receiver for business queries.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from secrets import token_bytes
from time import monotonic, sleep
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.entry_simulation import (
    entry_fixture,
    publish_entry,
    pump_entry,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import (
    ExitScenarioClock,
    fixture_context,
    publish,
    pump,
)
from auto_trading_system.apps.trade_runtime.exit_simulation import D1_NOW, fill_scenario
from auto_trading_system.control.plan_delivery import PlanDeliveryMailbox
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import sign_plan
from auto_trading_system.runtime.day_snapshot import DaySnapshot
from auto_trading_system.runtime.single_instance import WindowsNamedMutexBackend
from auto_trading_system.strategies.entry_gate import EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.exit_owner import D1StrategyOwner, ExitStateGate
from auto_trading_system.sync.agent_jobs import AgentJobs
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.engine_daily import freeze_engine_day
from auto_trading_system.sync.http_v2 import HttpQuantTransport, NodeCredentials, SignedHttpClient
from auto_trading_system.sync.job_store import JobStore, SyncJob
from auto_trading_system.testing.business_remote import BusinessRemote


def run_business_flow(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root.resolve() / ("business-flow-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    clock = ExitScenarioClock(DEMO_NOW)
    signer = Ed25519PrivateKey.generate()
    payload, entry_policy = entry_fixture()
    plan = sign_plan(payload, "offline-ephemeral-key", signer)
    credentials = NodeCredentials("simulation-node", "offline-http-key", token_bytes(32))
    remote = BusinessRemote(plan, credentials, lambda: clock.instant.timestamp())
    client = SignedHttpClient(
        "https://offline-quant.test",
        credentials,
        clock=lambda: clock.instant.timestamp(),
        transport=httpx.MockTransport(remote.handle),
    )
    transport = HttpQuantTransport(client)
    files = ArtifactFiles(directory / "plans")
    mailbox = PlanDeliveryMailbox()

    def agent() -> AgentJobs:
        return AgentJobs(
            files,
            JobStore(
                directory / "jobs",
                node_id=credentials.node_id,
                account_id=plan.payload.account_id,
                backend=WindowsNamedMutexBackend(),
            ),
            transport,
            mailbox,
            node_id=credentials.node_id,
            account_id=plan.payload.account_id,
            generation=1,
            strategy_id=plan.payload.strategy_id,
            trusted_keys={plan.envelope.key_id: signer.public_key()},
            clock=lambda: clock.instant,
            is_trading_day=lambda day: day in (DEMO_NOW.date(), D1_NOW.date()),
        )

    def pull() -> SyncJob | None:
        task = agent()
        with task.store:
            return task.pull_plan()

    def upload(snapshot: DaySnapshot, *, lose_ack: bool = False) -> tuple[str, bool]:
        # Independent agent invocation, outside the engine owner/lifecycle.
        frozen = freeze_engine_day(
            snapshot,
            files,
            node_id=credentials.node_id,
            node_generation=1,
            journal_generation="offline-shared-journal-1",
            generated_at=clock.instant,
        )
        remote.drop_complete_once = lose_ack
        task = agent()
        with task.store:
            first = task.run_job(task.register_daily(frozen))
        if lose_ack:
            assert first.state == "PENDING" and remote.receiver.receipt(first.job_id)
        # Recreate the agent and reopen the same durable job index as a later task run.
        task = agent()
        with task.store:
            outcomes = task.retry_retained()
        assert all(job.state == "SYNCED" for job in outcomes)
        assert remote.receiver.receipt(first.job_id) is not None
        return first.job_id, lose_ack and first.state == "PENDING"

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-agent") as worker:
            broker = MockBrokerGateway(clock)
            entry_gate = EntryStateGate(
                payload,
                entry_policy,
                clock=lambda: clock.instant,
                ns_clock=lambda: clock.nanoseconds,
            )
            with open_offline_engine(
                directory,
                broker=broker,
                clock=lambda: clock.instant,
                preload_plan=False,
                signing_key=signer,
                plan_payload=payload,
                entry_state_guard=entry_gate.allows,
                entry_target_reader=entry_gate.export_targets,
            ) as runtime:
                engine = runtime.kernel
                assert engine.plans.plans.loaded() is None
                pending = worker.submit(pull)
                until = monotonic() + 8
                while not pending.done() and monotonic() < until:
                    mailbox.poll(engine.apply)
                    engine.poll()
                    sleep(0.001)
                pulled = pending.result(timeout=1)
                assert pulled and pulled.state == "SYNCED"
                assert engine.plans.plans.loaded() == plan and engine.snapshot().halted
                # A repeated scheduled invocation must not reload/arm or create another receipt.
                repeat = worker.submit(pull)
                until = monotonic() + 8
                while not repeat.done() and monotonic() < until:
                    mailbox.poll(engine.apply)
                    engine.poll()
                    sleep(0.001)
                assert repeat.result(timeout=1) == pulled and len(remote.acks) == 1
                engine.start_recovery()
                wait_for(engine, lambda state: state.state == "REVIEW_REQUIRED")
                entry_owner = EntryStrategyOwner(engine, entry_gate)
                pump_entry(entry_owner, lambda: entry_owner.snapshot().state == "READY")
                runtime.confirmed_arm()
                broker.queue_submit(fill_scenario(100, full=True))
                publish_entry(entry_owner, clock, 1)
                pump_entry(
                    entry_owner,
                    lambda: dict(entry_owner.snapshot().progress).get("000001.SZ") == "FINISHED",
                )
                clock.advance(20 * 60)
                engine.start_recovery()
                wait_for(engine, lambda state: state.state == "REVIEW_REQUIRED")
                buy_day = engine.capture_day()
                opening = engine.seal_portfolio().result(timeout=3)
                assert buy_day.lots[0].remaining == 100
            clock.instant = DEMO_NOW.replace(hour=15, minute=30)
            buy_batch, ack_repaired = worker.submit(upload, buy_day, lose_ack=True).result(
                timeout=10
            )

            # Same Journal/lot, independently queried next-day broker. Carry actual
            # prior-day cash, not a fresh reset to the fixture's initial 10,000.
            clock.instant = D1_NOW
            context = fixture_context(directory, opening)
            gate = ExitStateGate(
                context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds
            )
            broker = MockBrokerGateway(clock)
            with open_offline_engine(
                directory,
                broker=broker,
                clock=lambda: clock.instant,
                opening_reference=opening,
                broker_opening_quantity=100,
                broker_opening_cash=buy_day.broker.account.cash,
                exit_context=context,
                exit_state_guard=gate.allows,
            ) as runtime:
                engine = runtime.kernel
                owner = D1StrategyOwner(engine, gate)
                engine.start_recovery()
                wait_for(engine, lambda state: state.state == "REVIEW_REQUIRED")
                pump(owner, lambda: owner.snapshot().state == "READY")
                assert engine.snapshot().halted and engine.snapshot().grant_id is None
                assert engine.snapshot().lots[0].lot_id == buy_day.lots[0].lot_id
                runtime.confirmed_arm(directions=("SELL",))
                broker.queue_submit(fill_scenario(100, full=True))
                publish(owner, clock, "10.7", 1)
                pump(
                    owner, lambda: engine.snapshot().lots[0].remaining == 0 and engine.critical_idle
                )
                clock.instant = D1_NOW.replace(hour=15, minute=1)
                engine.start_recovery()
                wait_for(engine, lambda state: state.state == "REVIEW_REQUIRED")
                sell_day = engine.capture_day()
                engine.seal_portfolio().result(timeout=3)
                assert sell_day.broker.account.cash == "10000.00"
            clock.instant = D1_NOW.replace(hour=15, minute=30)
            sell_batch, _ = worker.submit(upload, sell_day).result(timeout=10)

        days: list[JsonValue] = []
        for snapshot, batch_id in ((buy_day, buy_batch), (sell_day, sell_batch)):
            day = snapshot.report.trade_date.isoformat()
            receipt = remote.receiver.current(plan.payload.account_id, day)
            assert (
                receipt
                and receipt.batch_id == batch_id
                and receipt.financial_status == "PROVISIONAL"
            )
            trades = remote.receiver.records(plan.payload.account_id, day, "broker_trades")
            assert len(trades) == 1 and trades[0].data["quantity"] == 100
            asset = remote.receiver.records(plan.payload.account_id, day, "assets")[0]
            days.append(
                {
                    "trade_date": day,
                    "batch_id": batch_id,
                    "trade": trades[0].data,
                    "assets": asset.data,
                    "business_status": receipt.business_status,
                    "financial_status": receipt.financial_status,
                }
            )
        assert days[0] and days[1]
        result: dict[str, JsonValue] = {
            "mode": "OFFLINE_BUSINESS_FLOW",
            "real_trading_enabled": False,
            "transport": "SIGNED_HTTP_MOCK_TRANSPORT",
            "receiver": "IN_MEMORY_TEST_PEER",
            "plan_receipts": len(remote.acks),
            "ack_loss_repaired": ack_repaired,
            "same_lot_across_days": True,
            "restart_paused": True,
            "bought_quantity": 100,
            "sold_quantity": 100,
            "remaining_quantity": 0,
            "final_cash": sell_day.broker.account.cash,
            "days": days,
            "http_requests": remote.requests,
            "output_directory": str(directory),
        }
        (directory / "result.json").write_bytes(canonical_bytes(result))
        return result
    finally:
        mailbox.close()
        client.close()
