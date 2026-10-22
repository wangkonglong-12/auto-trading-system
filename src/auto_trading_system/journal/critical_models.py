"""Immutable J1 commands and recovery evidence shared with the engine.

Only critical facts cross this port. Quotes, ordinary logs, plans and upload
queues have different owners and are intentionally absent from this contract.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from auto_trading_system.domain import Money, Price, SecurityCode

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")]
Hash = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class CriticalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ActionTarget(CriticalModel):
    action_id: Identifier = Field(description="一次策略动作的稳定幂等键")
    target_remaining: int = Field(ge=0, le=2**63 - 1, description="目标剩余持仓，整数股")
    rule_id: Identifier = Field(description="触发该目标的规则及版本标识")


class SellAllocation(CriticalModel):
    lot_id: Identifier = Field(description="报单前冻结的持仓来源批次编号")
    quantity: int = Field(gt=0, le=2**31 - 1, description="本子单绑定该批次的整数股数")


class SellBinding(CriticalModel):
    """Recovery ownership, saved atomically with the SELL intent in J1."""

    schema_version: Literal[1] = Field(default=1, description="卖出归属证据版本")
    strategy_id: Identifier = Field(description="持仓及退出动作的责任策略")
    opening_hash: Hash = Field(description="本交易日已验证跨日持仓检查点的正文摘要")
    allocations: tuple[SellAllocation, ...] = Field(
        min_length=1, max_length=64, description="冻结的有序批次分配，覆盖全部委托股数"
    )
    fired_rules: tuple[Identifier, ...] = Field(
        default=(), max_length=32, description="本次退出决策已触发规则，随子单原子保存"
    )
    state_hash: Hash | None = Field(default=None, description="独立规则状态已成功保存的正文摘要")

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        result: dict[str, object] = handler(self)
        if self.state_hash is None:
            result.pop("state_hash", None)
        return result

    @model_validator(mode="after")
    def unique(self) -> SellBinding:
        if len({a.lot_id for a in self.allocations}) != len(self.allocations):
            raise ValueError("duplicate sell allocation")
        if tuple(sorted(set(self.fired_rules))) != self.fired_rules:
            raise ValueError("fired rules must be unique and ordered")
        return self


class EntryBinding(CriticalModel):
    """Frozen entry target; a plan quantity ceiling is not an execution target."""

    plan_id: Identifier = Field(description="本次买入目标所属不可变计划编号")
    allocation_id: Identifier = Field(description="本次买入目标对应计划分配编号")
    target_quantity: int = Field(gt=0, le=2**31 - 1, description="执行时冻结的目标买入股数")
    state_hash: Hash | None = Field(default=None, description="独立一次性买入目标记录的正文摘要")

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        result: dict[str, object] = handler(self)
        if self.state_hash is None:
            result.pop("state_hash", None)
        return result


class Submission(CriticalModel):
    client_order_id: str = Field(
        pattern=r"^ord_[A-Za-z0-9_-]{8,64}$", description="一次实际报单或撤单的唯一请求编号"
    )
    account_id: Identifier = Field(description="绑定账户标识，使用内部别名")
    trade_date: date = Field(description="所属交易日，Asia/Shanghai")
    action_type: Literal["NEW", "CANCEL"] = Field(description="新委托或撤单")
    original_order_id: Identifier | None = Field(default=None, description="撤单对应券商委托号")
    original_client_order_id: Identifier | None = Field(
        default=None, description="撤单对应本地原始子单编号"
    )
    plan_hash: Hash = Field(description="不可变执行上下文正文摘要，具体类型由context_type指定")
    context_type: Literal["ENTRY_PLAN", "EXIT_SESSION"] = Field(
        default="ENTRY_PLAN",
        description="plan_hash引用的上下文类型，买入计划或独立退出会话",
    )
    symbol: str = Field(description="带交易所后缀的证券代码")
    side: Literal["BUY", "SELL"] = Field(description="原委托方向")
    quantity: int = Field(gt=0, le=2**63 - 1, description="委托数量，整数股")
    limit_price: str = Field(description="限价，元，十进制字符串")
    maximum_amount: str = Field(description="最高资金预占，元，包含预计费用")
    remark_token: str = Field(pattern=r"^[A-Za-z0-9_-]{1,24}$", description="券商回报关联短令牌")
    engine_boot_id: Identifier = Field(description="本次引擎运行代际，重启时更新")
    permission_epoch: int = Field(ge=1, description="提交依据的本地许可版本")
    target: ActionTarget = Field(description="与提交在同一个J1中保存的一次性动作目标")
    entry_binding: EntryBinding | None = Field(
        default=None, description="买入分配及实际目标股数证据；缺失时日终标记目标未取得"
    )
    sell_binding: SellBinding | None = Field(
        default=None, description="卖出批次证据；历史无绑定记录仅可读取，执行层拒绝派发"
    )

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        # Preserve canonical hashes of both pre-dev42 and dev42 J1 records.
        # Omit new default fields only; older explicit null fields are retained.
        result: dict[str, object] = handler(self)
        if self.sell_binding is None:
            result.pop("sell_binding", None)
        if self.entry_binding is None:
            result.pop("entry_binding", None)
        if self.context_type == "ENTRY_PLAN":
            result.pop("context_type", None)
        return result

    @model_validator(mode="after")
    def validate_order(self) -> Submission:
        if self.entry_binding is not None and (
            self.side != "BUY"
            or self.context_type != "ENTRY_PLAN"
            or self.quantity > self.entry_binding.target_quantity
        ):
            raise ValueError("entry binding requires a BUY within its frozen target")
        SecurityCode.parse(self.symbol)
        price = Price.parse(self.limit_price)
        amount = Money.parse(self.maximum_amount)
        if self.side == "BUY" and amount.amount < price.amount * self.quantity:
            raise ValueError("buy reservation must cover limit price times quantity")
        is_cancel = self.action_type == "CANCEL"
        if is_cancel != (self.original_order_id is not None):
            raise ValueError("CANCEL requires original_order_id; NEW must omit it")
        if is_cancel != (self.original_client_order_id is not None):
            raise ValueError("CANCEL requires original_client_order_id; NEW must omit it")
        if self.sell_binding is not None and (
            self.side != "SELL"
            or sum(a.quantity for a in self.sell_binding.allocations) != self.quantity
        ):
            raise ValueError("sell binding must cover every SELL share")
        if self.context_type == "EXIT_SESSION" and (
            self.side != "SELL" or self.sell_binding is None or self.sell_binding.state_hash is None
        ):
            raise ValueError("exit-session SELL requires durable strategy-state binding")
        return self


class CommitEvidence(CriticalModel):
    client_order_id: Identifier
    payload_hash: Hash
    record_seq: int = Field(ge=1)
    fresh: bool = Field(description="仅本次新建且提交成功为真；重复请求不能据此派发")


class RecoveryRecord(CriticalModel):
    record_seq: int = Field(ge=1)
    payload_hash: Hash
    submission: Submission


class CriticalStoreError(RuntimeError):
    """Stable fail-closed failure; avoid including SDK or credential content."""
