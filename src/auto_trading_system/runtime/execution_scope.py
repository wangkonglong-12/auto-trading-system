"""Uniform read-only authorization scope for entry plans and SELL-only contexts."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.strategies.exit_context import ExitSessionContext


@dataclass(frozen=True)
class ExecutionScope:
    kind: Literal["ENTRY_PLAN", "EXIT_SESSION"]
    payload_hash: str
    strategy_id: str
    trade_date: date
    valid_until: datetime
    cancel_by: datetime
    directions: tuple[Literal["BUY", "SELL"], ...]
    max_buy_value: str


def execution_scope(
    plan: PlanEnvelope | None, exit_context: ExitSessionContext | None
) -> ExecutionScope | None:
    if exit_context:
        return ExecutionScope(
            "EXIT_SESSION",
            exit_context.payload_hash,
            exit_context.strategy_id,
            exit_context.trade_date,
            exit_context.valid_until,
            exit_context.cancel_by,
            ("SELL",),
            "0",
        )
    if plan:
        p = plan.payload
        return ExecutionScope(
            "ENTRY_PLAN",
            plan.envelope.payload_hash,
            p.strategy_id,
            p.trade_date,
            p.offline_authorization.valid_until,
            p.execution_window.cancel_by,
            p.offline_authorization.directions,
            p.risk_budget.max_buy_value,
        )
    return None


def execution_scopes(
    plan: PlanEnvelope | None, exit_context: ExitSessionContext | None
) -> tuple[ExecutionScope, ...]:
    """Return every independently verified scope available in this boot."""
    values: list[ExecutionScope] = []
    if plan is not None:
        p = plan.payload
        values.append(
            ExecutionScope(
                "ENTRY_PLAN",
                plan.envelope.payload_hash,
                p.strategy_id,
                p.trade_date,
                p.offline_authorization.valid_until,
                p.execution_window.cancel_by,
                p.offline_authorization.directions,
                p.risk_budget.max_buy_value,
            )
        )
    if exit_context is not None:
        values.append(
            ExecutionScope(
                "EXIT_SESSION",
                exit_context.payload_hash,
                exit_context.strategy_id,
                exit_context.trade_date,
                exit_context.valid_until,
                exit_context.cancel_by,
                ("SELL",),
                "0",
            )
        )
    return tuple(values)
