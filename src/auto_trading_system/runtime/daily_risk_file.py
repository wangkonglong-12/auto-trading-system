"""Create-once daily equity baseline used by the live loss circuit breaker."""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from auto_trading_system.protocol.v2.base import WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.records import MoneyText


class DailyRiskBaseline(WireModel):
    format: Literal["daily-risk-baseline/v1"] = "daily-risk-baseline/v1"
    account_id: WireId
    trade_date: date
    total_equity: MoneyText = Field(description="首次完成当日账户核对时的总资产")


def load_daily_risk_baseline(
    path: Path, *, account_id: str, trade_date: date
) -> DailyRiskBaseline | None:
    try:
        value = DailyRiskBaseline.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    if value.account_id != account_id or value.trade_date != trade_date:
        return None
    return value


def save_daily_risk_baseline(path: Path, baseline: DailyRiskBaseline) -> DailyRiskBaseline:
    """Publish once; concurrent/restarted writers must accept the first value."""
    existing = load_daily_risk_baseline(
        path, account_id=baseline.account_id, trade_date=baseline.trade_date
    )
    if existing is not None:
        return existing
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_bytes(baseline.model_dump(mode="json")))
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            # The account mutex guarantees one engine. A different date is a
            # completed session's stale pointer and can be atomically advanced.
            os.replace(temporary, path)
        else:
            with suppress(FileExistsError):
                os.rename(temporary, path)
        saved = load_daily_risk_baseline(
            path, account_id=baseline.account_id, trade_date=baseline.trade_date
        )
        if saved is None:
            raise ValueError("daily risk baseline publication failed")
        return saved
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
