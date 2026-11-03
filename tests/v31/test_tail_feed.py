"""Simple feed validation, durable completion and local execution-context fences."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from time import time_ns
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.ipc_protocol import IPCError, Request, RPCClient
from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.protocol.tail_picks import TailPicks
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import verify_plan
from auto_trading_system.runtime.tail_feed import LOCAL_FEED_KEY_ID, LocalTailFeed, installation_key
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.tail_feed import FeedRequestError, TailFeedHttp, TailFeedPuller

if TYPE_CHECKING:
    from auto_trading_system.runtime.node_engine import EngineEndpoint

NOW = datetime.fromisoformat("2026-10-23T14:51:00+08:00")


def wire(*, ready: tuple[str, ...] = ("1440", "1445", "1450")) -> dict[str, Any]:
    value = json.loads(Path("contracts/tail-picks/v1.response.example.json").read_bytes())
    value["server_time"] = NOW.isoformat()
    for batch in value["batches"]:
        batch.update(
            status="READY" if batch["phase"] in ready else "PENDING",
            stocks=[],
            source_run_id="tail-" + batch["phase"] if batch["phase"] in ready else None,
            completed_at=f"2026-10-23T14:{batch['phase'][2:]}:01+08:00"
            if batch["phase"] in ready
            else None,
        )
    return cast(dict[str, Any], value)


def node_config(root: Path) -> NodeProcessConfig:
    value = json.loads(Path("config/v2/node.mock.example.json").read_bytes())
    value.update(
        broker="QMT_TRADING",
        account_id="qmt-" + "a" * 16,
        data_root=str(root),
        operator_access={
            "actor_id": "local-operator",
            "password_verifier": {
                "source": "ENVIRONMENT",
                "variable": "ATS_SECRET_TEST_FEED_VERIFIER",
            },
        },
        qmt_read_only={
            "userdata_mini": str(root / "userdata_mini"),
            "account_fingerprint": "a" * 16,
        },
        tail_picks={"token": {"source": "ENVIRONMENT", "variable": "ATS_SECRET_TEST_FEED_TOKEN"}},
        calendar={"version": "feed-test", "sessions": ["2026-10-23", "2026-10-26"]},
    )
    return NodeProcessConfig.model_validate_json(json.dumps(value))


def local_feed(root: Path, now: datetime = NOW) -> LocalTailFeed:
    root.mkdir(parents=True, exist_ok=True)
    (root / "critical").mkdir(exist_ok=True)
    config = node_config(root)
    settings = TradingSettings.model_validate(
        {
            "buy": {
                "signal_mode": "TAIL_EXCELLENT_3_PHASES",
                "max_daily_symbols": 2,
                "max_daily_buy_value": "20000.00",
                "max_symbol_buy_value": "10000.00",
                "max_order_value": "10000.00",
                "cancel_after_seconds": 10,
                "max_retries": 2,
            },
            "sell": None,
            "safety": {
                "max_daily_loss": "1000.00",
                "max_positions": 5,
                "emergency_liquidation_enabled": False,
            },
            "advanced": {
                "max_order_quantity": 10000,
                "max_chase_bps": 30,
                "max_spread_bps": 20,
                "quote_max_age_ms": 1000,
            },
        }
    )
    key = installation_key(root)
    plans = EnginePlanControl(
        ArtifactFiles(root / "artifacts"),
        {LOCAL_FEED_KEY_ID: key.public_key()},
        node_id=config.node_id,
        account_id=config.account_id,
        node_generation=1,
        engine_boot_id="feed-test-boot",
        policies=config.policies,
        rulebook=config.rulebook,
        clock=lambda: now,
        pause_for_change=lambda: None,
        tail_phases=True,
        local_tail_history=root / "critical/tail-feed-phase-plans.json",
    )
    return LocalTailFeed(config, settings, plans, key)


class EnginePeer:
    def __init__(self, feed: LocalTailFeed, now: datetime = NOW) -> None:
        self.feed, self.now = feed, now
        self.uncertain = False

    def invoke(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if action == "LOAD_TAIL_PICKS":
            assert payload is not None
            self.feed.accept(
                ArtifactFiles(self.feed.config.data_root / "artifacts").read(
                    payload["artifact_hash"]
                ),
                self.now,
            )
            if self.uncertain:
                self.uncertain = False
                raise IPCError("reply lost after durable handoff")
        elif action == "TAIL_FEED_FAULT":
            self.feed.fault()
        else:
            assert action == "TAIL_FEED_STATUS"
        return {
            "trade_date": self.now.date().isoformat(),
            "all_ready": self.feed.complete,
            "suspended": self.feed.suspended,
        }


def test_all_three_including_empty_batches_stop_http_and_survive_restart(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    count = 0

    def fetch(_: datetime) -> bytes:
        nonlocal count
        count += 1
        return json.dumps(wire()).encode()

    peer = EnginePeer(feed)
    puller = TailFeedPuller(feed.config, cast(RPCClient, peer), lambda: NOW, fetch=fetch)
    assert puller.run()["status"] == "COMPLETE"
    assert count == 1 and feed.complete
    assert feed.allows_buy(NOW + timedelta(seconds=60))
    assert puller.run()["status"] == "SKIPPED"
    assert count == 1
    restored = local_feed(tmp_path)
    restored.restore(NOW)
    second = TailFeedPuller(
        restored.config, cast(RPCClient, EnginePeer(restored)), lambda: NOW, fetch=fetch
    )
    assert second.run()["status"] == "COMPLETE"
    assert count == 1
    assert len(restored.plans.plans.accepted()) == 3


def test_uncertain_final_handoff_rechecks_engine_without_fetching_again(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    peer = EnginePeer(feed)
    peer.uncertain = True
    count = 0
    seconds = [0.0]

    def fetch(_: datetime) -> bytes:
        nonlocal count
        count += 1
        return json.dumps(wire()).encode()

    puller = TailFeedPuller(
        feed.config, cast(RPCClient, peer), lambda: NOW, fetch=fetch, seconds=lambda: seconds[0]
    )
    with pytest.raises(IPCError):
        puller.run()
    seconds[0] += 5
    assert puller.run()["status"] == "COMPLETE"
    assert count == 1


def test_partial_snapshot_goes_stale_and_late_earlier_batch_can_append(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    feed.accept(json.dumps(wire(ready=("1450",))).encode(), NOW)
    assert {"complete": feed.complete} == {"complete": False}
    assert feed.allows_buy(NOW)
    assert not feed.allows_buy(NOW + timedelta(seconds=16))
    feed.accept(json.dumps(wire()).encode(), NOW)
    assert feed.complete
    assert {p.payload.source.decision_phase for p in feed.plans.plans.accepted()} == {
        "1440",
        "1445",
        "1450",
    }


def test_local_contexts_use_only_installed_budget_account_and_private_key(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    value = wire()
    value["batches"][0]["stocks"] = [{"code": "000001.SZ", "name": "平安银行", "rank": 1}]
    feed.accept(json.dumps(value).encode(), NOW)
    plan = feed.plans.plans.accepted()[0]
    checked = verify_plan(
        canonical_bytes(plan.model_dump(mode="json")), {LOCAL_FEED_KEY_ID: feed.key.public_key()}
    )
    assert checked.payload.account_id == feed.config.account_id
    assert checked.payload.allocations[0].max_value == "10000.00"
    assert checked.payload.risk_budget.max_buy_value == "20000.00"
    assert checked.payload.offline_authorization.directions == ("BUY",)
    with pytest.raises(ValueError):
        verify_plan(canonical_bytes(plan.model_dump(mode="json")), feed.config.public_keys())


def test_changed_ready_batch_latches_suspension_across_restart(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    feed.accept(json.dumps(wire(ready=("1440",))).encode(), NOW)
    changed = wire()
    changed["batches"][0]["source_run_id"] = "different-source"
    with pytest.raises(ValueError, match="changed"):
        feed.accept(json.dumps(changed).encode(), NOW)
    assert not feed.allows_buy(NOW)
    restored = local_feed(tmp_path)
    restored.restore(NOW)
    assert restored.suspended and not restored.allows_buy(NOW)


def test_failed_persistence_never_reports_day_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed = local_feed(tmp_path)

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(feed, "_save", fail)
    with pytest.raises(OSError):
        feed.accept(json.dumps(wire()).encode(), NOW)
    assert not feed.complete and not feed.allows_buy(NOW)


def test_restart_rejects_corrupt_snapshot_missing_key_or_changed_settings(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    feed.accept(json.dumps(wire()).encode(), NOW)
    body = feed.path.read_bytes()
    feed.path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        local_feed(tmp_path).restore(NOW)
    feed.path.write_bytes(body)
    changed = local_feed(tmp_path)
    changed.scope = "changed-settings"
    with pytest.raises(ValueError, match="scope"):
        changed.restore(NOW)
    (tmp_path / "critical/tail-feed-local-key.bin").unlink()
    with pytest.raises(ValueError, match="missing"):
        installation_key(tmp_path)


def test_next_trading_day_does_not_reuse_complete_marker(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    feed.accept(json.dumps(wire()).encode(), NOW)
    next_day = NOW + timedelta(days=3)
    restored = local_feed(tmp_path, next_day)
    restored.restore(next_day)
    assert not restored.complete
    assert not restored.allows_buy(next_day)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(schema_version=True),
        lambda v: v.update(schema_version="1"),
        lambda v: v["batches"].pop(),
        lambda v: v["batches"].reverse(),
        lambda v: v["batches"][0].update(source_run_id=None),
        lambda v: v["batches"][0].update(completed_at="2026-10-23T14:39:00+08:00"),
        lambda v: v["batches"][0].update(completed_at="2026-10-23T15:00:00+08:00"),
        lambda v: v.update(server_time="2026-10-23T06:51:00Z"),
        lambda v: v["batches"][0].update(extra=True),
        lambda v: v["batches"][0].update(stocks=[{"code": "000001.SZ", "name": None, "rank": 2}]),
        lambda v: v["batches"][0].update(stocks=[{"code": "000001.sz", "name": None, "rank": 1}]),
        lambda v: v["batches"][0].update(stocks=[{"code": "000001.SZ", "name": " ", "rank": 1}]),
    ],
)
def test_strict_wire_and_semantic_checks(change: Callable[[dict[str, Any]], object]) -> None:
    value = wire()
    change(value)
    with pytest.raises(ValueError):
        TailPicks.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "UNAUTHORIZED", False),
        (503, "FEED_DISABLED", False),
        (503, "FEED_SUSPENDED", False),
        (429, "RATE_LIMITED", True),
        (503, "SERVICE_UNAVAILABLE", True),
    ],
)
def test_transport_error_contract(status: int, code: str, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "account_id" not in request.url.query.decode()
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            status,
            headers={"Retry-After": "7"},
            json={
                "schema_version": 1,
                "error": {"code": code, "message": code, "retryable": retryable},
            },
        )

    client = TailFeedHttp(
        "https://quant.example.invalid", b"x" * 43, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(FeedRequestError) as raised:
            client.get(NOW)
        assert raised.value.retryable is retryable and raised.value.delay == 7
    finally:
        client.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_transport_validates_success_and_bounds_size(scheme: str) -> None:
    client = TailFeedHttp(
        f"{scheme}://quant.example.invalid:8001",
        b"x" * 43,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wire())),
    )
    try:
        assert TailPicks.model_validate_json(client.get(NOW)).all_ready
        client.close()
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b" " * 65537))
        )
        with pytest.raises(FeedRequestError, match="TOO_LARGE"):
            client.get(NOW)
    finally:
        client.close()


def endpoint_for(feed: LocalTailFeed) -> tuple[EngineEndpoint, list[str]]:
    from auto_trading_system.runtime.node_engine import EngineEndpoint

    endpoint = object.__new__(EngineEndpoint)
    halted: list[str] = []
    peer: Any = SimpleNamespace(
        local_tail_feed=feed,
        trade_date=NOW.date(),
        emergency_halt=halted.append,
    )
    endpoint.config = feed.config
    endpoint.engine = peer
    endpoint.clock = lambda: NOW
    endpoint.files = ArtifactFiles(feed.config.data_root / "artifacts")
    endpoint.mailbox = cast(Any, SimpleNamespace(call=lambda operation, **_: operation()))
    return endpoint, halted


def rpc_request(action: str, payload: dict[str, Any] | None = None) -> Request:
    return Request(
        request_id="feed-test-" + str(time_ns()),
        engine_boot_id="feed-test-boot",
        sender_role="agent",
        deadline_ms=time_ns() // 1_000_000 + 4000,
        action=action,
        payload=payload or {},
    )


def test_engine_endpoint_durable_ack_status_fault_and_legacy_fence(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    endpoint, halted = endpoint_for(feed)
    assert endpoint.handle(rpc_request("TAIL_FEED_STATUS"))["all_ready"] is False
    artifact = endpoint.files.put(json.dumps(wire()).encode())
    assert (
        endpoint.handle(rpc_request("LOAD_TAIL_PICKS", {"artifact_hash": artifact}))["all_ready"]
        is True
    )
    assert not halted
    with pytest.raises(IPCError, match="DISABLED"):
        endpoint.handle(rpc_request("LOAD_PLAN"))
    with pytest.raises(IPCError, match="INVALID_ARGUMENTS"):
        endpoint.handle(rpc_request("LOAD_TAIL_PICKS", {"unexpected": True}))
    endpoint.handle(rpc_request("TAIL_FEED_FAULT"))
    assert halted == ["TAIL_FEED_SUSPENDED"]
    assert endpoint.handle(rpc_request("TAIL_FEED_STATUS"))["suspended"] is True
    with pytest.raises(IPCError, match="INVALID_ARGUMENTS"):
        endpoint.handle(rpc_request("TAIL_FEED_STATUS", {"unexpected": True}))
    endpoint.config = endpoint.config.model_copy(update={"tail_picks": None})
    with pytest.raises(IPCError, match="NOT_CONFIGURED"):
        endpoint.handle(rpc_request("TAIL_FEED_STATUS"))


@pytest.mark.parametrize("failure", ["validation", "disk", "busy"])
def test_engine_endpoint_failure_never_acknowledges_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    feed = local_feed(tmp_path)
    endpoint, halted = endpoint_for(feed)
    artifact = endpoint.files.put(json.dumps(wire()).encode())

    def fail(*_: object) -> None:
        if failure == "disk":
            raise OSError("disk full")
        raise ValueError("control persistence is pending" if failure == "busy" else "bad feed")

    monkeypatch.setattr(feed, "accept", fail)
    with pytest.raises((ValueError, OSError, IPCError)):
        endpoint.handle(rpc_request("LOAD_TAIL_PICKS", {"artifact_hash": artifact}))
    assert not feed.complete
    if failure == "busy":
        assert not halted and not feed.suspended
    else:
        assert halted


def test_poller_partial_failure_backoff_and_permanent_failure_stop(tmp_path: Path) -> None:
    feed = local_feed(tmp_path)
    seconds = [0.0]
    attempt = [0]

    def fetch(_: datetime) -> bytes:
        attempt[0] += 1
        if attempt[0] == 1:
            raise FeedRequestError("SERVICE_UNAVAILABLE", retryable=True, delay=7)
        if attempt[0] == 2:
            return json.dumps(wire(ready=("1440",))).encode()
        raise FeedRequestError("FEED_DISABLED", retryable=False)

    puller = TailFeedPuller(
        feed.config,
        cast(RPCClient, EnginePeer(feed)),
        lambda: NOW,
        fetch=fetch,
        seconds=lambda: seconds[0],
    )
    assert puller.run()["status"] == "RETRY"
    assert puller.next_due == 7
    seconds[0] = 7
    assert puller.run()["status"] == "WAITING_PHASES"
    seconds[0] += 5
    assert puller.run()["status"] == "SUSPENDED"
    assert feed.suspended and puller.run()["status"] == "SKIPPED"


def test_agent_owns_single_flight_feed_and_does_not_use_old_remote_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading_system.sync.node_agent import AgentEndpoint, AgentWorker

    feed = local_feed(tmp_path)
    worker = AgentWorker(feed.config, cast(RPCClient, EnginePeer(feed)), clock=lambda: NOW)
    assert worker.run("pull-plan")["reason"] == "SIMPLE_TAIL_FEED_ENABLED"
    assert worker.run("daily-export")["reason"] == "SIMPLE_FEED_LOCAL_HISTORY_ONLY"
    assert worker.tail_feed is not None
    worker.tail_feed.fetch = lambda _: json.dumps(wire()).encode()
    assert worker.run("pull-tail-picks")["status"] == "COMPLETE"

    worker.tail_feed.stopped_day = None
    worker.tail_feed.next_due = 0
    entered, release = Event(), Event()
    calls: list[str] = []

    def run_once(kind: str) -> dict[str, Any]:
        calls.append(kind)
        entered.set()
        assert release.wait(2)
        assert worker.tail_feed is not None
        worker.tail_feed.stopped_day = NOW.date().isoformat()
        return {"status": "COMPLETE"}

    monkeypatch.setattr(worker, "run", run_once)
    endpoint = AgentEndpoint(worker, Event(), "agent-feed-test")
    try:
        endpoint.pump_tail_feed()
        assert entered.wait(1)
        for _ in range(10):
            endpoint.pump_tail_feed()
        assert calls == ["pull-tail-picks"]
        release.set()
    finally:
        release.set()
        endpoint.close()


def test_real_transport_creation_uses_separate_token_and_closes_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import auto_trading_system.sync.tail_feed as module

    feed = local_feed(tmp_path)
    seen: list[str] = []

    class Transport:
        def __init__(self, origin: str, token: bytes, **_: object) -> None:
            assert origin == feed.config.quant_origin and token == b"x" * 43

        def get(self, now: datetime) -> bytes:
            assert now == NOW
            return json.dumps(wire()).encode()

        def close(self) -> None:
            seen.append("closed")

    monkeypatch.setattr(module, "TailFeedHttp", Transport)
    monkeypatch.setattr(module, "resolve_secret", lambda _: b"x" * 43)
    monkeypatch.setattr(module, "role_keys", lambda *_: {"agent": b"different"})
    puller = TailFeedPuller(feed.config, cast(RPCClient, EnginePeer(feed)), lambda: NOW)
    assert puller.run()["status"] == "COMPLETE" and seen == ["closed"]
    second = local_feed(tmp_path / "other")
    monkeypatch.setattr(module, "role_keys", lambda *_: {"agent": b"x" * 43})
    rejected = TailFeedPuller(second.config, cast(RPCClient, EnginePeer(second)), lambda: NOW)
    assert rejected.run()["status"] == "SUSPENDED"


@pytest.mark.parametrize(
    "origin,token",
    [
        ("ftp://quant.test", b"x" * 43),
        ("https://user:password@quant.test", b"x" * 43),
        ("https://quant.test/path", b"x" * 43),
        ("https://quant.test", b"short"),
    ],
)
def test_transport_rejects_insecure_origin_or_invalid_token(origin: str, token: bytes) -> None:
    with pytest.raises(ValueError):
        TailFeedHttp(origin, token)


def test_local_private_files_reject_oversize_and_invalid_retained_key(tmp_path: Path) -> None:
    from auto_trading_system.runtime.tail_feed import read_private

    file = tmp_path / "oversize"
    file.write_bytes(b"xx")
    with pytest.raises(ValueError, match="budget"):
        read_private(file, 1)
    with pytest.raises(ValueError, match="regular"):
        read_private(tmp_path, 4096)
    (tmp_path / "critical").mkdir()
    (tmp_path / "critical/tail-feed-local-key.bin").write_bytes(b"bad")
    with pytest.raises(ValueError, match="key"):
        installation_key(tmp_path)
