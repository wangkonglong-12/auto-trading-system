"""Immutable export handoff, captured by the engine after durable day-end recovery.

It carries the exact queried facts rather than reconstructing broker values from
UI projections. Disk packaging and network upload run outside the engine owner.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.reconcile.position_lots import PositionLot
from auto_trading_system.runtime.recovery_v2 import BrokerRecoverySnapshot, RecoveryReport
from auto_trading_system.strategies.entry_state import EntryTarget


@dataclass(frozen=True)
class DaySnapshot:
    report: RecoveryReport
    broker: BrokerRecoverySnapshot
    submissions: tuple[RecoveryRecord, ...]
    lots: tuple[PositionLot, ...]
    plan: PlanEnvelope | None
    opening_hash: str | None
    journal_high_watermark: int
    entry_targets: tuple[EntryTarget, ...] = ()
