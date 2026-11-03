"""Independent, bounded Tick archive. No Journal or broker dependency.

Segments are immutable JSONL, atomically published after fsync. Time bounds in
their names form the index. An interrupted session remains explicitly unclean;
neither persisted history nor a local sequence proves exchange completeness.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Literal, cast
from uuid import uuid4

from pydantic import AwareDatetime, Field, JsonValue

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.domain.trading_time import SHANGHAI_TIMEZONE
from auto_trading_system.gateways.market import BookLevel, MarketTick
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.records import MoneyText, PriceText, Symbol

_SEGMENT = re.compile(r"part-(\d{20})-(\d{20})-[a-f0-9]{32}\.jsonl")
_MAX_SEGMENT_ROWS = 256


class TickRecord(WireModel):
    version: Literal[1] = 1
    session: str = Field(pattern=r"^[a-f0-9]{32}$")
    symbol: Symbol
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    last_price: PriceText
    volume: int = Field(ge=0)
    turnover: MoneyText
    bids: tuple[tuple[PriceText, int], ...] = Field(max_length=10)
    asks: tuple[tuple[PriceText, int], ...] = Field(max_length=10)
    sequence: int | None
    trading_status: int | None

    @classmethod
    def capture(cls, tick: MarketTick, session: str) -> TickRecord:
        return cls(
            session=session,
            symbol=tick.symbol.to_wire(),
            occurred_at=tick.occurred_at.value,
            received_at=tick.received_at.value,
            last_price=format(tick.last_price.amount, "f"),
            volume=tick.cumulative_volume.value,
            turnover=format(tick.cumulative_turnover.amount, "f"),
            bids=tuple((format(b.price.amount, "f"), b.quantity.value) for b in tick.bids),
            asks=tuple((format(b.price.amount, "f"), b.quantity.value) for b in tick.asks),
            sequence=tick.source_seq,
            trading_status=tick.trading_status,
        )

    def tick(self) -> MarketTick:
        return MarketTick(
            SecurityCode.parse(self.symbol),
            ShanghaiTimestamp(self.occurred_at),
            ShanghaiTimestamp(self.received_at),
            Price.parse(self.last_price),
            Quantity(self.volume),
            Money.parse(self.turnover),
            tuple(BookLevel(Price.parse(p), Quantity(q)) for p, q in self.bids),
            tuple(BookLevel(Price.parse(p), Quantity(q)) for p, q in self.asks),
            self.sequence,
            self.trading_status,
        )


@dataclass(frozen=True)
class TickHistory:
    records: tuple[TickRecord, ...]
    issues: tuple[str, ...]
    truncated: bool = False


def _instant(at: datetime) -> int:
    if at.utcoffset() is None:
        raise ValueError("history timestamps must have a timezone")
    return int(at.timestamp() * 1_000_000)


def _safe(root: Path, path: Path) -> Path:
    """Never follow reparse points outside the dedicated archive tree."""
    if root.resolve() != root.absolute() or path.resolve() != path.absolute():
        raise ValueError("market archive paths must not traverse links")
    if not path.is_relative_to(root) or path == root:
        raise ValueError("market archive path outside root")
    return path


def _publish(path: Path, body: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("xb") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TickHistoryReader:
    """Read by symbol and inclusive time range, independently of a live engine.

    Empty/missing, corrupt, unclean and cross-session history is explicit. The
    source completeness caveat always applies: these are subscribed snapshots,
    not an exchange transaction-by-transaction feed.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def query(
        self, symbol: str, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> TickHistory:
        symbol = SecurityCode.parse(symbol).to_wire()
        lo, hi = _instant(start), _instant(end)
        if lo > hi or not 1 <= limit <= 100_000:
            raise ValueError("invalid history range or limit")
        directory = _safe(self.root, self.root / symbol)
        issues = {"SOURCE_COMPLETENESS_UNVERIFIED"}
        records: list[TickRecord] = []
        sessions: set[str] = set()
        deadline = monotonic() + 5
        truncated = False
        first_day = start.astimezone(SHANGHAI_TIMEZONE).date().isoformat()
        last_day = end.astimezone(SHANGHAI_TIMEZONE).date().isoformat()
        for day in sorted(directory.glob("????-??-??")):
            if not first_day <= day.name <= last_day:
                continue
            _safe(self.root, day)
            for segment in sorted(day.iterdir()):
                if monotonic() >= deadline:
                    raise TimeoutError("market history query exceeded time budget")
                match = _SEGMENT.fullmatch(segment.name)
                if match is None:
                    if segment.suffix == ".tmp":
                        issues.add("UNCOMMITTED_SEGMENT")
                    continue
                if int(match[2]) < lo or int(match[1]) > hi:
                    continue
                _safe(self.root, segment)
                if segment.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("market segment exceeds read budget")
                for line in segment.read_bytes().splitlines():
                    envelope = parse_bytes(line, max_bytes=64 * 1024)
                    if not isinstance(envelope, dict) or set(envelope) != {"record", "sha256"}:
                        raise ValueError("invalid market segment envelope")
                    body = canonical_bytes(envelope["record"])
                    if digest(body) != envelope["sha256"]:
                        raise ValueError("market segment checksum differs")
                    record = TickRecord.model_validate_json(body)
                    record.tick()  # Validate domain values before releasing history.
                    if (
                        record.symbol != symbol
                        or record.occurred_at.astimezone(SHANGHAI_TIMEZONE).date().isoformat()
                        != day.name
                    ):
                        raise ValueError("market segment scope differs")
                    instant = _instant(record.occurred_at)
                    if not int(match[1]) <= instant <= int(match[2]):
                        raise ValueError("market time index differs")
                    if lo <= instant <= hi:
                        if len(records) == limit:
                            truncated = True
                            break
                        records.append(record)
                        sessions.add(record.session)
                if truncated:
                    break
            if truncated:
                break
        records.sort(key=lambda r: (r.occurred_at, r.received_at, r.session, r.sequence or 0))
        for session in sessions:
            marker = _safe(self.root, self.root / ".sessions" / f"{session}.json")
            state = parse_bytes(marker.read_bytes(), max_bytes=1024) if marker.exists() else None
            if not isinstance(state, dict) or state.get("closed") is not True:
                issues.add("UNCLEAN_OR_ACTIVE_SESSION")
        if len(sessions) > 1:
            issues.add("SESSION_BOUNDARY")
        if not records or records[0].occurred_at > start or records[-1].occurred_at < end:
            issues.add("RANGE_NOT_COVERED")
        if truncated:
            issues.add("QUERY_TRUNCATED")
        return TickHistory(tuple(records), tuple(sorted(issues)), truncated)


@dataclass(frozen=True)
class _Command:
    run: Callable[[], object]
    future: Future[object]


class TickArchive:
    """Single file worker. Enqueue never waits for disk; failure stops admission.

    A flush interval is a target, not a zero-loss guarantee: queued/buffered
    records can be lost on abrupt exit, especially during slow storage I/O.
    Session markers expose this uncertainty rather than claiming zero loss.
    """

    def __init__(self, root: Path, *, capacity: int = 8192, flush_seconds: float = 1) -> None:
        if capacity < 1 or not 0.01 <= flush_seconds <= 2:
            raise ValueError("invalid archive bounds")
        self.root = root.absolute()
        self.reader = TickHistoryReader(self.root)
        self.session = uuid4().hex
        self._queue: Queue[MarketTick | _Command] = Queue(maxsize=capacity)
        self._closing, self._failed = Event(), Event()
        self._ready: Future[None] = Future()
        self._lock = Lock()
        self._retired: set[str] = set()
        self._buffers: dict[tuple[str, str], list[TickRecord]] = {}
        self._accepted = self._durable = 0
        self._progress = monotonic()
        self._flush_seconds = flush_seconds
        self._thread = Thread(target=self._run, name="market-tick-archive", daemon=True)

    def start(self) -> None:
        self._thread.start()
        try:
            self._ready.result(timeout=5)
        except BaseException:
            self._failed.set()
            self._closing.set()
            raise

    @property
    def healthy(self) -> bool:
        if self._accepted > self._durable and monotonic() - self._progress > 5:
            self._failed.set()
        return self._ready.done() and not self._failed.is_set() and not self._closing.is_set()

    def append(self, tick: MarketTick) -> None:
        with self._lock:
            if tick.symbol.to_wire() in self._retired:
                return
            if not self.healthy:
                raise RuntimeError("MARKET_ARCHIVE_UNAVAILABLE")
            try:
                if self._accepted == self._durable:
                    self._progress = monotonic()
                self._queue.put_nowait(tick)
                self._accepted += 1
            except Full:
                self._failed.set()
                raise RuntimeError("MARKET_ARCHIVE_QUEUE_FULL") from None

    def activate(self, symbol: str) -> None:
        with self._lock:
            self._retired.discard(SecurityCode.parse(symbol).to_wire())

    def _command(self, run: Callable[[], object]) -> Future[object]:
        future: Future[object] = Future()
        with self._lock:
            if not self.healthy:
                future.set_exception(RuntimeError("MARKET_ARCHIVE_UNAVAILABLE"))
            else:
                try:
                    self._queue.put_nowait(_Command(run, future))
                except Full:
                    self._failed.set()
                    future.set_exception(RuntimeError("MARKET_ARCHIVE_QUEUE_FULL"))
        return future

    def query(
        self, symbol: str, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> Future[TickHistory]:
        symbol = SecurityCode.parse(symbol).to_wire()
        if _instant(start) > _instant(end) or not 1 <= limit <= 100_000:
            raise ValueError("invalid history range or limit")
        return cast(
            Future[TickHistory],
            self._command(lambda: self.reader.query(symbol, start, end, limit=limit)),
        )

    def flush(self) -> Future[object]:
        return self._command(lambda: None)  # Commands flush all earlier ticks first.

    def retire(self, symbol: str) -> Future[object]:
        symbol = SecurityCode.parse(symbol).to_wire()
        with self._lock:
            self._retired.add(symbol)
        return self._command(lambda: self._delete(symbol))

    def _delete(self, symbol: str) -> None:
        target = _safe(self.root, self.root / symbol)
        if not target.exists() or self._closing.is_set():
            return
        # Resolve and validate the entire exact symbol tree BEFORE a recursive
        # delete, including Windows junctions. Never delete the archive root.
        for entry in target.rglob("*"):
            _safe(self.root, entry)
        if self._closing.is_set():
            return
        # Detach first: a later subscription can create a new symbol directory
        # without having its files included in this background recursive delete.
        retired = _safe(self.root, self.root / f".retired-{symbol}-{uuid4().hex}")
        target.rename(retired)
        shutil.rmtree(retired)

    def close(self, timeout: float = 2) -> bool:
        with self._lock:
            self._closing.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def status(self) -> dict[str, JsonValue]:
        return {
            "healthy": self.healthy,
            "accepted": self._accepted,
            "durable": self._durable,
            "pending": self._accepted - self._durable,
            "session": self.session,
        }

    def _flush(self) -> None:
        for (symbol, day), records in tuple(self._buffers.items()):
            if not records:
                continue
            directory = _safe(self.root, self.root / symbol / day)
            directory.mkdir(parents=True, exist_ok=True)
            times = [_instant(r.occurred_at) for r in records]
            name = f"part-{min(times):020d}-{max(times):020d}-{uuid4().hex}.jsonl"
            lines = []
            for record in records:
                body = record.model_dump(mode="json")
                lines.append(
                    canonical_bytes({"record": body, "sha256": digest(canonical_bytes(body))})
                )
            _publish(_safe(self.root, directory / name), b"\n".join(lines) + b"\n")
            with self._lock:
                self._durable += len(records)
                self._progress = monotonic()
            del self._buffers[symbol, day]

    def _run(self) -> None:
        marker = self.root / ".sessions" / f"{self.session}.json"
        try:
            _safe(self.root, marker)
            marker.parent.mkdir(parents=True, exist_ok=True)
            _publish(marker, canonical_bytes({"closed": False}))
            self._ready.set_result(None)
            next_flush = monotonic() + self._flush_seconds
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    work = self._queue.get(timeout=min(0.05, self._flush_seconds))
                except Empty:
                    work = None
                if isinstance(work, _Command):
                    if not work.future.set_running_or_notify_cancel():
                        continue
                    try:
                        self._flush()
                        work.future.set_result(work.run())
                    except Exception as exc:
                        work.future.set_exception(exc)
                        raise
                elif isinstance(work, MarketTick):
                    record = TickRecord.capture(work, self.session)
                    day = record.occurred_at.astimezone(SHANGHAI_TIMEZONE).date().isoformat()
                    rows = self._buffers.setdefault((record.symbol, day), [])
                    rows.append(record)
                    if len(rows) >= _MAX_SEGMENT_ROWS:
                        self._flush()
                if monotonic() >= next_flush:
                    self._flush()
                    next_flush = monotonic() + self._flush_seconds
            self._flush()
            if not self._failed.is_set():
                _publish(marker, canonical_bytes({"closed": True}))
        except BaseException as exc:
            self._failed.set()
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            while True:
                try:
                    pending = self._queue.get_nowait()
                except Empty:
                    break
                if isinstance(pending, _Command) and not pending.future.done():
                    pending.future.set_exception(RuntimeError("MARKET_ARCHIVE_UNAVAILABLE"))
