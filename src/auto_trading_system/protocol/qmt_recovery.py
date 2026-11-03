"""Explicit operator-confirmed contract identities; never trading permission."""

from datetime import date
from typing import Literal

from pydantic import Field

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.records import PriceText


class QmtOrderRecoveryBinding(WireModel):
    trade_date: date
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    order_sysid: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    client_order_id: str = Field(pattern=r"^ord_[A-Za-z0-9_-]{8,64}$")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, le=2**31 - 1)
    limit_price: PriceText
