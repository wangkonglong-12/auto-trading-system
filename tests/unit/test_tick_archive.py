from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.market import BookLevel, MarketTick, MockMarketGateway
from auto_trading_system.recorder.tick_archive import TickArchive, TickHistory, TickHistoryReader
from auto_trading_system.runtime.plan_market import PlanMarketRuntime
from auto_trading_system.testing.v31_scenarios import example_plan

AT = datetime.fromisoformat("2026-09-07T14:40:00+08:00")


def tick(seq: int = 1, *, at: datetime = AT, symbol: str = "000001.SZ") -> MarketTick:
    stamp = ShanghaiTimestamp(at)
    return MarketTick(
        SecurityCode.parse(symbol),
        stamp,
        stamp,
        Price.parse("10.12"),
        Quantity(100_000 + seq),
        Money.parse("1000000.00"),
        (BookLevel(Price.parse("10.11"), Quantity(200)),),
        (BookLevel(Price.parse("10.13"), Quantity(300)),),
        seq,
        13,
    )


def test_all_tick_fields_survive_flush_restart_and_time_queries(tmp_path: Path) -> None:
    root = tmp_path / "ticks"
    archive = TickArchive(root)
    archive.start()
    values = [tick(i + 1, at=AT + timedelta(seconds=i)) for i in range(3)]
    for value in values:
        archive.append(value)
    history = archive.query("000001.SZ", AT, AT + timedelta(seconds=2)).result(2)
    assert isinstance(history, TickHistory)
    assert [r.tick() for r in history.records] == values
    assert "UNCLEAN_OR_ACTIVE_SESSION" in history.issues
    assert archive.status()["durable"] == 3
    assert archive.close()
    reader = TickHistoryReader(root)
    history = reader.query("000001.SZ", AT, AT + timedelta(seconds=2))
    assert [r.tick() for r in history.records] == values
    assert history.issues == ("SOURCE_COMPLETENESS_UNVERIFIED",)
    selected = reader.query("000001.SZ", AT + timedelta(seconds=1), AT + timedelta(seconds=1))
    assert [r.tick() for r in selected.records] == [values[1]]
    assert not reader.query("600000.SH", AT, AT).records


def test_time_index_skips_unrelated_segments_and_reports_limit(tmp_path: Path) -> None:
    archive = TickArchive(tmp_path)
    archive.start()
    try:
        archive.append(tick())
        archive.flush().result(2)
        old = next((tmp_path / "000001.SZ/2026-09-07").glob("*.jsonl"))
        archive.append(tick(2, at=AT + timedelta(minutes=1)))
        archive.append(tick(3, at=AT + timedelta(minutes=2)))
        archive.flush().result(2)
        old.write_bytes(b"corrupt old segment")
        selected = archive.reader.query(
            "000001.SZ", AT + timedelta(minutes=1), AT + timedelta(minutes=2), limit=1
        )
        assert selected.truncated and len(selected.records) == 1
        assert "QUERY_TRUNCATED" in selected.issues
        with pytest.raises(ValueError):
            archive.reader.query("000001.SZ", AT, AT)
    finally:
        assert archive.close()


def test_cross_day_and_session_history_keeps_restarted_sequence_numbers(tmp_path: Path) -> None:
    archive = TickArchive(tmp_path)
    archive.start()
    archive.append(tick(1))
    assert archive.close()
    restarted = TickArchive(tmp_path)
    restarted.start()
    restarted.append(tick(1, at=AT + timedelta(days=1)))
    assert restarted.close()
    history = TickHistoryReader(tmp_path).query("000001.SZ", AT, AT + timedelta(days=1))
    assert len(history.records) == 2
    assert history.records[0].session != history.records[1].session
    assert "SESSION_BOUNDARY" in history.issues


def test_checksum_failure_is_never_released_as_history(tmp_path: Path) -> None:
    archive = TickArchive(tmp_path)
    archive.start()
    archive.append(tick())
    assert archive.close()
    segment = next((tmp_path / "000001.SZ/2026-09-07").glob("*.jsonl"))
    segment.write_bytes(segment.read_bytes().replace(b"10.12", b"10.99"))
    with pytest.raises(ValueError, match="checksum"):
        TickHistoryReader(tmp_path).query("000001.SZ", AT, AT)


def test_unclean_session_and_uncommitted_tail_are_visible(tmp_path: Path) -> None:
    archive = TickArchive(tmp_path)
    archive.start()
    archive.append(tick())
    archive.flush().result(2)
    # Emulate an interrupted session, without killing any user-owned process.
    archive._failed.set()
    assert archive.close()
    day = tmp_path / "000001.SZ/2026-09-07"
    (day / "incomplete.jsonl.tmp").write_bytes(b"partial")
    result = TickHistoryReader(tmp_path).query("000001.SZ", AT, AT)
    assert len(result.records) == 1
    assert {"UNCLEAN_OR_ACTIVE_SESSION", "UNCOMMITTED_SEGMENT"} <= set(result.issues)


def test_recording_failure_rejects_flush_instead_of_leaving_future_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = TickArchive(tmp_path)
    archive.start()

    def full_disk() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(archive, "_flush", full_disk)
    with pytest.raises(OSError, match="disk full"):
        archive.flush().result(2)
    assert not archive.healthy
    assert archive.close()


def test_blocked_writer_does_not_block_callbacks_and_queue_overflow_is_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = TickArchive(tmp_path, capacity=1)
    archive.start()
    entered, release = Event(), Event()
    original = archive._flush

    def slow_flush() -> None:
        entered.set()
        assert release.wait(3)
        original()

    monkeypatch.setattr(archive, "_flush", slow_flush)
    flush = archive.flush()
    try:
        assert entered.wait(1)
        archive.append(tick())
        with pytest.raises(RuntimeError, match="QUEUE_FULL"):
            archive.append(tick(2))
        assert not archive.healthy
        assert not archive.close(timeout=0.01)
    finally:
        release.set()
        flush.result(2)
        assert archive.close()


def test_retire_deletes_only_exact_symbol_after_prior_ticks_and_can_reactivate(
    tmp_path: Path,
) -> None:
    archive = TickArchive(tmp_path)
    archive.start()
    try:
        archive.append(tick())
        archive.append(tick(symbol="600000.SH"))
        archive.retire("000001.SZ").result(2)
        archive.append(tick(2))  # A delayed callback cannot recreate a retired directory.
        archive.flush().result(2)
        assert not (tmp_path / "000001.SZ").exists()
        assert (tmp_path / "600000.SH").is_dir()
        archive.activate("000001.SZ")
        archive.append(tick(3))
        archive.flush().result(2)
        assert len(archive.reader.query("000001.SZ", AT, AT).records) == 1
    finally:
        assert archive.close()


@pytest.mark.parametrize("symbol", ["../critical", "..", "000001.SZ/../../critical"])
def test_unsafe_query_and_delete_symbols_are_rejected(tmp_path: Path, symbol: str) -> None:
    archive = TickArchive(tmp_path)
    archive.start()
    try:
        with pytest.raises(ValueError):
            archive.retire(symbol)
        with pytest.raises(ValueError):
            archive.reader.query(symbol, AT, AT)
    finally:
        assert archive.close()


def test_plan_callback_archives_every_tick_before_latest_cache_coalesces(tmp_path: Path) -> None:
    gateway = MockMarketGateway()
    archive = TickArchive(tmp_path)
    runtime = PlanMarketRuntime(gateway, archive=archive)
    try:
        runtime.reconcile(example_plan())
        for sequence in range(1, 4):
            gateway.publish(tick(sequence, at=AT + timedelta(seconds=sequence)))
        runtime._drain()
        assert runtime.latest["000001.SZ"].source_seq == 3
        archive.flush().result(2)
        assert len(archive.reader.query("000001.SZ", AT, AT + timedelta(seconds=3)).records) == 3
        runtime.cleanup_sold(frozenset({"000001.SZ"}))
        assert (tmp_path / "000001.SZ").exists()  # Still subscribed: not yet safe.
        runtime.reconcile(example_plan())
        runtime.cleanup_sold(frozenset({"000001.SZ"}))
        archive.flush().result(2)
        assert gateway.subscription_count == 0
        assert not (tmp_path / "000001.SZ").exists()
        runtime.cleanup_sold(frozenset())  # Missing/stale proof cannot resubscribe it.
        runtime.reconcile(example_plan())
        assert gateway.subscription_count == 0
        runtime.retain(frozenset({"000001.SZ"}))
        runtime.reconcile(example_plan())
        assert gateway.subscription_count == 1
    finally:
        runtime.stop()


def test_history_bounds_require_aware_ordered_times(tmp_path: Path) -> None:
    reader = TickHistoryReader(tmp_path)
    with pytest.raises(ValueError):
        reader.query("000001.SZ", AT.replace(tzinfo=None), AT)
    with pytest.raises(ValueError):
        reader.query("000001.SZ", AT + timedelta(seconds=1), AT)
    archive = TickArchive(tmp_path)
    archive.start()
    try:
        with pytest.raises(ValueError):
            archive.query("000001.SZ", AT + timedelta(seconds=1), AT)
        assert archive.healthy  # Bad query parameters do not poison recording.
    finally:
        assert archive.close()
