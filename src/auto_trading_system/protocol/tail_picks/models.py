"""Account-independent, immutable excellent-only tail selection feed V1."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

CN = timezone(timedelta(hours=8))
PHASES = ("1440", "1445", "1450")
Phase = Literal["1440", "1445", "1450"]
BatchError = Literal[
    "SOURCE_FAILED",
    "SOURCE_INCOMPLETE",
    "SOURCE_INVALID",
    "TOO_MANY_STOCKS",
    "SOURCE_NOT_READY_BY_CUTOFF",
]
RunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")]


class FeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def check_time_text(value: object) -> object:
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+08:00", value):
            raise ValueError("timestamps require whole seconds and explicit +08:00")
        return datetime.fromisoformat(value)
    return value


class TailStock(FeedModel):
    code: str = Field(pattern=r"^[0-9]{6}\.(SZ|SH)$")
    name: str | None = Field(min_length=1, max_length=64)
    rank: int = Field(ge=1, le=15)

    @model_validator(mode="after")
    def clean_name(self) -> Self:
        if self.name is not None and (
            self.name != self.name.strip() or "\n" in self.name or "\r" in self.name
        ):
            raise ValueError("stock name must be trimmed and single-line")
        return self


class TailBatch(FeedModel):
    phase: Phase
    status: Literal["PENDING", "READY", "FAILED"]
    source_run_id: RunId | None
    completed_at: AwareDatetime | None
    error_code: BatchError | None
    stocks: tuple[TailStock, ...] = Field(max_length=15)

    @field_validator("completed_at", mode="before")
    @classmethod
    def time_text(cls, value: object) -> object:
        return check_time_text(value)

    @model_validator(mode="after")
    def state_and_order(self) -> Self:
        if self.status == "READY":
            if self.source_run_id is None or self.completed_at is None or self.error_code:
                raise ValueError("ready batch requires source and completion, without error")
        elif self.stocks or (self.status == "FAILED") != (self.error_code is not None):
            raise ValueError("unfinished batch cannot contain stocks or inconsistent error")
        if self.status == "PENDING" and self.completed_at is not None:
            raise ValueError("pending batch cannot have a completion")
        if [s.rank for s in self.stocks] != list(range(1, len(self.stocks) + 1)):
            raise ValueError("stock ranks must be contiguous and ordered")
        if len({s.code for s in self.stocks}) != len(self.stocks):
            raise ValueError("batch contains duplicate securities")
        return self


class TailPicks(FeedModel):
    schema_version: int = Field(ge=1, le=1)
    trade_date: date
    server_time: AwareDatetime
    batches: tuple[TailBatch, ...] = Field(min_length=3, max_length=3)

    @field_validator("server_time", mode="before")
    @classmethod
    def time_text(cls, value: object) -> object:
        return check_time_text(value)

    @field_validator("trade_date", mode="before")
    @classmethod
    def day_text(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("date requires YYYY-MM-DD")
            return date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def dates_and_phases(self) -> Self:
        if tuple(b.phase for b in self.batches) != PHASES:
            raise ValueError("feed must contain exactly the three ordered phases")
        if (
            self.server_time.utcoffset() != timedelta(hours=8)
            or self.server_time.microsecond
            or self.server_time.date() != self.trade_date
        ):
            raise ValueError("feed time must be this trading day in whole CN seconds")
        for batch in self.batches:
            completed = batch.completed_at
            if completed is not None:
                scheduled = datetime.combine(self.trade_date, time(14, int(batch.phase[2:])), CN)
                if (
                    completed.utcoffset() != timedelta(hours=8)
                    or completed.microsecond
                    or not scheduled <= completed <= self.server_time
                ):
                    raise ValueError("batch completion is outside its source window")
        return self

    @property
    def all_ready(self) -> bool:
        return all(b.status == "READY" for b in self.batches)

    def check_current(self, now: datetime) -> None:
        if now.tzinfo is None or self.trade_date != now.astimezone(CN).date():
            raise ValueError("feed belongs to another trading day")
        if abs((self.server_time - now).total_seconds()) > 30:
            raise ValueError("feed server clock differs by more than 30 seconds")


def pending_batch(phase: Phase) -> TailBatch:
    return TailBatch(
        phase=phase,
        status="PENDING",
        source_run_id=None,
        completed_at=None,
        error_code=None,
        stocks=(),
    )


class TailPicksPreview(FeedModel):
    """Read-only diagnostics. Deliberately incompatible with an executable daily feed."""

    schema_version: int = Field(ge=1, le=1)
    mode: Literal["TEST"]
    status: Literal["FOUND", "NOT_FOUND"]
    requested_date: date
    trade_date: date | None
    server_time: AwareDatetime
    lookback_days: int = Field(ge=366, le=366)
    batches: tuple[TailBatch, ...] = Field(max_length=3)

    @field_validator("requested_date", "trade_date", mode="before")
    @classmethod
    def day_text(cls, value: object) -> object:
        return TailPicks.day_text(value)

    @field_validator("server_time", mode="before")
    @classmethod
    def time_text(cls, value: object) -> object:
        return check_time_text(value)

    @model_validator(mode="after")
    def preview_dates(self) -> Self:
        if (
            self.server_time.utcoffset() != timedelta(hours=8)
            or self.server_time.microsecond
            or self.server_time.date() != self.requested_date
        ):
            raise ValueError("preview server time must be the requested CN day")
        if self.status == "NOT_FOUND":
            if self.trade_date is not None or self.batches:
                raise ValueError("empty preview cannot contain historical data")
            return self
        if (
            self.trade_date is None
            or not 0 <= (self.requested_date - self.trade_date).days < self.lookback_days
            or tuple(b.phase for b in self.batches) != PHASES
            or not any(b.status == "READY" and b.stocks for b in self.batches)
        ):
            raise ValueError("preview requires an in-range day with real excellent picks")
        for batch in self.batches:
            completed = batch.completed_at
            if completed is not None:
                scheduled = datetime.combine(self.trade_date, time(14, int(batch.phase[2:])), CN)
                if (
                    completed.utcoffset() != timedelta(hours=8)
                    or completed.microsecond
                    or completed.date() != self.trade_date
                    or not scheduled <= completed <= self.server_time
                ):
                    raise ValueError("preview completion must retain the real historical date")
        return self

    def check_current(self, now: datetime) -> None:
        if now.tzinfo is None or self.requested_date != now.astimezone(CN).date():
            raise ValueError("preview request belongs to another day")
        if abs((self.server_time - now).total_seconds()) > 30:
            raise ValueError("preview server clock differs by more than 30 seconds")
