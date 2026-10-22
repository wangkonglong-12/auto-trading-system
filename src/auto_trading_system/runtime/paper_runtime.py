"""Normal-process composition of existing strategy owners with fictional inputs.

No automatic authorization: existing operator recovery/ARM commands are required
on every boot. Prices and clock are visibly synthetic, supplied by PaperSession.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from time import monotonic, monotonic_ns
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact
from auto_trading_system.strategies.entry_gate import EntryQuote, EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.entry_state import EntryTarget
from auto_trading_system.strategies.exit_context import verify_exit_context
from auto_trading_system.strategies.exit_owner import D1StrategyOwner, ExitQuote, ExitStateGate


class PaperRuntime:
    def __init__(self, config: NodeProcessConfig, clock: Callable[[], datetime]) -> None:
        assert config.paper_session is not None
        self.config, self.session, self.clock = config, config.paper_session, clock
        self.entry: EntryStrategyOwner | None = None
        self.exit: D1StrategyOwner | None = None
        self.entry_gate: EntryStateGate | None = None
        self.exit_gate: ExitStateGate | None = None
        self.context = None
        self.failed = False
        self.next_quote, self.sequence = 0.0, 0
        signed = self.session.exit_context
        if signed:
            # Signature remains checkable after the trading window for exports.
            # All live-in-session permissions still enforce current validity.
            self.context = verify_exit_context(
                canonical_bytes(signed.model_dump(mode="json")),
                config.public_keys(),
                now=signed.payload.issued_at,
            )
            if clock() < self.context.issued_at or self.context.account_id != config.account_id:
                raise ValueError("paper exit context scope/time differs")
            if set(self.session.prices) != {lot.symbol for lot in self.context.lots}:
                raise ValueError("paper exit price coverage differs")
            self.exit_gate = ExitStateGate(self.context, clock=clock)

    def allows_entry(self, item: Submission) -> bool:
        return self.entry_gate is not None and self.entry_gate.allows(item)

    def allows_exit(self, item: Submission) -> bool:
        return self.exit_gate is not None and self.exit_gate.allows(item)

    def targets(self) -> tuple[EntryTarget, ...]:
        return self.entry_gate.export_targets() if self.entry_gate else ()

    def poll(self, engine: EngineKernel) -> None:
        if self.failed:
            engine.poll()
            return
        try:
            plan = engine.plans.plans.loaded()
            if self.session.phase == "ENTRY" and plan is not None and self.entry is None:
                assert self.session.entry_policy
                gate = EntryStateGate(plan.payload, self.session.entry_policy, clock=self.clock)
                self.entry_gate = gate
                self.entry = EntryStrategyOwner(engine, gate)
            if self.exit_gate is not None and self.exit is None:
                self.exit = D1StrategyOwner(engine, self.exit_gate)
            if monotonic() >= self.next_quote:
                self.sequence += 1
                for symbol, price in self.session.prices.items():
                    if self.entry:
                        rule = self.entry.gate.rules[symbol]
                        self.entry.quote(
                            EntryQuote(
                                symbol=symbol,
                                sequence=self.sequence,
                                observed_at=self.clock(),
                                received_ns=monotonic_ns(),
                                bid_price=price,
                                ask_price=price,
                                ask_quantity=100000,
                                recent_volume=1000000,
                                cage_upper=rule.upper_price,
                                path_complete=True,
                                tradable=True,
                            )
                        )
                    if self.exit:
                        self.exit.quote(
                            ExitQuote(
                                symbol=symbol,
                                sequence=self.sequence,
                                observed_at=self.clock(),
                                received_ns=monotonic_ns(),
                                last_price=price,
                                bid_price=price,
                                tradable=True,
                            )
                        )
                self.next_quote = monotonic() + 0.1
            if self.entry:
                self.entry.poll()
            elif self.exit:
                self.exit.poll()
            else:
                engine.poll()
        except (ValueError, RuntimeError):
            self.failed = True
            engine.emergency_halt("PAPER_DRIVER_FAILED")

    def status(self) -> dict[str, JsonValue]:
        rules = self.context.sell_rules if self.context is not None else None
        return {
            "mode": "FICTIONAL_ACCEPTANCE_ONLY",
            "phase": self.session.phase,
            "rule_id": self.context.rule_id if self.context is not None else None,
            "sell_rules_hash": digest(canonical_bytes(rules.model_dump(mode="json")))
            if rules is not None
            else None,
            "as_of": self.clock().isoformat(),
            "driver_state": "FAILED"
            if self.failed
            else (
                self.entry.snapshot().state
                if self.entry
                else self.exit.snapshot().state
                if self.exit
                else "WAITING_PLAN"
            ),
        }

    def account(self, gateway: MockBrokerGateway) -> AccountFact:
        """Independent simulated broker balance; never reconstructed from J1."""
        trades, orders = gateway.paper_history(), gateway.query_orders()
        today = self.clock().date()
        cash = Decimal(self.config.mock_cash) + sum(
            (t.notional.amount * (1 if t.side.value == "SELL" else -1) for t in trades), Decimal(0)
        )
        frozen_cash = sum(
            (
                o.limit_price.amount * o.remaining_quantity.value
                for o in orders
                if not o.is_terminal and o.side.value == "BUY"
            ),
            Decimal(0),
        )
        positions: list[PositionFact] = []
        for symbol in sorted({str(t.symbol) for t in trades}):
            related = [t for t in trades if str(t.symbol) == symbol]
            held = sum(t.quantity.value * (1 if t.side.value == "BUY" else -1) for t in related)
            bought_today = sum(
                t.quantity.value
                for t in related
                if t.side.value == "BUY" and t.traded_at.value.date() == today
            )
            frozen = sum(
                o.remaining_quantity.value
                for o in orders
                if not o.is_terminal and o.side.value == "SELL" and str(o.symbol) == symbol
            )
            if held < 0 or any(t.traded_at.value.date() > today for t in related):
                raise ValueError("paper balance negative or future facts")
            if held:
                if symbol not in self.session.prices:
                    raise ValueError("paper holding has no explicit mark price")
                value = Decimal(self.session.prices[symbol]) * held
                positions.append(
                    PositionFact(
                        symbol=symbol,
                        quantity=held,
                        sellable=max(0, held - bought_today - frozen),
                        frozen=frozen,
                        market_value=f"{value:.2f}",
                        cost_amount=None,
                    )
                )
        market = sum((Decimal(p.market_value or "0") for p in positions), Decimal(0))
        if cash < frozen_cash:
            raise ValueError("paper cash cannot cover freezes")
        return AccountFact(
            account_id=self.config.account_id,
            snapshot_id="paper-" + uuid4().hex,
            observed_at=self.clock(),
            cash=f"{cash - frozen_cash:.2f}",
            cash_frozen=f"{frozen_cash:.2f}",
            market_value=f"{market:.2f}",
            total_equity=f"{cash + market:.2f}",
            positions=tuple(positions),
            observed_order_ids=tuple(o.client_order_id for o in orders),
            coherent=True,
        )
