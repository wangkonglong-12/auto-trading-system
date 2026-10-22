"""Local cross-day evidence; hashes bind history, never confer trading permission.

Only the engine's halted, terminal, reconciled closing path creates this object.
The next boot reads it from the same account-owned Journal, verifies its exact
submission prefix, then independently queries today's broker holdings. An
external JSON file or a hash by itself is not an accepted opening balance.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from auto_trading_system.journal.checkpoint_blobs import MAX_CHECKPOINT_BYTES
from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.position_lots import ExchangeCalendar, PositionLot


class OpeningReference(WireModel):
    trade_date: date = Field(description="已封存持仓的上一交易日")
    payload_hash: Sha256 = Field(description="同账户Journal内封存正文的完整摘要")

    @property
    def key(self) -> str:
        return closing_key(self.trade_date)


def closing_key(trade_date: date) -> str:
    return "portfolio-close:" + trade_date.isoformat()


def prefix_hash(records: tuple[RecoveryRecord, ...]) -> str:
    # Hash stored payload digests, not a reserialization under a newer model.
    return digest(canonical_bytes([[r.record_seq, r.payload_hash] for r in records]))


class PortfolioCheckpoint(WireModel):
    schema_version: Literal[1] = Field(default=1, description="跨日持仓检查点格式版本")
    account_id: str = Field(description="唯一所属账户，与Journal账户一致")
    trade_date: date = Field(description="已完成终态核对的封存交易日")
    calendar_version: str = Field(description="买入与T+1归属采用的交易日日历版本")
    recovery_report_hash: Sha256 = Field(description="当日已落盘且CLEAR的恢复报告摘要")
    submission_high_watermark: int = Field(ge=0, description="封存覆盖的J1最大记录序号")
    submission_prefix_hash: Sha256 = Field(description="全部已封存J1序号及原始正文摘要的摘要")
    lots: tuple[PositionLot, ...] = Field(max_length=2048, description="已核对归属及实成交的批次")

    @model_validator(mode="after")
    def settled(self) -> PortfolioCheckpoint:
        if len({lot.lot_id for lot in self.lots}) != len(self.lots) or any(
            lot.reserved_quantity
            or lot.trade_date_acquired > self.trade_date
            or lot.calendar_version != self.calendar_version
            for lot in self.lots
        ):
            raise ValueError("closing lots must be unique, settled and calendar-bound")
        return self

    def body(self) -> bytes:
        body = canonical_bytes(self.model_dump(mode="json"))
        if len(body) > MAX_CHECKPOINT_BYTES:
            raise ValueError("closing checkpoint exceeds evidence capacity")
        return body

    def reference(self) -> OpeningReference:
        return OpeningReference(trade_date=self.trade_date, payload_hash=digest(self.body()))


def verify_opening(
    body: bytes | None,
    reference: OpeningReference,
    records: tuple[RecoveryRecord, ...],
    *,
    account_id: str,
    trade_date: date,
    calendar: ExchangeCalendar,
) -> tuple[PortfolioCheckpoint, tuple[RecoveryRecord, ...]]:
    """Reject missing/altered prefixes and unsealed prior-day tails, never skip them."""
    checkpoint = load_opening(
        body, reference, account_id=account_id, trade_date=trade_date, calendar=calendar
    )
    prefix = tuple(r for r in records if r.record_seq <= checkpoint.submission_high_watermark)
    if (
        prefix_hash(prefix) != checkpoint.submission_prefix_hash
        or (prefix[-1].record_seq if prefix else 0) != checkpoint.submission_high_watermark
        or any(
            r.submission.account_id != account_id or r.submission.trade_date > checkpoint.trade_date
            for r in prefix
        )
    ):
        raise ValueError("opening checkpoint submission prefix mismatch")
    current = tuple(r for r in records if r.record_seq > checkpoint.submission_high_watermark)
    if any(r.submission.trade_date != trade_date for r in current):
        raise ValueError("unsealed historical or future submission after closing prefix")
    return checkpoint, current


def load_opening(
    body: bytes | None,
    reference: OpeningReference,
    *,
    account_id: str,
    trade_date: date,
    calendar: ExchangeCalendar,
) -> PortfolioCheckpoint:
    """Validate metadata first; caller must still verify the entire submission prefix."""
    if body is None or digest(body) != reference.payload_hash:
        raise ValueError("opening checkpoint absent or hash mismatch")
    checkpoint = PortfolioCheckpoint.model_validate_json(body)
    if (
        checkpoint.account_id != account_id
        or checkpoint.trade_date != reference.trade_date
        or checkpoint.calendar_version != calendar.version
        or calendar.next_session(checkpoint.trade_date) != trade_date
    ):
        raise ValueError("opening checkpoint account, calendar or next session mismatch")
    return checkpoint
