"""Independent board decision engines using the existing durable SELL driver.

Rule plugins return an absolute remaining target. They cannot call the broker.
Each owner has its own gate, state chain, policy version and pause switch; the
account engine alone owns orders, reservations, permissions and reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from decimal import Decimal
from typing import Protocol

from pydantic import JsonValue

from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.reconcile.position_lots import PositionLot
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.strategies.board_sell_configuration import BoardSellDraft, BoardSellRule
from auto_trading_system.strategies.exit_context import ExitLotContext
from auto_trading_system.strategies.exit_owner import D1StrategyOwner, ExitQuote, ExitStateGate
from auto_trading_system.strategies.exit_v3 import ExitState


@dataclass(frozen=True)
class RuleInput:
    lot: PositionLot
    policy: ExitLotContext
    settings: BoardSellRule
    state: ExitState
    now: datetime
    quote: ExitQuote | None
    quote_fresh: bool


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    target_remaining: int | None
    state: dict[str, JsonValue] | None = None


class SellRule(Protocol):
    def evaluate(self, value: RuleInput) -> RuleResult | None: ...


class TimeExit:
    def evaluate(self, value: RuleInput) -> RuleResult | None:
        if value.now.time() >= time.fromisoformat(value.settings.sell_at):
            return RuleResult("SELL_BY_TIME", 0)
        return None


class TakeProfit:
    def evaluate(self, value: RuleInput) -> RuleResult | None:
        if value.settings.mode != "TAKE_PROFIT_OR_TIME":
            return None
        if value.settings.take_profit_pct is None:
            raise ValueError("take-profit parameter is missing")
        if not value.quote_fresh or value.quote is None:
            return None
        # Preserve the existing price-return meaning: actual batch cost and last trade.
        change = (Decimal(value.quote.last_price) / Decimal(value.policy.cost) - 1) * 100
        if change >= Decimal(value.settings.take_profit_pct):
            return RuleResult("TAKE_PROFIT", 0)
        return None


class BoardSellEngine(D1StrategyOwner):
    """One instance per board. Construction/restoration never enables execution."""

    configuration: BoardSellDraft | None = None
    configuration_ready = True
    included_lots: frozenset[str] | None = None

    def apply_configuration(self, draft: BoardSellDraft, sources: dict[str, str]) -> None:
        self.gate.owner_only()
        self.configuration = draft
        self.included_lots = frozenset(
            p.lot_id
            for p in self.context.lots
            if (sources.get(p.lot_id) == "AUTOMATIC" and draft.settings.include_automatic)
            or (sources.get(p.lot_id) == "SYSTEM_MANUAL" and draft.settings.include_system_manual)
        )
        self.configuration_ready = True
        self.waiting_reasons.clear()

    def includes(self, lot_id: str) -> bool:
        return self.included_lots is None or lot_id in self.included_lots

    def allows(self, item: Submission) -> bool:
        if not self.enabled or not self.configuration_ready or not self.gate.allows(item):
            return False
        if item.action_type != "NEW" or self.configuration is None:
            return True
        assert item.sell_binding is not None
        lot_id = item.sell_binding.allocations[0].lot_id
        record = self.gate.latest.get(lot_id)
        marker = (record.state.rule_states or {}).get("BOARD_CONFIGURATION") if record else None
        return bool(
            self.includes(lot_id) and marker and marker.get("version") == self.configuration.version
        )

    @property
    def cancel_after_seconds(self) -> int:
        return (
            self.configuration.settings.cancel_after_seconds
            if self.configuration
            else super().cancel_after_seconds
        )

    def rule_for(self, policy: ExitLotContext) -> BoardSellRule:
        if self.configuration:
            return (
                self.configuration.settings.main
                if self.context.board == "MAIN"
                else self.configuration.settings.chinext
            )
        settings = policy.board_rule or self.context.board_rule
        assert settings is not None
        return settings

    def __init__(
        self,
        engine: EngineKernel,
        gate: ExitStateGate,
        *,
        rules: tuple[SellRule, ...] | None = None,
    ) -> None:
        if gate.context.board is None or gate.context.board_rule is None:
            raise ValueError("board engine requires a versioned board context")
        self.enabled = False
        self.waiting_reasons: dict[str, str] = {}
        self.rules: tuple[SellRule, ...] = (
            rules if rules is not None else (TimeExit(), TakeProfit())
        )
        super().__init__(engine, gate)

    def set_enabled(self, enabled: bool) -> None:
        self.gate.owner_only()
        if enabled and (self._failure or not self.gate.ready or self._pending):
            raise ValueError("BOARD_SELL_NOT_READY")
        self.enabled = enabled

    def _fail(self, reason: str) -> None:
        # The account engine independently halts on Journal/account failures.
        self.enabled = False
        self._failure = reason
        self.gate.blocked = True

    def _evaluate(self) -> None:
        if self.enabled and self.configuration_ready:
            super()._evaluate()

    def can_start_child(self, lot: PositionLot, known: tuple[Submission, ...]) -> bool:
        if not self.includes(lot.lot_id):
            return False
        count = sum(
            1
            for item in known
            if item.action_type == "NEW"
            and item.plan_hash == self.context.payload_hash
            and item.sell_binding is not None
            and any(a.lot_id == lot.lot_id for a in item.sell_binding.allocations)
        )
        maximum = (
            self.configuration.settings.max_reposts + 1
            if self.configuration
            else self.context.max_children
        )
        if maximum is not None and count >= maximum:
            self.waiting_reasons[lot.lot_id] = "REPOST_LIMIT_REACHED"
            return False
        return True

    def evaluate_lot(
        self, lot: PositionLot, policy: ExitLotContext, state: ExitState, quote: ExitQuote | None
    ) -> ExitState:
        now = self.gate.clock()
        if (
            not self.includes(lot.lot_id)
            or now.date() < lot.eligible_sell_date
            or now.date() != self.context.trade_date
        ):
            return state
        settings = self.rule_for(policy)
        if policy.prior_target_remaining is not None:
            state = replace(
                state,
                target_remaining=min(state.target_remaining, policy.prior_target_remaining),
                fired=state.fired | frozenset(policy.prior_fired_rules or ()),
                rule_states={**(policy.prior_rule_states or {}), **state.rule_states},
            )
        value = RuleInput(
            lot, policy, settings, state, now, quote, quote is not None and self.gate.fresh(quote)
        )
        results = tuple(
            result for rule in self.rules if (result := rule.evaluate(value)) is not None
        )
        if len({result.rule_id for result in results}) != len(results):
            raise ValueError("duplicate rule plugin identifiers")
        if any(
            r.target_remaining is not None and not 0 <= r.target_remaining <= lot.bought_quantity
            for r in results
        ):
            raise ValueError("rule returned an invalid batch target")
        return replace(
            state,
            target_remaining=min(
                (
                    state.target_remaining,
                    *(r.target_remaining for r in results if r.target_remaining is not None),
                )
            ),
            fired=state.fired
            | frozenset(r.rule_id for r in results if r.target_remaining is not None),
            rule_states={
                **state.rule_states,
                **{r.rule_id: r.state for r in results if r.state is not None},
                **(
                    {"BOARD_CONFIGURATION": self.configuration.model_dump(mode="json")}
                    if self.configuration
                    else {}
                ),
            },
        )
