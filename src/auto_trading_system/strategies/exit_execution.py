"""D1 reducer-to-intent adapter; engine admission and J1 remain mandatory.

Restarts recover durable targets/rules, not an unverified price path or a prior
process's monotonic clock. Scheduling, exchange-rule rounding and authoritative
quote/context adapters must be supplied by the production strategy owner.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from auto_trading_system.domain import Price
from auto_trading_system.execution.sell_policy import EXIT_RULE, exit_action_id
from auto_trading_system.journal.critical_models import (
    ActionTarget,
    SellAllocation,
    SellBinding,
    Submission,
)
from auto_trading_system.reconcile.position_lots import PositionLot
from auto_trading_system.strategies.exit_v3 import ExitDecision, ExitState


def restore_exit_state(
    lot: PositionLot,
    submissions: tuple[Submission, ...],
    *,
    account_id: str,
    rule_id: str = EXIT_RULE,
) -> ExitState:
    target = lot.bought_quantity
    fired: set[str] = set()
    action = exit_action_id(account_id, lot.strategy_id, lot.lot_id, rule_id)
    for item in submissions:
        if item.action_type != "NEW" or item.target.action_id != action:
            continue
        binding = item.sell_binding
        if (
            item.account_id != account_id
            or item.side != "SELL"
            or binding is None
            or binding.strategy_id != lot.strategy_id
            or len(binding.allocations) != 1
            or binding.allocations[0].lot_id != lot.lot_id
            or item.target.rule_id != rule_id
            or item.target.target_remaining > target
        ):
            raise ValueError("inconsistent durable exit history")
        target = item.target.target_remaining
        fired.update(binding.fired_rules)
    return ExitState(target_remaining=target, fired=frozenset(fired), path_complete=False)


def exit_child(
    *,
    lot: PositionLot,
    decision: ExitDecision,
    account_id: str,
    trade_date: date,
    plan_hash: str,
    opening_hash: str,
    boot_id: str,
    permission_epoch: int,
    limit_price: Price,
    quantity_limit: int,
    broker_sellable: int,
    rule_id: str = EXIT_RULE,
) -> Submission | None:
    """Use original lot target, never re-apply a fraction to residual holdings."""
    if quantity_limit < 1 or broker_sellable < 0:
        raise ValueError("exit quantity limits must be valid")
    if not 0 <= decision.state.target_remaining <= lot.bought_quantity:
        raise ValueError("exit target is outside original lot quantity")
    if not decision.can_start_child_order or trade_date < lot.eligible_sell_date:
        return None
    quantity = min(
        lot.remaining - lot.reserved_quantity - decision.state.target_remaining,
        quantity_limit,
        broker_sellable,
    )
    if quantity <= 0:
        return None
    remark_token = uuid4().hex[:20]
    return Submission(
        # MiniQMT only persists the short order_remark. Deriving the local ID
        # from that token makes broker orders recoverable after a process restart.
        client_order_id="ord_" + remark_token,
        account_id=account_id,
        trade_date=trade_date,
        action_type="NEW",
        plan_hash=plan_hash,
        symbol=lot.symbol,
        side="SELL",
        quantity=quantity,
        limit_price=str(limit_price),
        maximum_amount="0",
        remark_token=remark_token,
        engine_boot_id=boot_id,
        permission_epoch=permission_epoch,
        target=ActionTarget(
            action_id=exit_action_id(account_id, lot.strategy_id, lot.lot_id, rule_id),
            target_remaining=decision.state.target_remaining,
            rule_id=rule_id,
        ),
        sell_binding=SellBinding(
            strategy_id=lot.strategy_id,
            opening_hash=opening_hash,
            allocations=(SellAllocation(lot_id=lot.lot_id, quantity=quantity),),
            fired_rules=tuple(sorted(decision.state.fired)),
        ),
    )
