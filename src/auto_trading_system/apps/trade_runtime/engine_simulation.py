"""Managed offline composition of the V2 kernel, using only a Mock Broker.

There is no production configuration loader or live gateway option. Each CLI
run owns a new directory, and account ownership is acquired before opening the
Journal. Reopening the same demo Journal creates a fresh boot and stays paused.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.domain import Money, Price, Quantity
from auto_trading_system.execution.dispatch_boundary import DispatchPermission, SDKDispatcher
from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import ActionTarget, Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.protocol.v2.plans import PlanEnvelope, PlanPayload, sign_plan
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference
from auto_trading_system.reconcile.position_lots import ExchangeCalendar
from auto_trading_system.runtime.engine_v2 import EngineKernel, EngineLimits, EngineSnapshot
from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact
from auto_trading_system.runtime.single_instance import (
    AccountInstanceGuard,
    WindowsNamedMutexBackend,
)
from auto_trading_system.strategies.entry_state import EntryTarget
from auto_trading_system.strategies.exit_context import ExitSessionContext
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.testing.v31_scenarios import ScenarioClock, example_plan

DEMO_NOW = datetime.fromisoformat("2026-09-07T14:41:00+08:00")


@dataclass
class OfflineEngine:
    kernel: EngineKernel
    broker: MockBrokerGateway
    plan: PlanEnvelope
    approvals: OneShotApprovals
    journal: CriticalJournalOwner
    clock: Callable[[], datetime]

    def request(
        self, action: str, *, grant: SessionGrant | None = None, artifact_hash: str | None = None
    ) -> LocalControlRequest:
        expands = action in ("ARM", "RESUME", "CONFIRM_RECOVERY")
        return LocalControlRequest.model_validate_json(
            canonical_bytes(
                {
                    "request_id": "control-" + uuid4().hex,
                    "engine_boot_id": self.kernel.boot_id,
                    "sender_role": "agent",
                    "deadline": (self.clock() + timedelta(seconds=10)).isoformat(),
                    "expected_state_version": self.kernel.snapshot().state_version,
                    "action": action,
                    "grant": grant.model_dump(mode="json") if grant else None,
                    "artifact_hash": artifact_hash,
                    "operator_authorization_id": "test-approval-" + uuid4().hex
                    if expands
                    else None,
                }
            )
        )

    def confirmed_arm(
        self, *, directions: tuple[Literal["BUY", "SELL"], ...] = ("BUY",)
    ) -> SessionGrant:
        report = self.kernel.snapshot().recovery
        if report is None:
            raise ValueError("recover before granting")
        confirm = self.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
        self.approvals.approve(confirm, authenticated_actor="offline-test-operator")
        if self.kernel.control(confirm).status != "PENDING":
            raise RuntimeError("offline recovery confirmation rejected")
        wait_for(self.kernel, lambda s: s.state == "READY_PAUSED")
        grant = SessionGrant(
            grant_id="grant-" + uuid4().hex,
            account_id=self.kernel.account_id,
            engine_boot_id=self.kernel.boot_id,
            session_generation=1,
            mode="PAPER",
            directions=directions,
            max_buy_value=self.kernel.limits.max_buy_value,
            max_order_value=self.kernel.limits.max_order_value,
            max_positions=self.kernel.limits.max_positions,
            valid_from=self.clock(),
            valid_until=min(
                self.clock() + timedelta(minutes=5), self.kernel.exit_context.valid_until
            )
            if self.kernel.exit_context
            else self.clock() + timedelta(minutes=5),
            confirmed_by="offline-test-operator",
            recovery_report_hash=report.payload_hash,
        )
        arm = self.request("ARM", grant=grant)
        self.approvals.approve(arm, authenticated_actor="offline-test-operator")
        if self.kernel.control(arm).status != "PENDING":
            raise RuntimeError("offline arm rejected")
        wait_for(self.kernel, lambda s: s.state == "ARMED")
        return grant

    def buy(self) -> Submission:
        return Submission(
            client_order_id="ord_" + uuid4().hex,
            account_id=self.kernel.account_id,
            trade_date=self.plan.payload.trade_date,
            action_type="NEW",
            plan_hash=self.plan.envelope.payload_hash,
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            limit_price="10",
            maximum_amount="1005",
            remark_token=uuid4().hex[:20],
            engine_boot_id=self.kernel.boot_id,
            permission_epoch=self.kernel.snapshot().permission_epoch,
            target=ActionTarget(
                action_id="action-" + uuid4().hex, target_remaining=100, rule_id="simulation-v1"
            ),
        )


def wait_for(kernel: EngineKernel, predicate: Callable[[EngineSnapshot], bool]) -> EngineSnapshot:
    """Only the offline harness waits; the kernel's poll remains nonblocking."""
    deadline = monotonic() + 3
    while monotonic() < deadline:
        state = kernel.poll()
        if predicate(state):
            return state
        if state.state == "HALTED":
            raise RuntimeError("offline engine halted: " + kernel.permission.snapshot().reason)
        sleep(0.001)
    raise TimeoutError("offline acceptance state did not settle")


def _new_journal(path: Path, account_id: str) -> CriticalJournalOwner:
    """Return an unstarted owner so cleanup owns it even if startup times out."""
    return CriticalJournalOwner(lambda: APSWCriticalStore(path, account_id))


@contextmanager
def open_offline_engine(
    directory: Path,
    *,
    broker: MockBrokerGateway | None = None,
    clock: Callable[[], datetime] = lambda: DEMO_NOW,
    journal_factory: Callable[[Path, str], CriticalJournalOwner] = _new_journal,
    max_orders: int = 1,
    plan_payload: PlanPayload | None = None,
    opening_reference: OpeningReference | None = None,
    broker_opening_quantity: int = 0,
    exit_context: ExitSessionContext | None = None,
    exit_state_guard: Callable[[Submission], bool] | None = None,
    preload_plan: bool = True,
    signing_key: Ed25519PrivateKey | None = None,
    broker_opening_cash: str = "10000",
    entry_state_guard: Callable[[Submission], bool] | None = None,
    entry_target_reader: Callable[[], tuple[EntryTarget, ...]] | None = None,
    limits: EngineLimits | None = None,
) -> Iterator[OfflineEngine]:
    """Managed Mock lifecycle; injected factories must return unstarted owners."""
    if broker is not None and not isinstance(broker, MockBrokerGateway):
        raise ValueError("offline composition requires a Mock Broker")
    if type(max_orders) is not int or not 1 <= max_orders <= 10:
        raise ValueError("offline max_orders must be 1..10")
    if type(broker_opening_quantity) is not int or not 0 <= broker_opening_quantity <= 1000:
        raise ValueError("Mock opening quantity must be 0..1000")
    opening_cash = Money.parse(broker_opening_cash).amount
    gateway = broker or MockBrokerGateway(ScenarioClock(clock()))
    guard = AccountInstanceGuard("MOCKV31ENGINEACCOUNT", backend=WindowsNamedMutexBackend())
    guard.acquire()
    journal: CriticalJournalOwner | None = None
    dispatcher: SDKDispatcher | None = None
    permission: DispatchPermission | None = None
    safe_to_release = False
    try:
        payload = plan_payload or example_plan()
        if exit_context is not None and exit_context.account_id != payload.account_id:
            raise ValueError("Mock exit account differs from installed account")
        if max_orders != 1:
            allocation = payload.allocations[0].model_copy(
                update={"max_qty": 100 * max_orders, "max_value": str(1005 * max_orders)}
            )
            payload = payload.model_copy(
                update={
                    "allocations": (allocation,),
                    "risk_budget": payload.risk_budget.model_copy(
                        update={"max_buy_value": str(1005 * max_orders)}
                    ),
                }
            )
        boot = "engine-" + uuid4().hex
        journal = journal_factory(directory / "critical" / "journal-v2.db", payload.account_id)
        journal.start()
        permission = DispatchPermission()
        dispatcher = SDKDispatcher(gateway, permission)
        private = signing_key or Ed25519PrivateKey.generate()
        signed = sign_plan(payload, "offline-ephemeral-key", private)
        files = ArtifactFiles(directory / "plans")

        def pause_plan() -> None:
            assert permission is not None
            permission.halt("PLAN_CHANGED")

        plans = EnginePlanControl(
            files,
            {signed.envelope.key_id: private.public_key()},
            node_id="simulation-node",
            account_id=payload.account_id,
            node_generation=1,
            engine_boot_id=boot,
            policies=payload.policy_bundle,
            rulebook=payload.rulebook_version,
            clock=clock,
            pause_for_change=pause_plan,
        )
        approvals = OneShotApprovals(boot_id=boot, clock=clock)

        def account() -> AccountFact:
            # A deterministic broker-side account query for tests only. The QMT
            # adapter must supply an independently observed real account snapshot.
            trades = gateway.query_trades()
            quantity = broker_opening_quantity + sum(
                t.quantity.value if t.side.value == "BUY" else -t.quantity.value for t in trades
            )
            cash = opening_cash - sum(
                (
                    t.notional.amount if t.side.value == "BUY" else -t.notional.amount
                    for t in trades
                ),
                Decimal(0),
            )
            # Mock available cash, including broker-side freezes already reflected
            # by observed_order_ids. Formal fees remain a separate integration.
            frozen_cash = sum(
                (
                    o.limit_price.amount * o.remaining_quantity.value
                    for o in gateway.query_orders()
                    if not o.is_terminal and o.side.value == "BUY"
                ),
                Decimal(0),
            )
            cash -= frozen_cash
            positions = []
            symbols = {str(t.symbol) for t in trades} | {"000001.SZ"}
            for symbol in sorted(symbols):
                opening = broker_opening_quantity if symbol == "000001.SZ" else 0
                held = opening + sum(
                    t.quantity.value if t.side.value == "BUY" else -t.quantity.value
                    for t in trades
                    if str(t.symbol) == symbol
                )
                frozen = sum(
                    o.remaining_quantity.value
                    for o in gateway.query_orders()
                    if not o.is_terminal and o.side.value == "SELL" and str(o.symbol) == symbol
                )
                sellable = (
                    opening
                    - frozen
                    - sum(
                        t.quantity.value
                        for t in trades
                        if t.side.value == "SELL" and str(t.symbol) == symbol
                    )
                )
                if held:
                    positions.append(
                        PositionFact(
                            symbol=symbol,
                            quantity=held,
                            sellable=sellable,
                            frozen=frozen,
                            market_value=f"{held * 10:.2f}",
                            cost_amount=f"{held * 10:.2f}",
                        )
                    )
            return AccountFact(
                account_id=payload.account_id,
                snapshot_id="mock-account-" + uuid4().hex,
                observed_at=clock(),
                cash=f"{cash:.2f}",
                cash_frozen=f"{frozen_cash:.2f}",
                market_value=f"{quantity * 10:.2f}",
                total_equity=f"{cash + frozen_cash + quantity * 10:.2f}",
                positions=tuple(positions),
                observed_order_ids=tuple(o.client_order_id for o in gateway.query_orders()),
                coherent=True,
            )

        kernel = EngineKernel(
            account_id=payload.account_id,
            boot_id=boot,
            trade_date=exit_context.trade_date if exit_context else payload.trade_date,
            journal=journal,
            dispatcher=dispatcher,
            permission=permission,
            plans=plans,
            approvals=approvals,
            account_reader=account,
            clock=clock,
            limits=limits
            or EngineLimits(
                max_buy_value="0" if exit_context else str(1005 * max_orders),
                max_order_value="1005",
                max_positions=1,
                max_order_quantity=100,
            ),
            risk_guard=(lambda order: order.symbol in {a.symbol for a in payload.allocations})
            if entry_state_guard is not None
            else lambda order: order.symbol == "000001.SZ"
            and (order.quantity == 100 if order.side == "BUY" else 1 <= order.quantity <= 100)
            and Decimal(order.limit_price) == 10,
            calendar=ExchangeCalendar(
                version="offline-calendar-v1",
                sessions=tuple((DEMO_NOW + timedelta(days=n)).date() for n in range(3)),
            ),
            opening_reference=opening_reference,
            exit_context=exit_context,
            exit_state_guard=exit_state_guard,
            entry_state_guard=entry_state_guard,
            entry_target_reader=entry_target_reader,
        )
        # Only lifecycle startup waits, before the owner loop becomes available.
        dispatcher.observe(lambda broker: broker.start(kernel.broker_event)).result(timeout=2)
        body = canonical_bytes(signed.model_dump(mode="json"))
        if exit_context is None and preload_plan:
            kernel.apply(
                PlanLookup(
                    account_id=payload.account_id,
                    strategy_id=payload.strategy_id,
                    trade_date=payload.trade_date,
                    plan_id=payload.plan_id,
                    state="PUBLISHED",
                    state_version=1,
                    etag=digest(body),
                    plan=signed,
                ),
                files.put(body),
            )
        yield OfflineEngine(kernel, gateway, signed, approvals, journal, clock)
    finally:
        primary = sys.exc_info()[1]
        cleanup_failed = False
        if permission is not None:
            permission.halt("OFFLINE_SHUTDOWN")
        if dispatcher is not None:
            try:
                deadline = monotonic() + 2
                while not dispatcher.idle and monotonic() < deadline:
                    sleep(0.001)  # Lifecycle shutdown only; never the decision loop.
                if dispatcher.idle:
                    dispatcher.observe(lambda broker: broker.stop()).result(timeout=2)
                    safe_to_release = True
            except Exception:
                cleanup_failed = True
            finally:
                dispatcher.close()
        else:
            safe_to_release = True
        try:
            if journal is not None:
                safe_to_release = journal.close() and safe_to_release
        except Exception:
            cleanup_failed = True
            safe_to_release = False
        if safe_to_release:
            guard.release()
        else:
            # Never allow a second process while an old SDK/storage owner may
            # still be alive. The OS releases this mutex on process termination.
            cleanup_failed = True
        if cleanup_failed:
            if primary is not None:
                primary.add_note("offline cleanup incomplete; account mutex retained")
            else:
                raise RuntimeError("offline worker still active; account mutex retained")


def run_engine_simulation(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root.resolve() / ("engine-simulation-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    broker = MockBrokerGateway(ScenarioClock(DEMO_NOW))
    with open_offline_engine(directory, broker=broker) as runtime:
        engine = runtime.kernel
        assert engine.snapshot().halted
        engine.start_recovery()
        wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        grant = runtime.confirmed_arm()
        broker.queue_submit(
            SubmitScenario(
                outcome=SubmitOutcome.PARTIAL_FILL,
                partial_fill_quantity=Quantity(50),
                fill_price=Price.parse("10"),
                fill_notional=Money.parse("500"),
            )
        )
        order = runtime.buy()
        engine.submit(order, valid_until=monotonic() + 2)
        wait_for(
            engine, lambda s: bool(s.submissions) and s.submissions[0].status == "SDK_ACCEPTED"
        )
        # Pump the bounded callback evidence until it is durable before cancel J1.
        wait_for(engine, lambda _: engine.critical_idle)
        original = broker.query_orders()[0]
        cancel = order.model_copy(
            update={
                "client_order_id": "ord_" + uuid4().hex,
                "action_type": "CANCEL",
                "original_order_id": original.broker_order_id,
                "original_client_order_id": order.client_order_id,
            }
        )
        engine.submit(cancel, valid_until=monotonic() + 2)
        wait_for(
            engine, lambda s: len(s.submissions) == 2 and s.submissions[1].status == "SDK_ACCEPTED"
        )
        wait_for(engine, lambda _: engine.critical_idle)
        first_boot = engine.boot_id
    with open_offline_engine(directory, broker=broker) as restarted:
        engine = restarted.kernel
        assert engine.boot_id != first_boot and engine.snapshot().halted
        engine.start_recovery()
        state = wait_for(engine, lambda s: s.state in ("REVIEW_REQUIRED", "RECOVERY_BLOCKED"))
        assert state.recovery and state.recovery.status == "CLEAR"
        assert state.recovery.submission_count == 2 and state.grant_id is None
        old_grant = restarted.request(
            "ARM", grant=grant.model_copy(update={"engine_boot_id": engine.boot_id})
        )
        assert engine.control(old_grant).status == "REJECTED"
        assert len(broker.query_orders()) == 1
        result: dict[str, JsonValue] = {
            "mode": "OFFLINE_ENGINE",
            "real_trading_enabled": False,
            "durable_grant_applied": True,
            "order_count": 1,
            "filled_quantity": 50,
            "cancelled_remaining_quantity": 50,
            "submission_records": state.recovery.submission_count,
            "restart_paused": state.halted,
            "old_grant_not_restored": state.grant_id is None,
            "recovery_status": state.recovery.status,
            "recovery_report_hash": state.recovery.payload_hash,
            "output_directory": str(directory),
        }
    (directory / "result.json").write_bytes(canonical_bytes(result))
    return result
