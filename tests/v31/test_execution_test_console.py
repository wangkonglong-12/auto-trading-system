"""Test-only console flow: real feed model, durable queue, reducer and exact receipts."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from threading import Event as Signal
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from test_tail_feed import NOW
from test_tail_feed_preview import preview_wire

from auto_trading_system.apps.local_console import execution_test as module
from auto_trading_system.protocol.executions.models import Batch, body_hash
from auto_trading_system.protocol.executions.projection import State, apply_event, summary
from auto_trading_system.protocol.tail_picks import TailPicksPreview
from auto_trading_system.sync import execution_test_transport as transport_module
from auto_trading_system.sync.execution_test_scenarios import scenario


class FixedClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> FixedClock:
        return cls(2026, 10, 23, 14, 51, tzinfo=NOW.tzinfo)


class Remote:
    def __init__(self) -> None:
        self.preview = preview_wire()
        self.states: dict[str, State] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.calls: list[str] = []
        self.fail = False
        self.bad_read = False
        self.bad_receipt = False
        self.sent: list[Batch] = []

    def call(self, operation: str, payload: dict[str, object]) -> bytes:
        self.calls.append(operation)
        if self.fail:
            raise OSError("private transport detail must never appear in UI")
        if operation == "PREVIEW":
            return json.dumps(self.preview).encode()
        if operation == "SEND":
            data = Batch.model_validate_json(json.dumps(payload))
            self.sent.append(data)
            receipts = []
            for event in data.events:
                key = event.identity.execution_id
                known = self.hashes.setdefault(key, {})
                hashed = body_hash(event)
                if event.event_id not in known:
                    self.states[key] = apply_event(
                        self.states.get(key, State(identity=event.identity)), event
                    )
                    known[event.event_id] = hashed
                assert known[event.event_id] == hashed
                receipts.append({"event_id": event.event_id, "event_hash": hashed})
            if self.bad_receipt:
                receipts = []
            return json.dumps({"schema_version": 1, "acknowledgments": receipts}).encode()
        assert operation == "READ"
        ids = payload["ids"]
        assert isinstance(ids, list)
        readback = [
            {
                "execution": summary(self.states[key]).model_dump(mode="json"),
                "event_hashes": self.hashes[key],
            }
            for key in ids
        ]
        if self.bad_read:
            readback[0]["event_hashes"] = {}
        return json.dumps(readback).encode()


@pytest.fixture
def connection(tmp_path: Path) -> transport_module.TestConnection:
    return transport_module.TestConnection(
        host="example.test",
        user="tester",
        private_key=tmp_path / "key",
        known_hosts=tmp_path / "hosts",
        python="/venv/bin/python",
        script="/scripts/test.py",
        node_id="operator-console-test",
        account_id="operator-console-test",
        strategy_id="TAIL_CLOSE",
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "datetime", FixedClock)
    monkeypatch.setattr(module, "sleep", lambda _: None)


def finish(service: module.ExecutionTestService) -> dict[str, Any]:
    service._pool.submit(lambda: None).result(timeout=5)
    return service.snapshot()


def run(service: module.ExecutionTestService, action: Any) -> dict[str, Any]:
    service.submit(action, maximum=1, amount="2000.00")
    return finish(service)


def test_fetch_shows_unique_real_stock_before_simulation_and_durable_full_roundtrip(
    tmp_path: Path, connection: Any
) -> None:
    remote = Remote()
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    ready = run(service, "FETCH")
    assert ready["state"] == "READY" and ready["selection_date"] == "2026-10-19"
    assert len(ready["rows"]) == 1 and ready["rows"][0]["symbol"] == "000001.SZ"
    assert ready["rows"][0]["execution"] is None and remote.calls == ["PREVIEW"]
    result = run(service, "RUN")
    assert result["state"] == "SYNCED" and result["executable"] is False
    row = result["rows"][0]
    assert row["execution"]["identity"]["environment"] == "TEST"
    assert row["execution"]["bought_quantity"] == row["execution"]["sold_quantity"] == 200
    assert row["execution"]["stage"] == "CLOSED" and row["execution"]["buy_amount"] == "2000.00"
    assert len([e for e in row["events"] if e["fact"]["kind"] == "CANDIDATE"]) == 3
    assert {"TEST_BUY_REJECTED", "TEST_SELL_REJECTED", "TEST_CANCEL_FAILED"} <= {
        e["fact"].get("reason_code") for e in row["events"]
    }
    assert remote.sent[0] == remote.sent[1]
    hashes = dict(remote.hashes[next(iter(remote.hashes))])
    service.close()
    restored = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    assert finish(restored)["state"] == "SYNCED"
    assert run(restored, "FETCH")["state"] == "SYNCED"
    assert run(restored, "RUN")["state"] == "SYNCED"
    assert hashes == remote.hashes[next(iter(remote.hashes))]
    restored.close()
    assert not (tmp_path / "critical").exists() and not (tmp_path / "jobs").exists()


def test_lost_receipt_and_restart_retry_preserves_original_events(
    tmp_path: Path, connection: Any
) -> None:
    remote = Remote()
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    run(service, "FETCH")
    remote.bad_receipt = True
    failed = run(service, "RUN")
    assert failed["state"] == "ERROR" and failed["error"] == "TEST_REQUEST_FAILED"
    events = failed["rows"][0]["events"]
    assert run(service, "FETCH")["error"] == "TEST_PENDING_UPLOAD"
    service.close()
    remote.bad_receipt = False
    restored = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    assert run(restored, "RETRY")["state"] == "SYNCED"
    assert cast(dict[str, Any], restored.snapshot())["rows"][0]["events"] == events
    restored.close()


@pytest.mark.parametrize(
    "action,error", [("RUN", "TEST_FETCH_REQUIRED"), ("RETRY", "TEST_FETCH_REQUIRED")]
)
def test_no_simulation_without_real_picks(
    tmp_path: Path, connection: Any, action: str, error: str
) -> None:
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=Remote())
    assert run(service, action)["error"] == error
    service.close()


def test_empty_invalid_changed_feed_and_server_readback_mismatch(
    tmp_path: Path, connection: Any
) -> None:
    remote = Remote()
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    remote.preview.update(status="NOT_FOUND", trade_date=None, batches=[])
    assert run(service, "FETCH")["state"] == "EMPTY"
    remote.preview = preview_wire()
    remote.preview["server_time"] = "2026-10-23T12:00:00+08:00"
    assert run(service, "FETCH")["state"] == "ERROR"
    remote.preview = preview_wire()
    assert run(service, "FETCH")["state"] == "READY"
    assert run(service, "RETRY")["error"] == "TEST_RUN_REQUIRED"
    remote.preview["batches"][0]["stocks"][0]["name"] = "changed"
    assert run(service, "FETCH")["state"] == "ERROR"
    remote.bad_read = True
    assert run(service, "RUN")["state"] == "ERROR"
    remote.bad_read = False
    assert run(service, "RETRY")["state"] == "SYNCED"
    service.close()


def test_scope_change_corrupt_storage_and_unconfigured_connection(
    tmp_path: Path, connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "load_connection", lambda: None)
    service = module.ExecutionTestService(tmp_path)
    assert run(service, "FETCH")["error"] == "TEST_CONNECTION_REQUIRED"
    service.close()
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=Remote())
    run(service, "FETCH")
    run(service, "RUN")
    service.close()
    changed = connection.model_copy(update={"account_id": "another-test"})
    service = module.ExecutionTestService(tmp_path, connection=changed, transport=Remote())
    assert run(service, "RETRY")["state"] == "ERROR"
    service.close()
    (tmp_path / "execution-test/active.json").write_text('{"date":"../bad"}')
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=Remote())
    assert finish(service)["error"] == "TEST_STORAGE_INVALID"
    service.close()


def test_single_flight_and_close_fence(tmp_path: Path, connection: Any) -> None:
    started, release = Signal(), Signal()
    remote = Remote()
    call = remote.call

    def blocking(operation: str, payload: dict[str, object]) -> bytes:
        started.set()
        assert release.wait(5)
        return call(operation, payload)

    remote.call = blocking  # type: ignore[method-assign]
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    service.submit("FETCH", maximum=1, amount="2000.00")
    assert started.wait(5) and service.snapshot()["busy"] is True
    with pytest.raises(ValueError):
        service.submit("RUN", maximum=1, amount="2000.00")
    other_root = tmp_path / "other-console"
    other_root.mkdir()
    other = module.ExecutionTestService(other_root, connection=connection, transport=remote)
    assert run(other, "FETCH")["state"] == "ERROR"
    assert remote.calls == []
    other.close()
    release.set()
    finish(service)
    service.close()
    with pytest.raises(ValueError):
        service.submit("RUN", maximum=1, amount="2000.00")


def test_shared_storage_survives_profile_change(tmp_path: Path, connection: Any) -> None:
    root = tmp_path / "shared"
    connection = connection.model_copy(update={"storage_root": root})
    remote = Remote()
    first = module.ExecutionTestService(tmp_path, connection=connection, transport=remote)
    assert run(first, "FETCH")["state"] == "READY"
    assert run(first, "RUN")["state"] == "SYNCED", repr(first._error)
    original = first.snapshot()
    first.close()
    other = tmp_path / "another-node"
    other.mkdir()
    second = module.ExecutionTestService(other, connection=connection, transport=remote)
    assert run(second, "FETCH")["state"] == "SYNCED"
    assert second.snapshot()["rows"] == original["rows"]
    assert run(second, "RETRY")["state"] == "SYNCED"
    second.close()


@pytest.mark.parametrize(
    "maximum,amount,expected",
    [(1, "2000.00", "FILLED"), (1, "999.00", "SKIPPED"), (1, "1000.00", "FILLED")],
)
def test_scenario_limits_and_real_names(maximum: int, amount: str, expected: str) -> None:
    preview = preview_wire()
    for b in preview["batches"]:
        b["stocks"].append({"code": "000002.SZ", "name": "万科A", "rank": 2})
    feed = TailPicksPreview.model_validate_json(json.dumps(preview))
    events = scenario(
        feed,
        node_account="operator-console-test",
        strategy="TAIL_CLOSE",
        maximum=maximum,
        amount=amount,
        now=NOW,
    )
    states: dict[str, State] = {}
    for event in events:
        states[event.identity.symbol] = apply_event(
            states.get(event.identity.symbol, State(identity=event.identity)), event
        )
    assert summary(states["000001.SZ"]).buy_status == expected
    assert summary(states["000002.SZ"]).buy_status == "SKIPPED"
    assert states["000002.SZ"].name == "万科A"
    empty = feed.model_copy(update={"status": "NOT_FOUND", "trade_date": None, "batches": ()})
    with pytest.raises(ValueError):
        scenario(empty, node_account="a", strategy="s", maximum=1, amount="1000.00", now=NOW)


def test_ssh_transport_host_verification_no_shell_and_bounded_failures(
    tmp_path: Path, connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection.private_key.write_text("not-a-private-key")
    connection.known_hosts.write_text("test-placeholder")
    calls = []

    def execute(args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"status":"OK","data":{"ok":true}}')

    monkeypatch.setattr(subprocess, "run", execute)
    client = transport_module.SshTestTransport(connection)
    assert json.loads(client.call("PREVIEW", {})) == {"ok": True}
    args, kwargs = calls[0]
    assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
    assert kwargs["timeout"] == 25 and "shell" not in kwargs and "password" not in kwargs
    with pytest.raises(ValueError):
        client.call("ORDER", {})
    with pytest.raises(ValueError):
        client.call("SEND", {"oversize": "x" * 65536})
    for output, code in [(b'{"status":"ERROR"}', 0), (b"{}", 1), (b"x" * 1048577, 0)]:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, output=output, code=code, **k: SimpleNamespace(
                returncode=code, stdout=output
            ),
        )
        with pytest.raises(ValueError):
            client.call("READ", {"ids": []})
    connection.private_key.unlink()
    with pytest.raises(ValueError):
        client.call("PREVIEW", {})


def test_private_connection_parser_and_snapshot_copy(
    tmp_path: Path, connection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "connection.json"
    monkeypatch.setattr(transport_module, "connection_path", lambda: path)
    assert transport_module.load_connection() is None
    path.write_text(connection.model_dump_json())
    assert transport_module.load_connection() == connection
    path.write_text("{}")
    with pytest.raises(ValueError):
        transport_module.load_connection()
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=Remote())
    run(service, "FETCH")
    value = service.snapshot()
    value["rows"] = []
    assert len(cast(dict[str, Any], service.snapshot())["rows"]) == 1
    service.close()


@pytest.mark.parametrize("shared", [False, True])
def test_http_transport_uses_configured_origin_tokens_and_never_ssh(
    connection: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    from auto_trading_system.config.models import EnvironmentSecretReference

    remote = Remote()
    monkeypatch.setattr(
        transport_module, "read_access_token", lambda _: b"s" * 43 if shared else None
    )
    monkeypatch.setenv("ATS_SECRET_TEST_READ", "r" * 43)
    monkeypatch.setenv("ATS_SECRET_TEST_UPLOAD", "u" * 43)
    original_scope = connection.storage_scope()
    connection = connection.model_copy(
        update={
            "read_token": EnvironmentSecretReference(
                source="ENVIRONMENT", variable="ATS_SECRET_TEST_READ"
            ),
            "upload_token": EnvironmentSecretReference(
                source="ENVIRONMENT", variable="ATS_SECRET_TEST_UPLOAD"
            ),
        }
    )
    assert connection.storage_scope() == original_scope
    calls = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "http" and request.url.host == "backend.example.com"
        assert request.url.port == 8001
        calls.append(request)
        if request.url.path.endswith("tail-picks"):
            assert request.method == "GET" and request.url.params["test"] == "true"
            assert request.headers["authorization"] == "Bearer " + ("s" if shared else "r") * 43
            operation, value = "PREVIEW", {}
        else:
            assert (
                request.method == "POST"
                and request.headers["authorization"] == "Bearer " + ("s" if shared else "u") * 43
            )
            operation = "SEND" if request.url.path.endswith("execution-events") else "READ"
            value = json.loads(request.content)
        return httpx.Response(200, content=remote.call(operation, value))

    def no_ssh(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SSH must never run for HTTP tests")

    monkeypatch.setattr(subprocess, "run", no_ssh)
    transport = transport_module.HttpTestTransport(
        "http://backend.example.com:8001",
        connection,
        transport=httpx.MockTransport(answer),
        access_root=tmp_path,
    )
    service = module.ExecutionTestService(tmp_path, connection=connection, transport=transport)
    run(service, "FETCH")
    assert service.snapshot()["state"] == "READY"
    run(service, "RUN")
    assert service.snapshot()["state"] == "SYNCED", repr(service._error)
    assert {r.url.path for r in calls} == {
        "/api/auto-trading/tail-picks",
        "/api/auto-trading/execution-events",
        "/api/auto-trading/execution-test/readback",
    }
    service.close()
    for operation, value in [
        ("SEND", {"environment": "REAL"}),
        ("ORDER", {}),
        ("READ", {"ids": []}),
    ]:
        with pytest.raises(ValueError):
            transport.call(operation, value)
    for status in (301, 401, 403, 500):
        rejected = transport_module.HttpTestTransport(
            "http://backend.example.com:8001",
            connection,
            transport=httpx.MockTransport(
                lambda _, status=status: httpx.Response(
                    status, headers={"Location": "http://elsewhere.test"}
                )
            ),
        )
        with pytest.raises(ValueError):
            rejected.call("PREVIEW", {})
    missing = transport_module.HttpTestTransport(
        "http://backend.example.com:8001", connection.model_copy(update={"read_token": None})
    )
    with pytest.raises(ValueError, match="TEST_HTTP_CREDENTIALS_REQUIRED"):
        missing.call("PREVIEW", {})
