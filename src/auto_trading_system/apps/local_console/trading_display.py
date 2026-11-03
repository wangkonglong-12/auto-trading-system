"""Read-only display periods; these snapshots never feed order execution."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import AwareDatetime, JsonValue, ValidationError

from auto_trading_system.domain.trading_time import SHANGHAI_TIMEZONE
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes

from .buy_dashboard import buy_dashboard, transaction_rows


def _sell_states(
    result: dict[str, JsonValue],
    details: Mapping[str, JsonValue],
    engine: Mapping[str, JsonValue],
) -> None:
    """Overlay current batch bindings on historical buy rows; never persist authority."""
    box = engine.get("board_sell")
    boards = box.get("boards") if isinstance(box, dict) else None
    lots = details.get("managed_lots")
    rows = result.get("rows")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict) or not row.get("quantity"):
            continue
        row["sell_state"] = "SELL_UNVERIFIED"
        if (
            details.get("available") is not True
            or details.get("truncated") is True
            or engine.get("recovery") != "CLEAR"
            or not isinstance(boards, dict)
            or not isinstance(lots, list)
            or not isinstance(box, dict)
            or box.get("restored") is not True
        ):
            continue
        matching = [
            lot
            for lot in lots
            if isinstance(lot, dict)
            and lot.get("symbol") == row.get("symbol")
            and lot.get("acquired_on") == (row.get("trade_date") or result.get("selection_date"))
        ]
        if not matching:
            continue
        states = []
        for lot in matching:
            state = "SELL_NOT_BOUND"
            for board in boards.values():
                if not isinstance(board, dict):
                    continue
                targets = board.get("targets")
                target = (
                    next(
                        (
                            t
                            for t in targets
                            if isinstance(t, dict)
                            and t.get("lot_id") == lot.get("lot_id")
                            and t.get("symbol") == lot.get("symbol")
                        ),
                        None,
                    )
                    if isinstance(targets, list)
                    else None
                )
                if target is None:
                    continue
                if board.get("state") != "READY" or target.get("waiting_reason"):
                    state = "SELL_BLOCKED"
                elif (
                    engine.get("halted") is not False
                    or board.get("enabled") is not True
                    or board.get("authorized") is not True
                ):
                    state = "SELL_BOUND_PAUSED"
                else:
                    state = "SELL_TRIGGERED" if target.get("reasons") else "SELL_MONITORING"
                break
            states.append(state)
        row["sell_state"] = states[0] if len(set(states)) == 1 else "SELL_MIXED"


class DisplaySnapshot(WireModel):
    node_id: str
    account_id: str
    selection_date: date
    captured_at: AwareDatetime
    buys: dict[str, JsonValue]
    phases: list[str]


class ManualSnapshots(WireModel):
    snapshots: tuple[DisplaySnapshot, ...] = ()


class TradingDisplay:
    def __init__(
        self,
        data_root: Path,
        *,
        node_id: str,
        account_id: str,
        sessions: Sequence[date],
        calendar_reader: Callable[[date], Sequence[date]] | None = None,
    ) -> None:
        self.path = data_root / "operator" / "today_trading.json"
        self.manual_path = data_root / "operator" / "manual_trading.json"
        self.node_id, self.account_id = node_id, account_id
        self.sessions = sorted(set(sessions))
        self._lock = Lock()
        self._calendar_reader = calendar_reader
        self._calendar_checked_at: datetime | None = None
        self._calendar_loaded_on: date | None = None

    def _refresh_calendar(self, now: datetime) -> None:
        if self._calendar_reader is None or self._calendar_loaded_on == now.date():
            return
        if self._calendar_checked_at is not None and (
            0 <= (now - self._calendar_checked_at).total_seconds() < 60
        ):
            return
        self._calendar_checked_at = now
        try:
            days = tuple(self._calendar_reader(now.date()))
        except Exception:
            return
        if (
            len(days) >= 2
            and all(type(day) is date for day in days)
            and days == tuple(sorted(set(days)))
            and days[0] <= now.date()
            and days[-1] >= now.date() - timedelta(days=31)
        ):
            self.sessions = list(days)
            if days[-1] >= now.date():
                self._calendar_loaded_on = now.date()

    def _read(self) -> DisplaySnapshot | None:
        try:
            result = DisplaySnapshot.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError):
            return None
        return (
            result
            if (result.node_id, result.account_id) == (self.node_id, self.account_id)
            else None
        )

    def _save(self, value: DisplaySnapshot) -> bool:
        body = canonical_bytes(value.model_dump(mode="json"))
        return self._write(self.path, body)

    def _write(self, path: Path, body: bytes) -> bool:
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            if path.exists() and path.read_bytes() == body:
                return True
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return True
        except OSError:
            return False
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def project(
        self,
        details: Mapping[str, JsonValue],
        engine: Mapping[str, JsonValue],
        *,
        maximum: int | None,
        now: datetime,
    ) -> dict[str, JsonValue]:
        with self._lock:
            now = now.astimezone(SHANGHAI_TIMEZONE)
            submissions = details.get("submissions")
            manual = (
                [s for s in submissions if isinstance(s, dict) and s.get("source") == "MANUAL"]
                if isinstance(submissions, list)
                else []
            )
            manual_ids = {s["client_order_id"] for s in manual}
            auto_details = dict(details)
            for key in ("orders", "trades", "submissions"):
                values = details.get(key)
                if isinstance(values, list):
                    auto_details[key] = [
                        v
                        for v in values
                        if isinstance(v, dict) and v.get("client_order_id") not in manual_ids
                    ]
            result = self._project(auto_details, engine, maximum=maximum, now=now)
            plan = engine.get("plan")
            current_engine = (
                engine
                if isinstance(plan, dict) and plan.get("trade_date") == now.date().isoformat()
                else {}
            )
            summary = buy_dashboard(details, current_engine, maximum=maximum)
            if (
                details.get("trade_date") != now.date().isoformat()
                or engine.get("recovery") != "CLEAR"
            ):
                summary = buy_dashboard({"available": False}, {}, maximum=maximum)
            result["today_summary"] = {
                k: summary[k]
                for k in (
                    "available",
                    "completed",
                    "partial",
                    "pending",
                    "not_bought",
                    "occupied",
                    "remaining",
                    "maximum",
                )
            }
            # Keep selection statistics and original snapshots intact. Only the
            # transaction table excludes candidates without an order/submission.
            result["rows"] = transaction_rows(result.get("rows"))
            try:
                saved = ManualSnapshots.model_validate_json(self.manual_path.read_bytes()).snapshots
            except (OSError, ValidationError):
                saved = ()
            retained = {}
            for item in saved:
                if (item.node_id, item.account_id) != (
                    self.node_id,
                    self.account_id,
                ) or item.selection_date > now.date():
                    continue
                end = next((d for d in self.sessions if d > item.selection_date), None)
                if end is None or now < datetime.combine(end, time(14, 30), SHANGHAI_TIMEZONE):
                    retained[item.selection_date] = item
            live = False
            try:
                observed = datetime.fromisoformat(str(details.get("observed_at")))
                live = bool(
                    manual
                    and summary["available"]
                    and observed.tzinfo is not None
                    and observed.astimezone(SHANGHAI_TIMEZONE).date() == now.date()
                    and 0 <= (now - observed).total_seconds() < 15
                )
            except ValueError:
                pass
            if live:
                manual_details = dict(details)
                manual_details["submissions"] = [dict(s) for s in manual]
                for key in ("orders", "trades"):
                    values = details.get(key)
                    manual_details[key] = (
                        [
                            v
                            for v in values
                            if isinstance(v, dict) and v.get("client_order_id") in manual_ids
                        ]
                        if isinstance(values, list)
                        else []
                    )
                candidate = DisplaySnapshot(
                    node_id=self.node_id,
                    account_id=self.account_id,
                    selection_date=now.date(),
                    captured_at=observed,
                    buys=buy_dashboard(manual_details, {}, maximum=maximum),
                    phases=[],
                )
                previous = retained.get(now.date())
                if previous is None or candidate.captured_at >= previous.captured_at:
                    retained[now.date()] = candidate
                else:
                    live = False
            items = sorted(retained.values(), key=lambda item: item.selection_date)[-2:]
            if items or saved:
                stored = self._write(
                    self.manual_path,
                    canonical_bytes(
                        ManualSnapshots(snapshots=tuple(items)).model_dump(mode="json")
                    ),
                )
                result["snapshot_saved"] = result.get("snapshot_saved", True) and stored
            if not items:
                _sell_states(result, details, engine)
                return result
            # Independent row freshness prevents an old selection snapshot hiding today's orders.
            auto_rows = result.get("rows")
            rows = (
                [
                    {
                        **row,
                        "trade_date": row.get("trade_date") or result.get("selection_date"),
                        "expires_at": result.get("expires_at"),
                        "is_snapshot": result.get("display_state") == "SNAPSHOT",
                    }
                    for row in auto_rows
                    if isinstance(row, dict)
                ]
                if isinstance(auto_rows, list)
                else []
            )
            manual_rows: list[dict[str, JsonValue]] = []
            for item in items:
                values = item.buys.get("rows")
                if isinstance(values, list):
                    manual_rows.extend(
                        {
                            **row,
                            "trade_date": item.selection_date.isoformat(),
                            "is_snapshot": not (live and item.selection_date == now.date()),
                            "observed_at": item.captured_at.isoformat(),
                            "expires_at": next(
                                (
                                    datetime.combine(d, time(14, 30), SHANGHAI_TIMEZONE).isoformat()
                                    for d in self.sessions
                                    if d > item.selection_date
                                ),
                                None,
                            ),
                        }
                        for row in values
                        if isinstance(row, dict)
                    )
            replaced = {(r.get("trade_date"), r.get("symbol")) for r in manual_rows}
            rows = [r for r in rows if (r.get("trade_date"), r.get("symbol")) not in replaced]
            result["rows"] = [*rows, *manual_rows]
            result["has_manual"] = True
            result["available"] = True
            _sell_states(result, details, engine)
            return result

    def _project(
        self,
        details: Mapping[str, JsonValue],
        engine: Mapping[str, JsonValue],
        *,
        maximum: int | None,
        now: datetime,
    ) -> dict[str, JsonValue]:
        now = now.astimezone(SHANGHAI_TIMEZONE)
        self._refresh_calendar(now)
        boundaries = [
            datetime.combine(day, time(14, 30), SHANGHAI_TIMEZONE) for day in self.sessions
        ]
        start = next((item for item in reversed(boundaries) if item <= now), None)
        end = next((item for item in boundaries if item > now), None)
        saved = self._read()
        calendar_pending = start is None or end is None
        plan = engine.get("plan")
        # Historical QMT calendars may stop at today. A dated selection can
        # still be shown; the next confirmed session supplies its expiry later.
        if (
            calendar_pending
            and isinstance(plan, dict)
            and (plan.get("trade_date") == now.date().isoformat() and now.time() >= time(14, 30))
        ):
            start = datetime.combine(now.date(), time(14, 30), SHANGHAI_TIMEZONE)
        elif saved is not None and saved.selection_date <= now.date():
            saved_start = datetime.combine(saved.selection_date, time(14, 30), SHANGHAI_TIMEZONE)
            if saved_start <= now and (start is None or saved_start > start):
                start = saved_start
        empty = buy_dashboard({"available": False}, {}, maximum=maximum)
        empty.update(
            {
                "selection_date": start.date().isoformat() if start else None,
                "starts_at": start.isoformat() if start else None,
                "expires_at": end.isoformat() if end else None,
                "display_state": "WAITING_SELECTION",
                "calendar_pending": calendar_pending,
                "decision_phases": [],
                "snapshot_saved": True,
            }
        )
        # No weekday guesses beyond the installed exchange calendar.
        if start is None:
            return empty
        if saved is not None and saved.selection_date > start.date():
            return empty
        if saved is not None and saved.selection_date != start.date():
            saved = None
        live_current = False
        ready = (
            isinstance(plan, dict)
            and plan.get("trade_date") == start.date().isoformat()
            and engine.get("trade_date", start.date().isoformat()) == start.date().isoformat()
        )
        if ready and now.date() == start.date():
            try:
                observed = datetime.fromisoformat(str(details.get("observed_at")))
                same_day = (
                    observed.tzinfo is not None
                    and observed.astimezone(SHANGHAI_TIMEZONE).date() == start.date()
                    and 0 <= (now - observed).total_seconds() < 15
                )
            except ValueError:
                same_day = False
            buys = buy_dashboard(details, engine, maximum=maximum)
            phases = plan.get("decision_phases") if isinstance(plan, dict) else None
            phase_list = (
                [p for p in phases if isinstance(p, str) and p in ("1440", "1445", "1450")]
                if isinstance(phases, list)
                else []
            )
            if same_day and buys["available"] is True:
                candidate = DisplaySnapshot(
                    node_id=self.node_id,
                    account_id=self.account_id,
                    selection_date=start.date(),
                    captured_at=observed,
                    buys=buys,
                    phases=phase_list,
                )
                # An older concurrent query must not roll back newer display facts.
                if saved is None or candidate.captured_at >= saved.captured_at:
                    saved = candidate
                    empty["snapshot_saved"] = self._save(candidate)
                    live_current = True
            elif saved is None:
                empty["rows"] = buy_dashboard(
                    {**details, "available": False}, engine, maximum=maximum
                )["rows"]
                empty["display_state"] = "UNAVAILABLE"
                return empty
        if saved is None:
            return empty
        result = {
            **saved.buys,
            **{
                key: empty[key]
                for key in (
                    "selection_date",
                    "starts_at",
                    "expires_at",
                    "snapshot_saved",
                    "calendar_pending",
                )
            },
        }
        result["display_state"] = "CURRENT" if live_current else "SNAPSHOT"
        result["decision_phases"] = list(saved.phases)
        return result
