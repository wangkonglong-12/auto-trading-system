"""Bounded immutable rule-transition chain in the existing recovery table.

Operations run only inside the sole Journal owner. Quote ticks and evolving
peaks stay in memory; this chain stores discrete rule/target/quality changes.
Scanning every bounded slot detects holes instead of treating a gap as EOF.
"""

from __future__ import annotations

from datetime import date

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.strategies.exit_context import ExitSessionContext

CAPACITY = 256


class DurableExitState(WireModel):
    target_remaining: int = Field(ge=0, le=2**31 - 1, description="批次目标剩余股数")
    fired_rules: tuple[WireId, ...] = Field(
        max_length=32, description="已执行的一次性规则及检查标记"
    )
    profit_armed: bool = Field(description="是否已达到收益回撤跟踪激活条件")
    path_uncertain: bool = Field(description="历史行情路径缺口状态，仅可收紧")

    @model_validator(mode="after")
    def ordered(self) -> DurableExitState:
        if tuple(sorted(set(self.fired_rules))) != self.fired_rules:
            raise ValueError("exit rules must be unique and ordered")
        return self


class ExitTransition(WireModel):
    revision: int = Field(ge=1, le=CAPACITY, description="上下文内连续状态记录序号")
    previous_hash: Sha256 | None = Field(description="上一条状态正文摘要，首条为空")
    context_hash: Sha256 = Field(description="本次冻结退出上下文摘要")
    account_id: WireId = Field(description="唯一所属账户")
    trade_date: date = Field(description="策略状态所属交易日")
    lot_id: WireId = Field(description="此次状态改变的持仓批次")
    boot_id: WireId = Field(description="产生本次状态变化的进程代际")
    observed_at: AwareDatetime = Field(description="本地评估时间，含时区")
    state: DurableExitState = Field(description="已计算且待持久化的离散退出状态")

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))

    @property
    def key(self) -> str:
        return state_key(self.context_hash, self.revision)


def state_key(context_hash: str, revision: int) -> str:
    return f"exit-state:{context_hash}:{revision:04d}"


def load_exit_state(
    store: CriticalStorePort, context: ExitSessionContext
) -> tuple[ExitTransition, ...]:
    """Freeze one context per account/day; a new signature must not reset rule history.

    The Journal is already account-bound. A changed context on the same day is
    an explicit migration/reconciliation task, not an implicit new strategy run.
    """
    store.append_evidence(
        "recovery_checkpoints",
        f"exit-context:{context.trade_date}",
        canonical_bytes(context.model_dump(mode="json", exclude_none=True)),
    )
    return read_exit_history(store, context.payload_hash)


def read_exit_history(store: CriticalStorePort, context_hash: str) -> tuple[ExitTransition, ...]:
    history: list[ExitTransition] = []
    for revision in range(1, CAPACITY + 1):
        body = store.read_evidence("recovery_checkpoints", state_key(context_hash, revision))
        if body is None:
            continue
        record = ExitTransition.model_validate_json(body)
        if (
            revision != len(history) + 1
            or record.revision != revision
            or record.context_hash != context_hash
            or record.previous_hash != (history[-1].payload_hash if history else None)
        ):
            raise ValueError("exit state chain is incomplete or corrupt")
        history.append(record)
    return tuple(history)


def append_exit_transition(store: CriticalStorePort, record: ExitTransition) -> bool:
    """Single-owner serialized predecessor check + immutable append; no SDK I/O."""
    record = ExitTransition.model_validate_json(record.model_dump_json())
    if record.revision == 1:
        if record.previous_hash is not None:
            raise ValueError("first exit transition must have no predecessor")
    else:
        previous = store.read_evidence(
            "recovery_checkpoints", state_key(record.context_hash, record.revision - 1)
        )
        if previous is None or digest(previous) != record.previous_hash:
            raise ValueError("exit transition predecessor mismatch")
    return store.append_evidence(
        "recovery_checkpoints", record.key, canonical_bytes(record.model_dump(mode="json"))
    )
