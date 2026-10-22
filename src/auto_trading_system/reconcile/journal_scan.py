"""Streaming validation of a sealed prefix, bounded retention of today's J1.

Historical bodies are checked one page at a time and then released. We retain
only a rolling prefix digest and the tightest SELL target for each live opening
lot. That preserves cross-day target constraints without retaining old orders.
The SQL rows remain intact for independent audit, backup and later maintenance.
"""

from __future__ import annotations

import hashlib
from datetime import date

from auto_trading_system.journal.critical_models import ActionTarget, RecoveryRecord
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.reconcile.portfolio_checkpoint import PortfolioCheckpoint

MAX_CURRENT_RECORDS = 2048
MAX_SCAN_RECORDS = 100_000


class PrefixDigest:
    """Same SHA256 as canonical [[record_seq, stored_payload_hash], ...]."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256(b"[")
        self.high_watermark = 0
        self.count = 0

    def add(self, record: RecoveryRecord) -> None:
        if record.record_seq <= self.high_watermark:
            raise ValueError("Journal sequence did not advance")
        if self.count:
            self._hash.update(b",")
        self._hash.update(canonical_bytes([record.record_seq, record.payload_hash]))
        self.high_watermark = record.record_seq
        self.count += 1

    @property
    def payload_hash(self) -> str:
        snapshot = self._hash.copy()
        snapshot.update(b"]")
        return "sha256:" + snapshot.hexdigest()


class JournalScan:
    def __init__(
        self, *, account_id: str, trade_date: date, checkpoint: PortfolioCheckpoint | None
    ) -> None:
        self.account_id, self.trade_date, self.checkpoint = account_id, trade_date, checkpoint
        self.prefix = PrefixDigest()
        self.current: list[RecoveryRecord] = []
        self.targets: dict[str, ActionTarget] = {}
        self._live_lots = (
            {lot.lot_id for lot in checkpoint.lots if lot.remaining} if checkpoint else set()
        )
        self._prefix_verified = checkpoint is None
        self._finished = False

    def _verify_prefix(self) -> None:
        if self._prefix_verified:
            return
        assert self.checkpoint is not None
        if (
            self.prefix.high_watermark != self.checkpoint.submission_high_watermark
            or self.prefix.payload_hash != self.checkpoint.submission_prefix_hash
        ):
            raise ValueError("opening checkpoint submission prefix mismatch")
        self._prefix_verified = True

    def consume(self, page: tuple[RecoveryRecord, ...]) -> None:
        if self._finished or not 1 <= len(page) <= 200:
            raise ValueError("invalid Journal scan page or lifecycle")
        for record in page:
            if self.prefix.count >= MAX_SCAN_RECORDS:
                raise ValueError("historical scan capacity exhausted")
            item = record.submission
            if item.account_id != self.account_id:
                raise ValueError("Journal account differs from engine")
            if self.checkpoint and record.record_seq <= self.checkpoint.submission_high_watermark:
                if item.trade_date > self.checkpoint.trade_date or self._prefix_verified:
                    raise ValueError("invalid record inside sealed prefix")
                binding = item.sell_binding
                if (
                    item.action_type == "NEW"
                    and item.side == "SELL"
                    and binding is not None
                    and any(a.lot_id in self._live_lots for a in binding.allocations)
                ):
                    old = self.targets.get(item.target.action_id)
                    if old and (
                        old.rule_id != item.target.rule_id
                        or item.target.target_remaining > old.target_remaining
                    ):
                        raise ValueError("historical target widened or changed rule")
                    self.targets[item.target.action_id] = item.target
                    if len(self.targets) > MAX_CURRENT_RECORDS:
                        raise ValueError("historical target capacity exhausted")
            else:
                self._verify_prefix()
                if item.trade_date != self.trade_date:
                    raise ValueError("unsealed historical or future submission")
                if len(self.current) >= MAX_CURRENT_RECORDS:
                    raise ValueError("current recovery record capacity exhausted")
                self.current.append(record)
            self.prefix.add(record)

    def finish(self) -> None:
        if self._finished:
            raise ValueError("Journal scan already finished")
        self._verify_prefix()
        self._finished = True
