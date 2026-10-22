"""Pure single-lot SELL admission, separate from permission and J1 ownership.

One D1 action owns one acquisition lot. Child orders may reduce that lot only
towards a non-increasing target after prior child orders are terminal-confirmed.
The low-level allocation contract is extensible; multi-lot strategy actions are
not admitted until their per-lot target semantics are defined.
"""

from __future__ import annotations

from datetime import date

from auto_trading_system.journal.critical_models import ActionTarget, Submission
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.position_lots import PortfolioProjection

EXIT_RULE = "T1_EXIT_BASELINE_V3"


def exit_action_id(account_id: str, strategy_id: str, lot_id: str, rule_id: str = EXIT_RULE) -> str:
    return "exit-" + digest(canonical_bytes([account_id, strategy_id, lot_id, rule_id]))[7:]


def allows_sell(
    submission: Submission,
    *,
    portfolio: PortfolioProjection,
    strategy_id: str,
    opening_hash: str | None,
    as_of: date,
    broker_sellable: int,
    reflected_order_ids: frozenset[str],
    prior: tuple[Submission, ...],
    historical_targets: tuple[ActionTarget, ...] = (),
    rule_id: str = EXIT_RULE,
) -> bool:
    binding = submission.sell_binding
    if (
        submission.action_type != "NEW"
        or submission.side != "SELL"
        or binding is None
        or binding.opening_hash != opening_hash
        or binding.strategy_id != strategy_id
        or len(binding.allocations) != 1
    ):
        return False
    allocation = binding.allocations[0]
    lot = next((lot for lot in portfolio.lots if lot.lot_id == allocation.lot_id), None)
    target = submission.target
    if (
        lot is None
        or lot.strategy_id != strategy_id
        or lot.symbol != submission.symbol
        or as_of < lot.eligible_sell_date
        or target.rule_id != rule_id
        or target.action_id
        != exit_action_id(submission.account_id, strategy_id, lot.lot_id, rule_id)
        or allocation.quantity != submission.quantity
        or submission.quantity > lot.remaining - lot.reserved_quantity - target.target_remaining
    ):
        return False
    if any(
        s.action_type == "NEW"
        and s.client_order_id != submission.client_order_id
        and s.target.action_id == target.action_id
        and s.target.target_remaining < target.target_remaining
        for s in prior
    ):
        return False
    if any(
        t.action_id == target.action_id
        and (t.rule_id != target.rule_id or t.target_remaining < target.target_remaining)
        for t in historical_targets
    ):
        return False
    return submission.quantity <= portfolio.sellable(
        symbol=submission.symbol,
        strategy_id=strategy_id,
        as_of=as_of,
        broker_sellable=broker_sellable,
        reflected_order_ids=reflected_order_ids,
    )
