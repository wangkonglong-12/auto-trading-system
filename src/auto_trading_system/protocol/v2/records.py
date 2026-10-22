"""Daily business records with explicit units and cross-field invariants.

The outer DailyRecord carries the stable identifier; its dataset selects exactly
one payload model here. Unknown fields and imprecise numeric money are rejected
before the receiver writes a part. Broker observations are not settlement claims.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes

MoneyText = Annotated[
    str,
    Field(pattern=r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$", description="人民币元，最多2位小数"),
]
SignedMoneyText = Annotated[
    str, Field(pattern=r"^-?(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$", description="有符号人民币元")
]
PriceText = Annotated[
    str,
    Field(pattern=r"^(0|[1-9][0-9]{0,9})(\.[0-9]{1,8})?$", description="每股人民币元，最多8位小数"),
]
Shares = Annotated[int, Field(ge=0, le=2**31 - 1, description="整数股")]
Symbol = Annotated[
    str, Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$", description="带市场后缀的证券代码")
]


class Observation(WireModel):
    account_id: WireId = Field(description="内部账户标识，必须与上传清单一致")
    trade_date: date = Field(description="所属交易日")
    observed_at: AwareDatetime = Field(description="该条事实的观测时点")

    @model_validator(mode="after")
    def observation_day(self) -> Observation:
        # Observations may be collected during next-day repair, but never before
        # the trading day to which they claim to belong.
        if self.observed_at.astimezone(timezone(timedelta(hours=8))).date() < self.trade_date:
            raise ValueError("observation predates the claimed trading day")
        return self


class PlanResultRecord(Observation):
    plan_id: WireId
    plan_hash: Sha256
    allocation_id: WireId
    symbol: Symbol
    target_quantity: Shares
    filled_quantity: Shares
    status: Literal["SKIPPED", "PARTIAL", "COMPLETED", "CANCELLED", "UNRESOLVED"]
    reason_code: WireId

    @model_validator(mode="after")
    def totals(self) -> PlanResultRecord:
        if self.filled_quantity > self.target_quantity:
            raise ValueError("plan fill exceeds target")
        if self.status == "COMPLETED" and self.filled_quantity != self.target_quantity:
            raise ValueError("completed plan must meet its target")
        return self


class SubmissionRecord(Observation):
    client_order_id: WireId
    action_type: Literal["NEW", "CANCEL"]
    action_id: WireId
    plan_hash: Sha256
    symbol: Symbol
    side: Literal["BUY", "SELL"]
    quantity: Shares
    limit_price: PriceText
    maximum_amount: MoneyText
    payload_hash: Sha256
    record_seq: int = Field(ge=1, description="关键Journal提交序号")
    engine_boot_id: WireId
    permission_epoch: int = Field(ge=1)
    target_order_id: WireId | None = None
    status: Literal[
        "SEND_REQUESTED", "NOT_DISPATCHED", "SDK_ACCEPTED", "REJECTED", "UNCERTAIN", "RESOLVED"
    ]

    @model_validator(mode="after")
    def cancellation_target(self) -> SubmissionRecord:
        if (self.action_type == "CANCEL") != (self.target_order_id is not None):
            raise ValueError("CANCEL requires the original broker order id")
        if self.quantity < 1 or Decimal(self.limit_price) <= 0:
            raise ValueError("submission quantity and limit price must be positive")
        return self


class BrokerOrderRecord(Observation):
    broker_order_id: WireId
    client_order_id: WireId | None = None
    market: Literal["SH", "SZ", "BJ"]
    symbol: Symbol
    side: Literal["BUY", "SELL"]
    quantity: Shares
    filled_quantity: Shares
    cancelled_quantity: Shares
    limit_price: PriceText
    status: Literal["ACTIVE", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"]
    raw_status: str = Field(min_length=1, max_length=96, description="券商原始状态值原文")
    remark_token: str = Field(default="", max_length=24)

    @model_validator(mode="after")
    def totals(self) -> BrokerOrderRecord:
        if self.symbol[-2:] != self.market:
            raise ValueError("symbol market mismatch")
        if self.quantity < 1 or self.filled_quantity + self.cancelled_quantity > self.quantity:
            raise ValueError("invalid order quantities")
        if self.status == "FILLED" and self.filled_quantity != self.quantity:
            raise ValueError("filled order quantity mismatch")
        if self.status == "CANCELLED" and (
            self.filled_quantity + self.cancelled_quantity != self.quantity
        ):
            raise ValueError("cancelled order has unexplained remaining shares")
        return self


class BrokerTradeRecord(Observation):
    broker_trade_id: WireId
    broker_order_id: WireId
    client_order_id: WireId | None = None
    market: Literal["SH", "SZ", "BJ"]
    symbol: Symbol
    side: Literal["BUY", "SELL"]
    quantity: Shares
    price: PriceText
    gross_amount: MoneyText = Field(description="券商原始成交金额，元；费用单独核对")
    traded_at: AwareDatetime
    origin: Literal["STRATEGY", "MANUAL", "UNKNOWN"]

    @model_validator(mode="after")
    def positive(self) -> BrokerTradeRecord:
        if self.quantity < 1 or Decimal(self.price) <= 0 or Decimal(self.gross_amount) <= 0:
            raise ValueError("trade quantity, price and amount must be positive")
        if self.symbol[-2:] != self.market or self.traded_at > self.observed_at:
            raise ValueError("trade market/time mismatch")
        if self.traded_at.astimezone(timezone(timedelta(hours=8))).date() != self.trade_date:
            raise ValueError("trade timestamp belongs to another trading day")
        return self


class PositionRecord(Observation):
    snapshot_id: WireId
    symbol: Symbol
    total_quantity: Shares
    available_quantity: Shares
    frozen_quantity: Shares
    strategy_quantity: Shares
    market_value: MoneyText
    cost_amount: MoneyText | None = Field(default=None, description="未知成本为空，不能填零代替")
    attribution_status: Literal["MATCHED", "UNEXPLAINED", "MANUAL"]

    @model_validator(mode="after")
    def totals(self) -> PositionRecord:
        if self.available_quantity + self.frozen_quantity > self.total_quantity:
            raise ValueError("available plus frozen exceeds total position")
        if self.strategy_quantity > self.total_quantity:
            raise ValueError("strategy position exceeds observed total")
        return self


class AssetRecord(Observation):
    snapshot_id: WireId
    currency: Literal["CNY"] = "CNY"
    cash_available: MoneyText
    cash_frozen: MoneyText
    market_value: MoneyText
    total_equity: MoneyText
    quality: Literal["CONFIRMED", "INDETERMINATE"]


class ReconciliationRecord(Observation):
    reconciliation_id: WireId
    scope: Literal["ORDERS", "TRADES", "POSITIONS", "CASH", "DAILY"]
    symbol: Symbol | None = None
    status: Literal["MATCHED", "DISCREPANCY", "INDETERMINATE"]
    expected_quantity: Shares | None = None
    observed_quantity: Shares | None = None
    amount_difference: SignedMoneyText | None = None
    reason_code: WireId
    related_ids: tuple[WireId, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def matched_evidence(self) -> ReconciliationRecord:
        if self.status == "MATCHED":
            if self.expected_quantity != self.observed_quantity:
                raise ValueError("matched reconciliation has unequal quantities")
            if self.amount_difference is not None and Decimal(self.amount_difference) != 0:
                raise ValueError("matched reconciliation has a nonzero amount difference")
        return self


class IncidentRecord(Observation):
    incident_id: WireId
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]
    code: WireId
    summary: str = Field(min_length=1, max_length=2048, description="脱敏说明，禁止凭据和完整账号")
    related_ids: tuple[WireId, ...] = Field(default=(), max_length=200)


RECORD_MODELS: dict[str, type[Observation]] = {
    "plan_results": PlanResultRecord,
    "submission_records": SubmissionRecord,
    "broker_orders": BrokerOrderRecord,
    "broker_trades": BrokerTradeRecord,
    "positions": PositionRecord,
    "assets": AssetRecord,
    "reconciliations": ReconciliationRecord,
    "incidents": IncidentRecord,
}


def validate_record(
    dataset: str, data: dict[str, JsonValue], *, account_id: str, trade_date: date
) -> Observation:
    """Reject a well-signed payload that carries facts for a different account/day."""
    model = RECORD_MODELS.get(dataset)
    if model is None:
        raise ValueError("unknown daily dataset")
    record = model.model_validate_json(canonical_bytes(data))
    if record.account_id != account_id or record.trade_date != trade_date:
        raise ValueError("daily record account/date differs from manifest")
    return record
