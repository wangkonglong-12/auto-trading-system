"""Account aliases and immutable business facts; never order instructions."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")]
Hash = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
Shares = Annotated[int, Field(ge=0, le=2**31 - 1)]
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,11})\.[0-9]{2}$")]
Price = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,9})(\.[0-9]{1,8})?$")]
Symbol = Annotated[str, Field(pattern=r"^[0-9]{6}\.(SH|SZ)$")]
Side = Literal["BUY", "SELL"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def body_hash(value: BaseModel) -> str:
    body = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


class Identity(Model):
    account_id: Id
    strategy_id: Id
    selection_date: date
    symbol: Symbol
    environment: Literal["REAL", "TEST"]

    @property
    def execution_id(self) -> str:
        return "exec-" + body_hash(self)[7:]


class Candidate(Model):
    kind: Literal["CANDIDATE"]
    phase: Literal["1440", "1445", "1450"]
    source_run_id: Id
    name: str | None = Field(min_length=1, max_length=64)
    target_amount: Money


class Progress(Model):
    kind: Literal["PROGRESS"]
    plan_hash: Hash
    status: Id
    target_quantity: Shares | None


class SubmissionFact(Model):
    kind: Literal["SUBMISSION"]
    client_order_id: Id
    side: Side
    action: Literal["NEW", "CANCEL"]
    original_client_order_id: Id | None
    quantity: int = Field(ge=1, le=2**31 - 1)
    price: Price
    status: Literal[
        "PERSISTING",
        "DISPATCHING",
        "SDK_ACCEPTED",
        "NOT_DISPATCHED",
        "UNCERTAIN",
        "PERSISTENCE_FAILED",
        "PERSISTENCE_UNCERTAIN",
        "EVIDENCE_MISMATCH",
        "RECOVERY_REQUIRED",
        "BEGIN_FAILED",
        "RECOVERED",
    ]
    lot_id: Id | None

    @model_validator(mode="after")
    def action_binding(self) -> Self:
        if (self.action == "CANCEL") != (self.original_client_order_id is not None):
            raise ValueError("cancel must reference the original local order")
        if Decimal(self.price) <= 0:
            raise ValueError("submission price must be positive")
        return self


class OrderFact(Model):
    kind: Literal["ORDER"]
    client_order_id: Id
    broker_order_id: Id | None
    side: Side
    quantity: int = Field(ge=1, le=2**31 - 1)
    filled_quantity: Shares
    price: Price
    status: Literal["ACTIVE", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"]
    reason_code: Id

    @model_validator(mode="after")
    def quantities(self) -> Self:
        if self.filled_quantity > self.quantity or Decimal(self.price) <= 0:
            raise ValueError("invalid order quantities or price")
        if self.status == "FILLED" and self.filled_quantity != self.quantity:
            raise ValueError("filled order must be completely filled")
        return self


class TradeFact(Model):
    kind: Literal["TRADE"]
    client_order_id: Id
    broker_order_id: Id
    broker_trade_id: Id
    side: Side
    quantity: int = Field(ge=1, le=2**31 - 1)
    price: Price
    amount: Money

    @model_validator(mode="after")
    def amount_matches(self) -> Self:
        expected = (Decimal(self.price) * self.quantity).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if Decimal(self.price) <= 0 or Decimal(self.amount) != expected:
            raise ValueError("trade amount differs from price times shares")
        return self


class CancelFailure(Model):
    kind: Literal["CANCEL_ERROR"]
    client_order_id: Id
    broker_order_id: Id
    side: Side
    reason_code: Id


class RequestFailure(Model):
    kind: Literal["REQUEST_ERROR"]
    client_order_id: Id
    action: Literal["NEW", "CANCEL"]
    side: Side
    reason_code: Id


Fact = Annotated[
    Candidate | Progress | SubmissionFact | OrderFact | TradeFact | CancelFailure | RequestFailure,
    Field(discriminator="kind"),
]


class Draft(Model):
    identity: Identity
    source_key: Hash
    occurred_at: AwareDatetime
    fact: Fact

    @model_validator(mode="after")
    def timing(self) -> Self:
        if (
            self.occurred_at.utcoffset() != timedelta(hours=8)
            or self.occurred_at.date() < self.identity.selection_date
        ):
            raise ValueError("event must retain its actual CN business date")
        if (
            isinstance(self.fact, Candidate)
            and self.occurred_at.date() != self.identity.selection_date
        ):
            raise ValueError("candidate must be received on its selection day")
        return self


class Event(Draft):
    seq: int = Field(ge=1, le=2**63 - 1)

    @property
    def event_id(self) -> str:
        return self.identity.execution_id + ":" + str(self.seq)


class Batch(Model):
    schema_version: int = Field(ge=1, le=1)
    node_id: Id
    account_id: Id
    strategy_id: Id
    environment: Literal["REAL", "TEST"]
    events: tuple[Event, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def scope(self) -> Self:
        if len({e.event_id for e in self.events}) != len(self.events):
            raise ValueError("duplicate event within upload")
        if any(
            (e.identity.account_id, e.identity.strategy_id, e.identity.environment)
            != (self.account_id, self.strategy_id, self.environment)
            for e in self.events
        ):
            raise ValueError("event belongs to another upload scope")
        return self


class Acknowledgment(Model):
    event_id: Id
    event_hash: Hash


class Receipt(Model):
    schema_version: int = Field(ge=1, le=1)
    acknowledgments: tuple[Acknowledgment, ...] = Field(min_length=1, max_length=20)
