"""Long-lived engine composition and owner-safe named-pipe endpoints.

QMT_READ_ONLY owns a real SDK session but rejects orders and cancels.
QMT_TRADING composes account recovery, plan-driven market subscriptions, guarded
entry/exit owners and the query-backed order gateway. Every boot remains paused
until a local operator grants the bounded live session; network jobs cannot grant
trading authority or select a broker profile.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from threading import Event, Lock
from time import monotonic, sleep, time_ns
from typing import cast
from uuid import uuid4

from pydantic import JsonValue, SecretStr

from auto_trading_system.config.node_v2 import NodeProcessConfig, resolve_secret, role_keys
from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.control.ipc_protocol import IPCError, Request, Role, RPCService
from auto_trading_system.control.node_authorization import AUTH_ACTIONS, NodeAuthorization
from auto_trading_system.control.owner_mailbox import OwnerMailbox
from auto_trading_system.control.windows_pipe import PipeServer
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.domain.clock import SystemClock
from auto_trading_system.execution.dispatch_boundary import DispatchPermission, SDKDispatcher
from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.gateways.broker.cicc_qmt import discover_qmt_accounts
from auto_trading_system.gateways.broker.cicc_qmt_readonly_gateway import CiccQmtReadOnlyGateway
from auto_trading_system.gateways.broker.cicc_qmt_trading_gateway import CiccQmtTradingGateway
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.gateways.broker.paper_state import PaperStateFile
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.gateways.market import CiccQmtMarketGateway
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.checkpoint_blobs import read_checkpoint
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference, load_opening
from auto_trading_system.runtime.daily_risk_file import (
    DailyRiskBaseline,
    load_daily_risk_baseline,
    save_daily_risk_baseline,
)
from auto_trading_system.runtime.day_snapshot import DaySnapshot
from auto_trading_system.runtime.engine_v2 import CN, EngineKernel, EngineLimits
from auto_trading_system.runtime.node_layout import validate_layout
from auto_trading_system.runtime.node_plan_recovery import (
    PlanRecoveryReference,
    restore_reference,
    save_reference,
)
from auto_trading_system.runtime.opening_reference_file import (
    load_opening_reference,
    save_opening_reference,
)
from auto_trading_system.runtime.paper_runtime import PaperRuntime
from auto_trading_system.runtime.plan_market import PlanMarketRuntime
from auto_trading_system.runtime.qmt_trading_runtime import QmtTradingRuntime
from auto_trading_system.runtime.recovery_v2 import AccountFact, BrokerRecoverySnapshot
from auto_trading_system.runtime.single_instance import (
    AccountInstanceGuard,
    WindowsNamedMutexBackend,
    create_account_instance_guard,
)
from auto_trading_system.security.operator_password import validate_operator_password_verifier
from auto_trading_system.security.operator_session import LocalOperatorSessionManager
from auto_trading_system.strategies.exit_context import ExitLotContext, ExitSessionContext
from auto_trading_system.strategies.exit_owner import ExitStateGate
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.strategies.trading_settings_drafts import TradingSettingsDraftStore
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_catalog import DailyCatalog, closing_slot
from auto_trading_system.sync.engine_daily import freeze_engine_day


@dataclass
class _Clock:
    read: Callable[[], datetime]

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.read())


def _live_opening_context(
    config: NodeProcessConfig,
    journal: CriticalJournalOwner,
    clock: Callable[[], datetime],
    settings: TradingSettings | None,
) -> tuple[OpeningReference | None, ExitSessionContext | None]:
    """Load the sealed prior day and build today's local cost based exit scope."""
    if config.broker != "QMT_TRADING":
        return None, None
    reference = load_opening_reference(config.data_root / "critical/opening-reference.json")
    if reference is None:
        return None, None
    now = clock().astimezone(CN)
    body = journal.request(
        lambda store: read_checkpoint(store, reference.key), deadline=monotonic() + 2
    ).result(timeout=3)
    checkpoint = load_opening(
        body,
        reference,
        account_id=config.account_id,
        trade_date=now.date(),
        calendar=config.calendar,
    )
    if settings is None or now.time() >= time(14, 55):
        return reference, None
    managed = tuple(
        lot
        for lot in checkpoint.lots
        if lot.remaining and lot.cost_price is not None and lot.strategy_id == config.strategy_id
    )
    if not managed:
        return reference, None
    valid_from = max(now, datetime.combine(now.date(), time(9, 30), CN))
    stop_new = datetime.combine(now.date(), time(14, 55), CN)
    if valid_from >= stop_new:
        return reference, None
    lots = []
    for lot in managed:
        assert lot.cost_price is not None
        cost = Decimal(lot.cost_price)
        evidence: dict[str, JsonValue] = {
            "opening_hash": reference.payload_hash,
            "lot_id": lot.lot_id,
            "cost": lot.cost_price,
            "rulebook": config.rulebook,
        }
        lots.append(
            ExitLotContext(
                lot_id=lot.lot_id,
                symbol=lot.symbol,
                cost=lot.cost_price,
                previous_close=lot.cost_price,
                open_price=lot.cost_price,
                price_tick="0.01",
                lower_price="0.01",
                upper_price=format(cost * Decimal(10), "f"),
                sell_step=100,
                allow_odd_liquidation=True,
                source_hash=digest(canonical_bytes(evidence)),
            )
        )
    max_order = settings.buy.max_order_value if settings.buy.enabled else "1000000.00"
    context_seed: dict[str, JsonValue] = {
        "account_id": config.account_id,
        "trade_date": now.date().isoformat(),
        "opening_hash": reference.payload_hash,
        "settings": settings.sell.model_dump(mode="json"),
    }
    return reference, ExitSessionContext(
        context_id="exit-" + digest(canonical_bytes(context_seed))[7:],
        account_id=config.account_id,
        strategy_id=config.strategy_id,
        trade_date=now.date(),
        opening_hash=reference.payload_hash,
        calendar_hash=digest(canonical_bytes(config.calendar.model_dump(mode="json"))),
        rule_id="COST_EXIT_V1",
        sell_rules=settings.sell,
        issued_at=now,
        valid_from=valid_from,
        stop_new_at=stop_new,
        cancel_by=datetime.combine(now.date(), time(14, 55, 20), CN),
        valid_until=datetime.combine(now.date(), time(14, 55, 30), CN),
        max_order_value=max_order,
        max_order_quantity=settings.advanced.max_order_quantity,
        cancel_after_seconds=settings.buy.cancel_after_seconds,
        lots=tuple(lots),
    )


@contextmanager
def open_node_engine(
    config: NodeProcessConfig, clock: Callable[[], datetime], paper: PaperRuntime | None = None
) -> Iterator[EngineKernel]:
    validate_layout(config)
    # Account identity, not data directory or node id, prevents a second engine.
    guard = AccountInstanceGuard(
        hashlib.sha256(config.account_id.upper().encode()).hexdigest(),
        backend=WindowsNamedMutexBackend(),
    )
    guard.acquire()
    installation_guard: AccountInstanceGuard | None = None
    broker_account_guard: AccountInstanceGuard | None = None
    journal = CriticalJournalOwner(
        lambda: APSWCriticalStore(
            config.data_root / "critical" / "journal-v2.db", config.account_id
        )
    )
    permission = DispatchPermission()
    dispatcher: SDKDispatcher | None = None
    safe = False
    try:
        journal.start()
        live_settings: TradingSettings | None = None
        if config.broker == "QMT_TRADING":
            draft = TradingSettingsDraftStore(config.data_root).read()
            live_settings = draft.settings if draft is not None else None
        live_opening, live_exit = _live_opening_context(config, journal, clock, live_settings)
        risk_path = config.data_root / "critical/daily-risk-baseline.json"
        installed_risk = load_daily_risk_baseline(
            risk_path,
            account_id=config.account_id,
            trade_date=clock().astimezone(CN).date(),
        )
        live_exit_gate = ExitStateGate(live_exit, clock=clock) if live_exit else None
        if (config.paper_session is not None) != (paper is not None):
            raise ValueError("paper session must have its strategy composition")
        if config.broker in {"QMT_READ_ONLY", "QMT_TRADING"}:
            assert config.qmt_read_only is not None
            qmt = config.qmt_read_only
            installation_guard = create_account_instance_guard(
                "QmtReadonly"
                + hashlib.sha256(str(qmt.userdata_mini.resolve()).casefold().encode()).hexdigest()[
                    :16
                ]
            )
            installation_guard.acquire()
            # MiniQMT can retain closed client IDs briefly. Each engine boot
            # needs a new SDK session pair, including after a day rollover.
            session_base = (qmt.session_base + int(uuid4().hex[:8], 16)) % 2_147_483_645 + 1
            discovery = QmtProbeConfig(qmt.userdata_mini, session_base, "STOCK", 3)
            accounts = [
                item.account_id
                for item in discover_qmt_accounts(discovery)
                if item.account_type == 2
                and hashlib.sha256(item.account_id.encode()).hexdigest()[:16]
                == qmt.account_fingerprint
            ]
            if len(accounts) != 1:
                raise ValueError("QMT_ACCOUNT_BINDING_UNAVAILABLE")
            broker_account_guard = create_account_instance_guard(accounts[0])
            broker_account_guard.acquire()
            # CICC MiniQMT needs a short gap after the discovery connection.
            sleep(3.1)

            def halt_on_qmt_unavailable(reason: str) -> None:
                permission.halt(reason)

            probe = QmtProbeConfig(qmt.userdata_mini, session_base + 1, "STOCK", 3)
            if config.broker == "QMT_READ_ONLY":
                gateway: BrokerGateway = CiccQmtReadOnlyGateway(
                    probe,
                    accounts[0],
                    config.account_id,
                    clock,
                    unavailable_sink=halt_on_qmt_unavailable,
                )
            else:
                gateway = CiccQmtTradingGateway(probe, accounts[0], config.account_id, clock)
        else:
            gateway = MockBrokerGateway(
                _Clock(clock),
                paper_state=PaperStateFile(
                    config.data_root / "critical/paper-broker.json",
                    config.account_id,
                    config.mock_cash,
                )
                if paper
                else None,
            )
        dispatcher = SDKDispatcher(gateway, permission)
        boot = "engine-" + uuid4().hex

        def pause_plan() -> None:
            permission.halt("PLAN_CHANGED")

        plans = EnginePlanControl(
            ArtifactFiles(config.data_root / "artifacts"),
            config.public_keys(),
            node_id=config.node_id,
            account_id=config.account_id,
            node_generation=config.node_generation,
            engine_boot_id=boot,
            policies=config.policies,
            rulebook=config.rulebook,
            clock=clock,
            pause_for_change=pause_plan,
        )

        if not paper or paper.context is None:
            restore_reference(
                config.data_root / "critical/plan-recovery.json", plans, clock().date()
            )

        def account() -> AccountFact:
            if paper:
                assert isinstance(gateway, MockBrokerGateway)
                return paper.account(gateway)
            if isinstance(gateway, (CiccQmtReadOnlyGateway, CiccQmtTradingGateway)):
                return gateway.read_account()
            return AccountFact(
                account_id=config.account_id,
                snapshot_id="mock-" + uuid4().hex,
                observed_at=clock(),
                cash=config.mock_cash,
                cash_frozen="0.00",
                market_value="0.00",
                total_equity=config.mock_cash,
                positions=(),
                observed_order_ids=(),
                coherent=True,
            )

        engine = EngineKernel(
            account_id=config.account_id,
            boot_id=boot,
            trade_date=clock().astimezone(CN).date(),
            journal=journal,
            dispatcher=dispatcher,
            permission=permission,
            plans=plans,
            approvals=OneShotApprovals(boot_id=boot, clock=clock),
            account_reader=account,
            clock=clock,
            limits=(
                paper.session.limits
                if paper
                else EngineLimits(
                    mode="LIVE" if config.broker == "QMT_TRADING" else "PAPER",
                    max_buy_value=(live_settings.buy.max_daily_buy_value if live_settings else "0"),
                    max_order_value=(
                        live_settings.buy.max_order_value
                        if live_settings and live_settings.buy.enabled
                        else "1000000.00"
                        if live_settings
                        else "0"
                    ),
                    max_positions=(live_settings.safety.max_positions if live_settings else 0),
                    max_order_quantity=(
                        live_settings.advanced.max_order_quantity
                        if live_settings
                        else 100
                        if config.broker == "QMT_TRADING"
                        else 1
                    ),
                    max_daily_loss=(live_settings.safety.max_daily_loss if live_settings else "0"),
                )
            ),
            risk_guard=lambda item: (paper is not None and item.symbol in paper.session.prices)
            or (config.broker == "QMT_TRADING" and live_settings is not None),
            calendar=config.calendar,
            opening_reference=paper.session.opening if paper else live_opening,
            exit_context=paper.context if paper else live_exit,
            exit_state_guard=(
                paper.allows_exit
                if paper
                else live_exit_gate.allows
                if live_exit_gate is not None
                else None
            ),
            entry_state_guard=paper.allows_entry if paper else None,
            entry_target_reader=paper.targets if paper else None,
            risk_baseline_equity=(installed_risk.total_equity if installed_risk else None),
        )
        dispatcher.observe(lambda broker: broker.start(engine.broker_event)).result(timeout=10)
        engine.start_recovery()
        engine.live_exit_gate = live_exit_gate
        yield engine
    finally:
        permission.halt("PROCESS_SHUTDOWN")
        try:
            if dispatcher is None:
                safe = True
            else:
                until = monotonic() + 3
                while not dispatcher.idle and monotonic() < until:
                    sleep(0.005)
                if dispatcher.idle:
                    dispatcher.observe(lambda broker: broker.stop()).result(timeout=3)
                    safe = True
        finally:
            if dispatcher is not None:
                dispatcher.close()
            safe = journal.close() and safe
            if safe:
                if broker_account_guard is not None:
                    broker_account_guard.release()
                if installation_guard is not None:
                    installation_guard.release()
                guard.release()
            else:
                # Only process exit may release ownership if a worker is alive.
                raise RuntimeError("NODE_WORKER_SHUTDOWN_INCOMPLETE")


class EngineEndpoint:
    def __init__(
        self,
        config: NodeProcessConfig,
        engine: EngineKernel,
        stop: Event,
        clock: Callable[[], datetime],
        paper: PaperRuntime | None = None,
        market: PlanMarketRuntime | QmtTradingRuntime | None = None,
    ) -> None:
        self.config, self.engine, self.stop, self.clock = config, engine, stop, clock
        self.paper = paper
        self.market = market
        self.mailbox: OwnerMailbox[object] = OwnerMailbox()
        self.authorization: NodeAuthorization | None = None
        if config.operator_access:
            access = config.operator_access
            verifier = validate_operator_password_verifier(
                resolve_secret(access.password_verifier).decode("utf-8")
            )
            sessions = LocalOperatorSessionManager(
                SecretStr(verifier),
                actor_id=access.actor_id,
                clock=_Clock(clock),
                monotonic_clock=SystemClock(),
            )
            self.authorization = NodeAuthorization(engine, self.mailbox, sessions, clock=clock)
        self._status_lock = Lock()
        self._status: dict[str, JsonValue] = {}
        self._owner_sequence = 0
        self._supervision_id = os.environ.get("ATS_NODE_SUPERVISION_ID")
        self.publish_status()
        self.files = ArtifactFiles(config.data_root / "artifacts")
        self.catalog = DailyCatalog(
            config.data_root / "daily-catalog",
            self.files,
            node_id=config.node_id,
            account_id=config.account_id,
            generation=config.node_generation,
        )

    def status(self) -> dict[str, JsonValue]:
        snapshot = self.engine.snapshot()
        loaded = self.engine.plans.plans.loaded()
        return {
            "component": "engine",
            "node_id": self.config.node_id,
            "account_id": self.config.account_id,
            "node_generation": self.config.node_generation,
            "process_id": os.getpid(),
            "supervision_id": self._supervision_id,
            "boot_id": self.engine.boot_id,
            "broker": self.config.broker,
            "real_trading_enabled": self.config.broker == "QMT_TRADING",
            "halted": snapshot.halted,
            "state": snapshot.state,
            "state_version": snapshot.state_version,
            "trade_date": self.engine.trade_date.isoformat(),
            "plan_loaded": self.engine.plans.plans.loaded() is not None,
            "exit_context_loaded": self.engine.exit_context is not None,
            "recovery": snapshot.recovery.status if snapshot.recovery else None,
            "pause_reason": self.engine.permission.snapshot().reason,
            "journal_healthy": self.engine.journal.healthy,
            "operator_authorization_enabled": self.authorization is not None,
            "installed_limits": self.engine.limits.model_dump(mode="json"),
            "paper_session": self.paper.status() if self.paper else None,
            "market_session": self.market.status() if self.market else None,
            "broker_account": self.engine.broker_account_summary(),
            "order_count": len(snapshot.orders),
            "positions": [
                {"lot_id": lot.lot_id, "symbol": lot.symbol, "remaining": lot.remaining}
                for lot in snapshot.lots
            ],
            "plan": {
                "plan_id": loaded.payload.plan_id,
                "strategy_id": loaded.payload.strategy_id,
                "trade_date": loaded.payload.trade_date.isoformat(),
                "symbols": [item.symbol for item in loaded.payload.allocations],
            }
            if loaded
            else None,
        }

    def publish_status(self) -> None:
        """Only owner publishes; pipe readers never wait on the trading owner."""
        current = self.status()
        self._owner_sequence += 1
        current["owner_sequence"] = self._owner_sequence
        current["published_at"] = self.clock().isoformat()
        with self._status_lock:
            self._status = current

    def handle(self, request: Request) -> dict[str, JsonValue]:
        def call(operation: Callable[[], object]) -> object:
            remaining = min(4.0, (request.deadline_ms - time_ns() // 1_000_000) / 1000)
            if remaining <= 0:
                raise IPCError("REQUEST_EXPIRED")
            return self.mailbox.call(operation, timeout=remaining)

        if request.action in (
            "OPERATOR_HALT",
            "OPERATOR_RECOVER",
            "OPERATOR_DETAILS",
            "OPERATOR_CANCEL",
            "OPERATOR_LIQUIDATION_PREVIEW",
            "OPERATOR_LIQUIDATE",
        ):
            authorization = self.authorization
            if authorization is None:
                raise IPCError("OPERATOR_ACCESS_NOT_CONFIGURED")
            auth_request = (
                request.model_copy(
                    update={
                        "payload": {
                            key: request.payload[key]
                            for key in ("session_token", "csrf_token")
                            if key in request.payload
                        }
                    }
                )
                if request.action
                in {
                    "OPERATOR_CANCEL",
                    "OPERATOR_LIQUIDATION_PREVIEW",
                    "OPERATOR_LIQUIDATE",
                }
                else request
            )
            actor = authorization.authorize_control_channel(auth_request)
            if request.action == "OPERATOR_DETAILS":

                def request_snapshot() -> Future[BrokerRecoverySnapshot]:
                    authorization.authorize_control_channel(request)
                    try:
                        return self.engine.request_operator_snapshot()
                    except ValueError:
                        raise IPCError("ACCOUNT_QUERY_BUSY") from None

                page_query = cast(Future[BrokerRecoverySnapshot], call(request_snapshot))
                try:
                    page_snapshot = page_query.result(timeout=2)
                except Exception:
                    raise IPCError("ACCOUNT_QUERY_FAILED") from None

                def operator_details() -> dict[str, JsonValue]:
                    authorization.authorize_control_channel(request)
                    try:
                        return self.engine.operator_trade_details(page_snapshot)
                    except ValueError:
                        raise IPCError("ACCOUNT_QUERY_FAILED") from None

                return cast(dict[str, JsonValue], call(operator_details))
            if request.action == "OPERATOR_CANCEL":
                client_order_id = request.payload.get("client_order_id")
                if not isinstance(client_order_id, str):
                    raise IPCError("INVALID_ARGUMENTS")

                def operator_cancel() -> dict[str, JsonValue]:
                    authorization.authorize_control_channel(auth_request)
                    try:
                        return self.engine.operator_cancel_order(client_order_id)
                    except ValueError as exc:
                        raise IPCError(str(exc)) from None

                return cast(dict[str, JsonValue], call(operator_cancel))
            if request.action in {
                "OPERATOR_LIQUIDATION_PREVIEW",
                "OPERATOR_LIQUIDATE",
            }:
                if not isinstance(self.market, QmtTradingRuntime):
                    raise IPCError("LIQUIDATION_UNAVAILABLE")
                runtime = self.market
                draft = TradingSettingsDraftStore(self.config.data_root).read()
                if draft is None or not draft.settings.safety.emergency_liquidation_enabled:
                    raise IPCError("LIQUIDATION_DISABLED")

                def liquidation() -> dict[str, JsonValue]:
                    authorization.authorize_control_channel(auth_request)
                    lots = (
                        runtime.request_liquidation()
                        if request.action == "OPERATOR_LIQUIDATE"
                        else runtime.liquidation_preview()
                    )
                    return {
                        "status": (
                            "REQUESTED" if request.action == "OPERATOR_LIQUIDATE" else "PREVIEW"
                        ),
                        "confirmation_phrase": "确认清仓策略持仓",
                        "positions": list(lots),
                    }

                return cast(dict[str, JsonValue], call(liquidation))
            if request.action == "OPERATOR_HALT":
                self.engine.emergency_halt("AUTHENTICATED_OPERATOR_HALT")
                return {
                    "status": "APPLIED",
                    "action": "HALT",
                    "actor_id": actor,
                    "halt_latched": True,
                    "real_trading_enabled": False,
                }

            def operator_recover() -> dict[str, JsonValue]:
                # Revalidate after queueing; logout/expiry fences delayed work.
                authorization.authorize_control_channel(request)
                self.engine.start_recovery()
                return {
                    "status": "REQUESTED",
                    "action": "RECOVER",
                    "actor_id": actor,
                    "state_version": self.engine.snapshot().state_version,
                    "real_trading_enabled": False,
                }

            return cast(dict[str, JsonValue], call(operator_recover))
        if request.action == "LOAD_PLAN":
            if set(request.payload) != {"lookup_hash", "artifact_hash"}:
                raise IPCError("INVALID_ARGUMENTS")
            lookup_hash, plan_hash = (
                request.payload["lookup_hash"],
                request.payload["artifact_hash"],
            )
            if not isinstance(lookup_hash, str) or not isinstance(plan_hash, str):
                raise IPCError("INVALID_ARGUMENTS")
            lookup = PlanLookup.model_validate_json(self.files.read(lookup_hash))
            if (lookup.account_id, lookup.strategy_id, lookup.trade_date) != (
                self.config.account_id,
                self.config.strategy_id,
                self.engine.trade_date,
            ):
                raise IPCError("PLAN_SCOPE_MISMATCH")
            receipt = cast(PlanReceipt, call(lambda: self.engine.apply(lookup, plan_hash)))
            if receipt.status in ("LOADED", "WITHDRAWN"):
                try:
                    save_reference(
                        self.config.data_root / "critical/plan-recovery.json",
                        PlanRecoveryReference(
                            lookup_hash=lookup_hash,
                            artifact_hash=plan_hash if receipt.status == "LOADED" else None,
                            trade_date=lookup.trade_date,
                        ),
                    )
                except (OSError, ValueError):
                    self.engine.emergency_halt("PLAN_REFERENCE_PERSISTENCE_FAILED")
                    raise
            return receipt.model_dump(mode="json")
        if request.action == "SUPERVISOR_STOP":
            if not self._supervision_id or request.payload != {
                "supervision_id": self._supervision_id
            }:
                raise IPCError("SUPERVISION_SCOPE_MISMATCH")
            self.engine.emergency_halt("SUPERVISOR_STOP")
            self.stop.set()
            return {"halt_latched": True}
        if request.payload:
            raise IPCError("INVALID_ARGUMENTS")
        if request.action == "SEAL_DAY":
            pending = cast(Future[OpeningReference], call(self.engine.seal_portfolio))
            reference = pending.result(timeout=3)
            save_opening_reference(
                self.config.data_root / "critical/opening-reference.json", reference
            )
            return reference.model_dump(mode="json")
        if request.action == "STATUS":
            with self._status_lock:
                return dict(self._status)
        if request.action in ("HALT", "STOP"):
            self.engine.emergency_halt("AUTHENTICATED_LOCAL_" + request.action)
            if request.action == "STOP":
                self.stop.set()
            return {"halt_latched": True}
        if request.action in ("RECOVER_DAILY", "REQUEST_RECOVERY"):
            if request.action == "RECOVER_DAILY" and (
                self.clock().astimezone(CN).time() < time(15)
                or self.clock().astimezone(CN).date() != self.engine.trade_date
            ):
                raise IPCError("OUTSIDE_CLOSING_WINDOW")

            def recover() -> dict[str, JsonValue]:
                self.engine.start_recovery()
                return {
                    "state": "RECOVERING",
                    "state_version": self.engine.snapshot().state_version,
                }

            return cast(dict[str, JsonValue], call(recover))
        if request.action in ("EXPORT_DAILY", "REFRESH_DAILY"):
            # This pipe worker does file I/O; only capture runs on the owner.
            if self.clock().astimezone(CN).time() < time(15):
                raise IPCError("OUTSIDE_CLOSING_WINDOW")
            batch = self.catalog.get(self.engine.trade_date)
            refresh = False
            if request.action == "REFRESH_DAILY":
                now = self.clock().astimezone(CN)
                if now.date() != self.engine.trade_date:
                    raise IPCError("OUTSIDE_CLOSING_WINDOW")
                try:
                    slot = closing_slot(now)
                except ValueError as exc:
                    raise IPCError("OUTSIDE_CLOSING_WINDOW") from exc
                refresh = batch is not None and batch.manifest.data_as_of < slot
            if batch is None or refresh:
                snapshot = cast(DaySnapshot, call(self.engine.capture_day))
                if request.action == "REFRESH_DAILY" and snapshot.report.observed_at < slot:
                    raise IPCError("CLOSING_OBSERVATION_TOO_OLD")
                prior = batch
                batch = freeze_engine_day(
                    snapshot,
                    self.files,
                    node_id=self.config.node_id,
                    node_generation=self.config.node_generation,
                    journal_generation=self.config.journal_generation,
                    generated_at=self.clock(),
                    revision=prior.manifest.revision + 1 if prior else 1,
                    supersedes_batch_id=prior.manifest.batch_id if prior else None,
                )
                self.catalog.publish(batch)
            return {"artifact_hash": batch.artifact_hash, "batch_id": batch.manifest.batch_id}
        raise IPCError("ACTION_DENIED")


def run_engine(
    config: NodeProcessConfig,
    *,
    stop: Event | None = None,
    clock: Callable[[], datetime] | None = None,
    ready: Callable[[dict[str, JsonValue]], None] = lambda _: None,
) -> None:
    stopped = stop or Event()
    if clock is None:
        session = config.paper_session
        clock = (lambda: session.as_of) if session else lambda: datetime.now(CN)
    paper = PaperRuntime(config, clock) if config.paper_session else None
    keys = role_keys(config, ("agent", "operator"))
    servers: list[PipeServer] = []
    with open_node_engine(config, clock, paper) as engine:
        market: PlanMarketRuntime | QmtTradingRuntime | None = None
        if config.broker == "QMT_TRADING":
            qmt_market = CiccQmtMarketGateway()
            plan_market = PlanMarketRuntime(
                qmt_market,
                fault_reader=lambda: qmt_market.fault_code,
            )
            draft = TradingSettingsDraftStore(config.data_root).read()
            entry_install = next(item for item in config.policies if item.component == "entry")
            live_exit_gate = cast(ExitStateGate | None, engine.live_exit_gate)
            market = QmtTradingRuntime(
                plan_market,
                draft.settings if draft is not None else None,
                entry_install=entry_install,
                rulebook=config.rulebook,
                clock=clock,
                exit_gate=live_exit_gate,
            )
        endpoint = EngineEndpoint(config, engine, stopped, clock, paper, market)
        try:
            channels: tuple[tuple[str, dict[Role, frozenset[str]]], ...] = (
                (
                    "engine",
                    {
                        "agent": frozenset(
                            {
                                "HELLO",
                                "STATUS",
                                "LOAD_PLAN",
                                "RECOVER_DAILY",
                                "EXPORT_DAILY",
                                "REFRESH_DAILY",
                                "SEAL_DAY",
                            }
                        )
                    },
                ),
                (
                    "control",
                    {
                        "operator": frozenset(
                            {
                                "HELLO",
                                "STATUS",
                                "HALT",
                                "STOP",
                                "REQUEST_RECOVERY",
                                "SEAL_DAY",
                                "SUPERVISOR_STOP",
                                "OPERATOR_HALT",
                                "OPERATOR_RECOVER",
                                "OPERATOR_CANCEL",
                                "OPERATOR_LIQUIDATION_PREVIEW",
                                "OPERATOR_LIQUIDATE",
                            }
                        )
                    },
                ),
                (
                    "query",
                    {"operator": frozenset({"HELLO", "OPERATOR_DETAILS"})},
                ),
            )
            for channel, permissions in channels:
                service = RPCService(engine.boot_id, keys, permissions, endpoint.handle)
                servers.append(
                    PipeServer(config.pipe_name(channel), config.service_sid, service.handle)
                )
            if endpoint.authorization:
                # Login hashing/interactive proposals get a separate worker so
                # an expensive login cannot occupy the emergency HALT channel.
                service = RPCService(
                    engine.boot_id, keys, {"operator": AUTH_ACTIONS}, endpoint.authorization.handle
                )
                servers.append(
                    PipeServer(
                        config.pipe_name("authorization"), config.service_sid, service.handle
                    )
                )
            ready(endpoint.status())
            next_status = 0.0
            risk_baseline_saved = (
                config.broker != "QMT_TRADING"
                or load_daily_risk_baseline(
                    config.data_root / "critical/daily-risk-baseline.json",
                    account_id=config.account_id,
                    trade_date=engine.trade_date,
                )
                is not None
            )
            while not stopped.wait(0.005):
                if not all(server.healthy for server in servers):
                    raise IPCError("PIPE_WORKER_FAILED")
                if paper:
                    paper.poll(engine)
                elif market:
                    market.poll(engine)
                else:
                    engine.poll()
                endpoint.mailbox.poll()
                if not risk_baseline_saved:
                    baseline = engine.risk_baseline_equity()
                    if baseline is not None:
                        try:
                            save_daily_risk_baseline(
                                config.data_root / "critical/daily-risk-baseline.json",
                                DailyRiskBaseline(
                                    account_id=config.account_id,
                                    trade_date=engine.trade_date,
                                    total_equity=baseline,
                                ),
                            )
                            risk_baseline_saved = True
                        except Exception:
                            engine.emergency_halt("DAILY_RISK_BASELINE_PERSISTENCE_FAILED")
                if monotonic() >= next_status:
                    endpoint.publish_status()
                    next_status = monotonic() + 0.1
                if clock().astimezone(CN).date() != engine.trade_date:
                    engine.emergency_halt("TRADING_DATE_ROLLOVER")
                    stopped.set()  # Next scheduled startup gets a new date and fresh recovery.
        finally:
            engine.emergency_halt("PROCESS_EXIT")
            if market:
                try:
                    market.stop()
                except Exception:
                    engine.emergency_halt("MARKET_SHUTDOWN_FAILED")
            endpoint.mailbox.close()
            for server in reversed(servers):
                server.close()
