"""Signed, opening-time SELL context, independent of the afternoon entry plan.

Signing proves provenance, not local authority. The composition root supplies
pinned keys and a verified calendar/opening reference; every boot still needs
fresh broker reconciliation and a local SELL-only session grant.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.records import MoneyText, PriceText, Symbol
from auto_trading_system.strategies.sell_rules import SellRules

DOMAIN = b"AUTO-TRADING/EXIT-CONTEXT/V1\n"


class ExitLotContext(WireModel):
    lot_id: WireId = Field(description="开盘检查点中唯一持仓批次")
    symbol: Symbol = Field(description="批次证券及交易所")
    cost: PriceText = Field(description="经来源核对的单位持仓成本，元")
    previous_close: PriceText = Field(description="同复权口径前收盘价，元")
    open_price: PriceText = Field(description="当日正式开盘价，元")
    price_tick: PriceText = Field(description="已验证规则包规定的价格档位，元")
    lower_price: PriceText = Field(description="当日可申报价下界，元")
    upper_price: PriceText = Field(description="当日可申报价上界，元")
    sell_step: int = Field(ge=1, le=1000, description="正常卖出数量步长，整数股")
    allow_odd_liquidation: bool = Field(description="规则包是否允许一次清仓卖出零股")
    source_hash: Sha256 = Field(description="开盘价、成本及市场规则来源证据摘要")

    @model_validator(mode="after")
    def prices(self) -> ExitLotContext:
        if any(
            Decimal(getattr(self, key)) <= 0
            for key in (
                "cost",
                "previous_close",
                "open_price",
                "price_tick",
                "lower_price",
                "upper_price",
            )
        ) or Decimal(self.lower_price) > Decimal(self.upper_price):
            raise ValueError("exit context requires positive consistent prices")
        return self


class ExitSessionContext(WireModel):
    schema_version: Literal[1] = Field(default=1, description="开盘退出上下文版本")
    context_id: WireId = Field(description="不可变退出会话编号")
    account_id: WireId = Field(description="绑定账户内部别名")
    strategy_id: WireId = Field(description="责任策略编号")
    trade_date: date = Field(description="Asia/Shanghai交易日")
    opening_hash: Sha256 = Field(description="已验证前日封存持仓正文摘要")
    calendar_hash: Sha256 = Field(description="完整交易日日历规范正文摘要")
    rule_id: Literal["T1_EXIT_BASELINE_V3", "COST_EXIT_V1"] = Field(
        default="T1_EXIT_BASELINE_V3", description="退出规则版本"
    )
    sell_rules: SellRules | None = Field(
        default=None, description="按本批买入成本执行的三项卖出规则"
    )
    issued_at: AwareDatetime = Field(description="来源证据完成并签发的时间")
    valid_from: AwareDatetime = Field(description="最早允许退出的时间")
    stop_new_at: AwareDatetime = Field(description="停止新增SELL子单的时间")
    cancel_by: AwareDatetime = Field(description="允许提交撤单的截止时间")
    valid_until: AwareDatetime = Field(description="本上下文最终失效时间")
    max_order_value: MoneyText = Field(description="上下文单笔卖出名义金额上限，元")
    max_order_quantity: int = Field(ge=1, le=2**31 - 1, description="上下文单笔数量上限，股")
    cancel_after_seconds: int = Field(
        default=30, ge=1, le=300, description="在途订单连续未更新多少秒后请求撤单"
    )
    lots: tuple[ExitLotContext, ...] = Field(
        min_length=1, max_length=15, description="本会话允许退出的批次及价格规则"
    )

    @model_validator(mode="after")
    def scope(self) -> ExitSessionContext:
        times = (
            self.issued_at,
            self.valid_from,
            self.stop_new_at,
            self.cancel_by,
            self.valid_until,
        )
        if any(t.utcoffset() != timedelta(hours=8) or t.date() != self.trade_date for t in times):
            raise ValueError("exit timestamps require same trading date and UTC+08:00")
        if (
            not self.issued_at
            <= self.valid_from
            < self.stop_new_at
            <= self.cancel_by
            <= self.valid_until
        ):
            raise ValueError("exit window is not ordered")
        if self.valid_from.time() < time(9, 30) or self.stop_new_at.time() > time(14, 55, 30):
            raise ValueError("exit context exceeds installed session window")
        if len({lot.lot_id for lot in self.lots}) != len(self.lots):
            raise ValueError("duplicate exit lot")
        if Decimal(self.max_order_value) <= 0:
            raise ValueError("exit amount ceiling must be positive")
        if (self.rule_id == "COST_EXIT_V1") != (self.sell_rules is not None):
            raise ValueError("cost exit rule requires exactly one frozen sell policy")
        if self.sell_rules is not None and self.sell_rules.sell_by >= self.stop_new_at.time():
            raise ValueError("sell-by must precede stop-new-order cutoff")
        return self

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json", exclude_none=True)))


class SignedExitContext(WireModel):
    payload: ExitSessionContext = Field(description="冻结的开盘退出上下文")
    key_id: WireId = Field(description="本地已信任的签名公钥标识")
    signature: str = Field(min_length=88, max_length=88, description="域分离Ed25519签名，Base64")


def sign_exit_context(
    context: ExitSessionContext, key_id: str, key: Ed25519PrivateKey
) -> SignedExitContext:
    return SignedExitContext(
        payload=context,
        key_id=key_id,
        signature=base64.b64encode(
            key.sign(DOMAIN + canonical_bytes(context.model_dump(mode="json", exclude_none=True)))
        ).decode("ascii"),
    )


def verify_exit_context(
    data: bytes, keys: Mapping[str, Ed25519PublicKey], *, now: datetime
) -> ExitSessionContext:
    signed = SignedExitContext.model_validate_json(
        canonical_bytes(parse_bytes(data, max_bytes=64 * 1024))
    )
    key = keys.get(signed.key_id)
    if (
        key is None
        or now.utcoffset() is None
        or not signed.payload.issued_at <= now < signed.payload.valid_until
    ):
        raise ValueError("exit context key or validity is not trusted")
    key.verify(
        base64.b64decode(signed.signature, validate=True),
        DOMAIN + canonical_bytes(signed.payload.model_dump(mode="json", exclude_none=True)),
    )
    return signed.payload
