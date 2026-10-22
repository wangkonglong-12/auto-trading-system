"""Order gate adapters that re-evaluate Windows Hard Safety at submit time."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from auto_trading_system.journal import IntentSide, OrderIntentRecord
from auto_trading_system.risk import (
    HardSafetyDecision,
    HardSafetyDecisionOutcome,
    HardSafetyEvaluator,
    HardSafetyFacts,
    HardSafetyOrder,
    HardSafetySide,
)


class HardSafetyViolation(RuntimeError):
    def __init__(self, decision: HardSafetyDecision) -> None:
        if not isinstance(decision, HardSafetyDecision):
            raise ValueError("decision must be a HardSafetyDecision")
        super().__init__("WINDOWS_HARD_SAFETY_REJECTED: order exceeded a machine-local limit")
        self.code = "WINDOWS_HARD_SAFETY_REJECTED"
        self.decision = decision


class HardSafetyOrderExecutionGuard:
    """Load current local facts and reject any order outside Windows limits."""

    def __init__(
        self,
        evaluator: HardSafetyEvaluator,
        *,
        facts_supplier: Callable[[], HardSafetyFacts],
    ) -> None:
        if not isinstance(evaluator, HardSafetyEvaluator):
            raise ValueError("evaluator must be a HardSafetyEvaluator")
        if not callable(facts_supplier):
            raise ValueError("facts_supplier must be callable")
        self._evaluator = evaluator
        self._facts_supplier = facts_supplier

    def __call__(self, intent: OrderIntentRecord) -> None:
        if not isinstance(intent, OrderIntentRecord):
            raise ValueError("intent must be an OrderIntentRecord")
        facts = self._facts_supplier()
        if not isinstance(facts, HardSafetyFacts):
            raise ValueError("facts_supplier must return HardSafetyFacts")
        side = HardSafetySide.BUY if intent.side is IntentSide.BUY else HardSafetySide.SELL
        order = HardSafetyOrder(
            intent.client_order_id,
            intent.account_id,
            intent.symbol,
            side,
            intent.limit_price.amount * intent.quantity.value,
            intent.max_notional,
        )
        decision = self._evaluator.evaluate(
            order,
            facts,
            decision_id=_decision_id(intent),
        )
        if decision.decision is HardSafetyDecisionOutcome.REJECT:
            raise HardSafetyViolation(decision)


class CompositeOrderExecutionGuard:
    """Apply all local execution permissions in a fixed, immutable order."""

    def __init__(self, guards: tuple[Callable[[OrderIntentRecord], None], ...]) -> None:
        if (
            not isinstance(guards, tuple)
            or not guards
            or not all(callable(value) for value in guards)
        ):
            raise ValueError("guards must be a non-empty tuple of callables")
        self._guards = guards

    def __call__(self, intent: OrderIntentRecord) -> None:
        if not isinstance(intent, OrderIntentRecord):
            raise ValueError("intent must be an OrderIntentRecord")
        for guard in self._guards:
            guard(intent)


def _decision_id(intent: OrderIntentRecord) -> str:
    digest = hashlib.sha256(f"{intent.account_id}\0{intent.client_order_id}".encode()).hexdigest()[
        :32
    ]
    return f"risk_hard_{digest}"
