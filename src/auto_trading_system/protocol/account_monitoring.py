"""Whole-account observations, independent of strategy ownership and trading grants."""

from __future__ import annotations

import hashlib
from datetime import timedelta, timezone
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CN = timezone(timedelta(hours=8))
MAX_BODY = 8 * 1024 * 1024
Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")]
Symbol = Annotated[str, Field(pattern=r"^[0-9]{6}\.(SH|SZ)$")]
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,11})\.[0-9]{2}$")]
Price = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,8})?$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Asset(Model):
    total_asset: Money
    available_cash: Money
    frozen_cash: Money
    market_value: Money


class Position(Model):
    symbol: Symbol
    quantity: int = Field(ge=0, le=2**31 - 1)
    sellable_quantity: int = Field(ge=0, le=2**31 - 1)
    frozen_quantity: int = Field(ge=0, le=2**31 - 1)
    market_value: Money

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.sellable_quantity + self.frozen_quantity > self.quantity:
            raise ValueError("position quantities conflict")
        return self


class Trade(Model):
    trade_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    symbol: Symbol
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, le=2**31 - 1)
    price: Price
    amount: Money
    traded_at: AwareDatetime

    @property
    def key(self) -> str:
        # Broker identifiers are not assumed globally unique across days or markets.
        return fingerprint(
            [self.traded_at.astimezone(CN).date().isoformat(), self.symbol[-2:], self.trade_id]
        )

    @model_validator(mode="after")
    def positive(self) -> Self:
        from decimal import Decimal

        if Decimal(self.price) <= 0 or Decimal(self.amount) <= 0:
            raise ValueError("trade value must be positive")
        return self


class Observation(Model):
    asset_observed_at: AwareDatetime
    positions_observed_at: AwareDatetime
    captured_at: AwareDatetime
    asset: Asset
    positions: tuple[Position, ...] = Field(max_length=10000)
    trades: tuple[Trade, ...] = Field(max_length=10000)
    trade_history_scope: Literal["CURRENT_DAY_ONLY"] = "CURRENT_DAY_ONLY"

    @model_validator(mode="after")
    def complete(self) -> Self:
        if len({p.symbol for p in self.positions}) != len(self.positions):
            raise ValueError("duplicate position")
        if len({t.key for t in self.trades}) != len(self.trades):
            raise ValueError("duplicate trade")
        if not (
            0 <= (self.captured_at - self.asset_observed_at).total_seconds() <= 5
            and 0 <= (self.captured_at - self.positions_observed_at).total_seconds() <= 5
        ):
            raise ValueError("account query window too wide")
        day = self.captured_at.astimezone(CN).date()
        if any(
            t.traded_at.astimezone(CN).date() != day or t.traded_at > self.captured_at
            for t in self.trades
        ):
            raise ValueError("trade outside current query day")
        return self


class Snapshot(Model):
    sequence: int = Field(ge=1, le=2**63 - 1)
    observation: Observation

    @model_validator(mode="after")
    def separate_trades(self) -> Self:
        if self.observation.trades:
            raise ValueError("snapshot must not duplicate trade history")
        return self


class Batch(Model):
    schema_version: Literal[1] = 1
    node_id: Id
    account_id: Id
    snapshot: Snapshot
    trades: tuple[Trade, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def unique_trades(self) -> Self:
        if len({t.key for t in self.trades}) != len(self.trades):
            raise ValueError("duplicate trade")
        return self


class Receipt(Model):
    schema_version: Literal[1] = 1
    batch_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    received_at: AwareDatetime


def fingerprint(value: object) -> str:
    import json

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
