"""Historical payload checks, transport selection and live-ingestion rejection."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_tail_feed import NOW, local_feed, wire

from auto_trading_system.protocol.tail_picks import TailPicks, TailPicksPreview
from auto_trading_system.sync import tail_feed_probe
from auto_trading_system.sync.tail_feed import TailFeedHttp
from auto_trading_system.sync.tail_feed_probe import inspect_preview


def preview_wire() -> dict[str, Any]:
    value = wire()
    value.update(
        mode="TEST",
        status="FOUND",
        requested_date="2026-10-23",
        trade_date="2026-10-19",
        lookback_days=366,
    )
    for batch in value["batches"]:
        batch["completed_at"] = batch["completed_at"].replace("2026-10-23", "2026-10-19")
        batch["stocks"] = [{"code": "000001.SZ", "name": "平安银行", "rank": 1}]
    return value


def body() -> bytes:
    return json.dumps(preview_wire()).encode()


def test_history_is_parsed_and_deduped_for_report_but_not_accepted_for_trading(
    tmp_path: Path,
) -> None:
    report = inspect_preview(body(), NOW)
    assert report["trade_date"] == "2026-10-19" and report["executable"] is False
    assert report["unique_stock_count"] == 1
    assert report["repeated_across_phases"] == ["000001.SZ"]
    feed = local_feed(tmp_path)
    with pytest.raises(ValueError):
        feed.accept(body(), NOW)
    assert feed.snapshot is None and not feed.path.exists() and not feed.plans.plans.accepted()
    with pytest.raises(ValueError):
        TailPicks.model_validate_json(body())


@pytest.mark.parametrize(
    "change",
    [
        {"trade_date": "2026-10-24"},
        {"trade_date": "2025-10-01"},
        {"requested_date": "2026-10-22"},
        {"lookback_days": 0},
        {"mode": "LIVE"},
        {"status": "NOT_FOUND"},
        {"batches": []},
        {"server_time": "2026-10-23T14:51:00Z"},
    ],
)
def test_invalid_history_is_rejected(change: dict[str, object]) -> None:
    value = preview_wire()
    value.update(change)
    with pytest.raises(ValueError):
        inspect_preview(json.dumps(value).encode(), NOW)


def test_empty_search_and_bad_completion_and_stale_report() -> None:
    value = preview_wire()
    value.update(status="NOT_FOUND", trade_date=None, batches=[])
    assert inspect_preview(json.dumps(value).encode(), NOW)["unique_stock_count"] == 0
    for completed in ("2026-10-23T14:40:01+08:00", "2026-10-19T14:39:00+08:00"):
        value = preview_wire()
        value["batches"][0]["completed_at"] = completed
        with pytest.raises(ValueError):
            inspect_preview(json.dumps(value).encode(), NOW)
    with pytest.raises(ValueError):
        inspect_preview(body(), NOW.replace(minute=53))


def test_transport_sets_test_only_for_preview_and_live_refuses_test_response() -> None:
    requests = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body())

    client = TailFeedHttp("https://quant.test", b"x" * 43, transport=httpx.MockTransport(reply))
    try:
        assert TailPicksPreview.model_validate_json(client.get_preview(NOW)).mode == "TEST"
        assert requests[0].url.params["test"] == "true"
        with pytest.raises(ValueError):
            client.get(NOW)
        assert "test" not in requests[1].url.params
    finally:
        client.close()


def test_probe_offline_report_and_existing_report_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls(2026, 10, 23, 14, 51, tzinfo=NOW.tzinfo)

    source, output = tmp_path / "response.json", tmp_path / "report.json"
    source.write_bytes(body())
    monkeypatch.setattr(tail_feed_probe, "datetime", Clock)
    monkeypatch.setattr("sys.argv", ["probe", "--input", str(source), "--output", str(output)])
    assert tail_feed_probe.main() == 0
    assert json.loads(output.read_text(encoding="utf-8"))["executable"] is False
    first = output.read_bytes()
    assert tail_feed_probe.main() == 1
    assert output.read_bytes() == first
    assert "名单检查失败" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["probe", "--output", str(output)])
    with pytest.raises(SystemExit):
        tail_feed_probe.main()


def test_probe_network_branch_closes_client_and_does_not_echo_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    closed = []

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_preview(self, now: datetime) -> bytes:
            raise ValueError("test-only-private-token")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv("TAIL_PICKS_TEST_TOKEN", "test-only-private-token")
    monkeypatch.setattr(tail_feed_probe, "TailFeedHttp", Client)
    monkeypatch.setattr(
        "sys.argv",
        ["probe", "--origin", "https://quant.test", "--output", str(tmp_path / "report.json")],
    )
    assert tail_feed_probe.main() == 1 and closed == [True]
    assert "test-only-private-token" not in capsys.readouterr().out
