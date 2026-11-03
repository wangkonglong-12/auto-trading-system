"""Private, account-scoped cache of the agreeing SH/SZ execution calendar.

Desktop startup refreshes through its existing QMT adapter. HTTP/agent workers
only read this bounded file, never access QMT or invent exchange sessions.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.reconcile.position_lots import ExchangeCalendar

if TYPE_CHECKING:
    from auto_trading_system.config.node_v2 import NodeProcessConfig


class ExecutionCalendarCache(WireModel):
    schema_version: Literal[1] = 1
    source: Literal["QMT_SH_SZ"] = "QMT_SH_SZ"
    scope_hash: Sha256
    as_of: date
    calendar: ExchangeCalendar


def _path(root: Path) -> Path:
    path = root / "critical" / "execution-calendar.json"
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("execution calendar requires a direct path")
    return path


def cached_execution_calendar(config: NodeProcessConfig) -> ExchangeCalendar:
    try:
        with _path(config.data_root).open("rb") as stream:
            body = parse_bytes(stream.read(131073), max_bytes=131072)
    except FileNotFoundError:
        return config.calendar
    saved = ExecutionCalendarCache.model_validate_json(canonical_bytes(body))
    if saved.scope_hash != digest(config.scope()):
        raise ValueError("execution calendar scope differs")
    if saved.calendar.version != config.calendar.version:
        return config.calendar  # An explicitly changed installation needs a new refresh.
    if not set(config.calendar.sessions) <= set(saved.calendar.sessions):
        raise ValueError("execution calendar lost installed sessions")
    return saved.calendar


def refresh_execution_calendar(
    config: NodeProcessConfig,
    as_of: date,
    reader: Callable[[str, str, str], object],
) -> bool:
    start = date(as_of.year - 1, 1, 1)
    end = date(as_of.year, 12, 31)
    markets = []
    for market in ("SH", "SZ"):
        rows = reader(market, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if not isinstance(rows, list) or not 2 <= len(rows) <= 800:
            return False
        if any(
            not isinstance(row, str) or len(row) != 8 or not row.isascii() or not row.isdigit()
            for row in rows
        ):
            return False
        try:
            days = tuple(date(int(row[:4]), int(row[4:6]), int(row[6:])) for row in rows)
        except ValueError:
            return False
        if days != tuple(sorted(set(days))) or any(not start <= day <= end for day in days):
            return False
        markets.append(days)
    days = markets[0]
    if days != markets[1] or not days[0] <= as_of < days[-1]:
        return False
    existing = cached_execution_calendar(config)
    if any(start <= day <= end and day not in days for day in existing.sessions):
        return False
    calendar = ExchangeCalendar(
        version=config.calendar.version,
        sessions=tuple(sorted(set(existing.sessions) | set(days))),
    )
    cache = ExecutionCalendarCache(
        scope_hash=digest(config.scope()),
        as_of=as_of,
        calendar=calendar,
    )
    path = _path(config.data_root)
    temporary = path.with_name(".execution-calendar-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_bytes(cache.model_dump(mode="json")))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
