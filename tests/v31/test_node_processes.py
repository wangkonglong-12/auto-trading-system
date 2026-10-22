"""Real Windows pipe/process coverage plus deterministic protocol and task cases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_hex
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import pywintypes
import win32con
import win32file
import win32pipe
import win32security
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.apps.node_process.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.config.node_v2 import (
    NodeProcessConfig,
    load_node_config,
    resolve_secret,
    role_keys,
)
from auto_trading_system.control.ipc_protocol import (
    IPCError,
    Request,
    Role,
    RPCClient,
    RPCService,
    sign_packet,
    verify_packet,
)
from auto_trading_system.control.owner_mailbox import OwnerMailbox
from auto_trading_system.control.windows_pipe import PipeServer, current_sid, exchange
from auto_trading_system.orders.projection import OrderProjection
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope, sign_plan
from auto_trading_system.runtime.node_engine import run_engine
from auto_trading_system.runtime.node_layout import initialize_layout, validate_layout
from auto_trading_system.runtime.node_tasks import NS, task_plan
from auto_trading_system.runtime.observation_v2 import check_empty_observation
from auto_trading_system.runtime.recovery_v2 import (
    AccountFact,
    BrokerRecoverySnapshot,
    PositionFact,
)
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_catalog import DailyCatalog
from auto_trading_system.sync.http_v2 import HttpQuantTransport, NodeCredentials, SignedHttpClient
from auto_trading_system.sync.node_agent import (
    AgentEndpoint,
    AgentWorker,
    EnginePlanClient,
    run_agent,
)
from auto_trading_system.testing.business_remote import BusinessRemote
from auto_trading_system.testing.v31_scenarios import example_plan

KEY = b"ephemeral-test-ipc-key-000000000000"
NOW = 1000000.0


def request(action: str = "STATUS", **updates: Any) -> Request:
    values: dict[str, JsonValue] = {
        "request_id": uuid4().hex,
        "engine_boot_id": "boot",
        "sender_role": "agent",
        "deadline_ms": int(NOW * 1000) + 7000,
        "action": action,
        "payload": {},
    }
    values.update(updates)
    return Request.model_validate_json(canonical_bytes(values))


def service(
    handler: Callable[[Request], dict[str, JsonValue]] = lambda _: {"paused": True},
    *,
    capacity: int = 512,
) -> RPCService:
    return RPCService(
        "boot",
        {"agent": KEY},
        {"agent": frozenset({"HELLO", "STATUS"})},
        handler,
        clock=lambda: NOW,
        capacity=capacity,
    )


def encoded(value: Request) -> bytes:
    return sign_packet(value.model_dump(mode="json"), KEY)


@pytest.fixture
def node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[NodeProcessConfig, Path, PlanEnvelope]:
    private = Ed25519PrivateKey.generate()
    account = "paper-" + uuid4().hex
    plan = sign_plan(example_plan().model_copy(update={"account_id": account}), "test-key", private)
    for name in ("AGENT", "JOBRUNNER", "OPERATOR", "HTTP"):
        monkeypatch.setenv("ATS_SECRET_NODE_" + name, token_hex(32))

    def reference(name: str) -> dict[str, JsonValue]:
        return {"source": "ENVIRONMENT", "variable": "ATS_SECRET_NODE_" + name}

    raw: dict[str, JsonValue] = {
        "schema_major": 2,
        "broker": "MOCK",
        "node_id": "node-" + uuid4().hex,
        "account_id": account,
        "node_generation": 1,
        "journal_generation": "journal-1",
        "strategy_id": plan.payload.strategy_id,
        "service_sid": current_sid(),
        "data_root": str(tmp_path / "node-data"),
        "ipc_secrets": {
            name.lower(): reference(name) for name in ("AGENT", "JOBRUNNER", "OPERATOR")
        },
        "quant_origin": "https://offline-quant.test",
        "quant_key_id": "test-http",
        "quant_secret": reference("HTTP"),
        "trusted_plan_keys": {"test-key": private.public_key().public_bytes_raw().hex()},
        "policies": [item.model_dump(mode="json") for item in plan.payload.policy_bundle],
        "rulebook": plan.payload.rulebook_version,
        "calendar": {"version": "test-calendar", "sessions": ["2026-09-07", "2026-09-08"]},
    }
    path = tmp_path / "node.json"
    path.write_bytes(canonical_bytes(raw))
    config = load_node_config(path)
    initialize_layout(config)
    return config, path, plan


def rpc(config: NodeProcessConfig, role: Role, channel: str) -> RPCClient:
    return RPCClient(
        lambda body: exchange(config.pipe_name(channel), body),
        role,
        role_keys(config, (role,))[role],
    )


@contextmanager
def running_engine(config: NodeProcessConfig, clock: Callable[[], datetime]) -> Iterator[RPCClient]:
    stop, ready = Event(), Event()
    failures: list[BaseException] = []

    def target() -> None:
        try:
            run_engine(config, stop=stop, clock=clock, ready=lambda _: ready.set())
        except BaseException as exc:
            failures.append(exc)
            ready.set()

    thread = Thread(target=target)
    thread.start()
    try:
        assert ready.wait(8) and not failures
        yield rpc(config, "agent", "engine")
    finally:
        stop.set()
        thread.join(12)
        assert not thread.is_alive()
        assert not failures


def eventually(predicate: Callable[[], bool], seconds: float = 8) -> None:
    until = monotonic() + seconds
    while monotonic() < until:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition did not become true within budget")


def last_job_status(client: RPCClient) -> JsonValue:
    value = client.invoke("STATUS")["last_result"]
    assert isinstance(value, dict)
    return value["status"]


def recovered(client: RPCClient, *, minimum_version: int = 0) -> None:
    last: dict[str, JsonValue] = {}

    def check() -> bool:
        last.update(client.invoke("STATUS"))
        version = last.get("state_version")
        # STATUS 是发布的副本；必须等本次恢复版本，不能复用上次的完成状态。
        return (
            last.get("state") == "REVIEW_REQUIRED"
            and type(version) is int
            and version >= minimum_version
        )

    try:
        eventually(check)
    except AssertionError:
        raise AssertionError(last) from None


def test_rpc_signed_handshake_idempotence_and_response_binding() -> None:
    calls: list[str] = []

    def record(req: Request) -> dict[str, JsonValue]:
        calls.append(req.request_id)
        return {"paused": True}

    server = service(record)
    client = RPCClient(server.handle, "agent", KEY, clock=lambda: NOW)
    assert client.invoke("STATUS") == {"paused": True}
    value = encoded(request())
    first = server.handle(value)
    assert server.handle(value) == first and len(calls) == 2
    assert verify_packet(first, KEY, response=True)["ok"] is True
    with pytest.raises(IPCError):
        verify_packet(first, KEY)


@pytest.mark.parametrize(
    "updates",
    [
        {"sender_role": "operator"},
        {"engine_boot_id": "old"},
        {"action": "ARM"},
        {"deadline_ms": int(NOW * 1000)},
        {"deadline_ms": int(NOW * 1000) + 8001},
    ],
)
def test_rpc_rejects_role_boot_action_and_deadline(updates: dict[str, Any]) -> None:
    with pytest.raises(IPCError):
        service().handle(encoded(request(**updates)))


@pytest.mark.parametrize(
    "body",
    [b"{}", b"null", b"[]", b'{"body":1}', b'{"body":{},"body":{}}', b"x" * 65537],
    ids=["empty", "null", "list", "integer", "duplicate", "oversize"],
)
def test_rpc_malformed_and_oversize(body: bytes) -> None:
    with pytest.raises((ValueError, IPCError)):
        service().handle(body)


@pytest.mark.parametrize("signature", ["x" * 64, "密" * 64, 1, None])
def test_rpc_invalid_signature(signature: JsonValue) -> None:
    with pytest.raises(IPCError):
        service().handle(
            canonical_bytes({"body": request().model_dump(mode="json"), "signature": signature})
        )


def test_rpc_conflict_capacity_and_error_replies() -> None:
    server = service(capacity=1)
    value = request()
    server.handle(encoded(value))
    with pytest.raises(IPCError, match="CONFLICT"):
        server.handle(encoded(value.model_copy(update={"payload": {"different": True}})))
    with pytest.raises(IPCError, match="FULL"):
        server.handle(encoded(request()))
    server.clock = lambda: NOW + 10
    newer = request(deadline_ms=int((NOW + 17) * 1000))
    assert server.handle(encoded(newer))
    for error in (IPCError("OWNER_BUSY"), ValueError("sensitive-input")):

        def fail(_: Request, error: Exception = error) -> dict[str, JsonValue]:
            raise error

        result = verify_packet(service(fail).handle(encoded(request())), KEY, response=True)
        assert result["ok"] is False and "sensitive-input" not in str(result)
    with pytest.raises(ValueError):
        service(capacity=0)
    with pytest.raises(ValueError):
        sign_packet({}, b"short")
    with pytest.raises(IPCError):
        sign_packet({"too_large": "x" * 65536}, KEY)


def test_rpc_client_rejects_wrong_response_and_late_response() -> None:
    server = service()

    def alter(data: bytes) -> bytes:
        result = verify_packet(server.handle(data), KEY, response=True)
        result["request_hash"] = digest(b"different")
        return sign_packet(result, KEY, response=True)

    with pytest.raises(IPCError, match="BINDING"):
        RPCClient(alter, "agent", KEY, clock=lambda: NOW).invoke("STATUS")
    with pytest.raises(IPCError):
        RPCClient(
            service(lambda _: (_ for _ in ()).throw(IPCError("FAILED"))).handle,
            "agent",
            KEY,
            clock=lambda: NOW,
        ).invoke("STATUS")


def test_owner_mailbox_only_owner_and_timeout_close() -> None:
    box: OwnerMailbox[int] = OwnerMailbox()
    with pytest.raises(IPCError):
        box.call(lambda: 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(box.call, lambda: 3)
        eventually(lambda: not box._queue.empty())
        box.poll()
        assert result.result() == 3
        expired = pool.submit(box.call, lambda: 9, timeout=0.01)
        with pytest.raises(IPCError):
            expired.result()
        box.poll()
        invalid = pool.submit(box.poll)
        with pytest.raises(IPCError):
            invalid.result()
        waiting = pool.submit(box.call, lambda: 4)
        eventually(lambda: not box._queue.empty())
        box.close()
        with pytest.raises(IPCError):
            waiting.result()
        with pytest.raises(IPCError):
            pool.submit(box.call, lambda: 5).result()


def test_real_pipe_acl_roundtrip_first_instance_and_partial_client() -> None:
    name = rf"\\.\pipe\AutoTrading.v2.{uuid4().hex}.test"
    server = PipeServer(name, current_sid(), lambda data: data)
    try:
        assert exchange(name, b"one") == b"one"
        with pytest.raises(pywintypes.error):
            PipeServer(name, current_sid(), lambda data: data)
        # A client that sends a partial header cannot hold the endpoint forever.
        win32pipe.WaitNamedPipe(name, 2000)
        handle = win32file.CreateFile(
            name,
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0,
            None,
            win32con.OPEN_EXISTING,
            0,
            None,
        )
        try:
            win32file.WriteFile(handle, b"\x00")
            sleep(2.1)
        finally:
            handle.Close()
        assert exchange(name, b"after-timeout") == b"after-timeout"
        assert server.healthy
    finally:
        server.close()
    with pytest.raises(ValueError):
        exchange(r"\\remote\pipe\unsafe", b"bad")


def test_node_layout_scope_acl_existing_directory_and_credentials(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path, _ = node
    validate_layout(config)
    assert main(["--config", str(path), "check"]) == 0
    with pytest.raises(FileExistsError):
        initialize_layout(config)
    wrong = config.model_copy(update={"account_id": "other"})
    with pytest.raises(ValueError):
        validate_layout(wrong)
    assert len(resolve_secret(config.ipc_secrets.agent)) >= 32
    monkeypatch.setenv("ATS_SECRET_NODE_OPERATOR", os.environ["ATS_SECRET_NODE_AGENT"])
    with pytest.raises(ValueError):
        role_keys(config, ("agent", "operator"))
    monkeypatch.setenv("ATS_SECRET_NODE_AGENT", "too-short")
    with pytest.raises(ValueError):
        resolve_secret(config.ipc_secrets.agent)
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;OICI;FA;;;{config.service_sid})(A;OICI;GR;;;WD)", win32security.SDDL_REVISION_1
    )
    win32security.SetFileSecurity(
        str(config.data_root / "logs"),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )
    with pytest.raises(ValueError, match="unapproved"):
        validate_layout(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("broker", "LIVE"),
        ("data_root", "relative"),
        ("data_root", "E:/"),
        ("quant_origin", "http://example.test"),
        ("quant_origin", "https://example.test/path"),
        ("quant_ca_file", "relative.pem"),
        ("trusted_plan_keys", {"bad": "00"}),
        ("policies", []),
        ("service_sid", "everyone"),
    ],
)
def test_node_config_rejects_unsafe_values(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope], field: str, value: JsonValue
) -> None:
    config, _, _ = node
    raw = config.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValueError):
        NodeProcessConfig.model_validate_json(canonical_bytes(raw))


def test_real_engine_plan_handoff_pause_export_and_catalog_restart(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, _, plan = node
    instant = [DEMO_NOW]
    files = ArtifactFiles(config.data_root / "artifacts")
    lookup = PlanLookup(
        account_id=config.account_id,
        strategy_id=config.strategy_id,
        trade_date=DEMO_NOW.date(),
        plan_id=plan.payload.plan_id,
        state="PUBLISHED",
        state_version=1,
        etag=digest(canonical_bytes(plan.model_dump(mode="json"))),
        plan=plan,
    )
    artifact = files.put(canonical_bytes(plan.model_dump(mode="json")))
    catalog = DailyCatalog(
        config.data_root / "daily-catalog",
        files,
        node_id=config.node_id,
        account_id=config.account_id,
        generation=1,
    )
    with running_engine(config, lambda: instant[0]) as client:
        recovered(client)
        port = EnginePlanClient(files, client)
        receipt = port.apply(lookup, artifact)
        assert receipt.status == "LOADED" and port.apply(lookup, artifact) == receipt
        assert client.invoke("STATUS")["halted"]
        with pytest.raises(IPCError):
            client.invoke("ARM")
        with pytest.raises(IPCError):
            client.invoke("RECOVER_DAILY")
        instant[0] = DEMO_NOW.replace(hour=15, minute=31)
        requested = client.invoke("RECOVER_DAILY")
        version = requested["state_version"]
        assert type(version) is int
        recovered(client, minimum_version=version)
        result = client.invoke("EXPORT_DAILY")
        assert client.invoke("EXPORT_DAILY") == result
        batch = catalog.get(DEMO_NOW.date())
        assert batch and batch.artifact_hash == result["artifact_hash"]
        assert tuple(catalog.discover()) == (batch,)
        assert catalog.publish(batch) == batch
        assert rpc(config, "operator", "control").invoke("HALT")["halt_latched"]
        boot = client.invoke("STATUS")["boot_id"]
    with running_engine(config, lambda: instant[0]) as restarted:
        state = restarted.invoke("STATUS")
        # Restart restores signed ownership, never the former trading authority.
        assert state["boot_id"] != boot and state["halted"] and state["plan_loaded"]
        assert restarted.invoke("EXPORT_DAILY") == result


def test_agent_live_pipe_jobs_signed_http_and_restart_discovery(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, _, plan = node
    instant = [DEMO_NOW]
    credentials = NodeCredentials(
        config.node_id, config.quant_key_id, resolve_secret(config.quant_secret)
    )
    remote = BusinessRemote(plan, credentials, lambda: instant[0].timestamp())
    http = SignedHttpClient(
        config.quant_origin,
        credentials,
        transport=httpx.MockTransport(remote.handle),
        clock=lambda: instant[0].timestamp(),
    )
    with running_engine(config, lambda: instant[0]) as engine:
        recovered(engine)
        stop, ready = Event(), Event()
        failures: list[BaseException] = []

        def target() -> None:
            try:
                run_agent(
                    config,
                    stop=stop,
                    clock=lambda: instant[0],
                    remote=HttpQuantTransport(http),
                    ready=lambda _: ready.set(),
                )
            except BaseException as exc:
                failures.append(exc)
                ready.set()

        thread = Thread(target=target)
        thread.start()
        try:
            assert ready.wait(8) and not failures
            job = rpc(config, "jobrunner", "agent")
            eventually(lambda: job.invoke("STATUS")["status"] == "IDLE")
            job.invoke("RUN_JOB", {"kind": "pull-plan"})
            eventually(lambda: job.invoke("STATUS")["status"] == "IDLE")
            assert last_job_status(job) == "SYNCED"
            eventually(lambda: engine.invoke("STATUS")["plan_loaded"] is True)
            instant[0] = DEMO_NOW.replace(hour=15, minute=31)
            remote.drop_complete_once = True
            job.invoke("RUN_JOB", {"kind": "daily-export"})
            eventually(lambda: job.invoke("STATUS")["status"] == "IDLE")
            assert last_job_status(job) == "PENDING"
            job.invoke("RUN_JOB", {"kind": "catch-up"})
            eventually(lambda: job.invoke("STATUS")["status"] == "IDLE")
            assert last_job_status(job) == "SYNCED"
            assert engine.invoke("STATUS")["halted"]
            assert main(["--config", str(node[1]), "job", "catch-up"]) == 0
            with pytest.raises(IPCError):
                job.invoke("STOP")
            assert main(["--config", str(node[1]), "status", "agent"]) == 0
            assert main(["--config", str(node[1]), "stop", "agent"]) == 0
        finally:
            stop.set()
            thread.join(12)
            http.close()
            assert not thread.is_alive() and not failures


def test_task_xml_is_scoped_and_plan_only(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, path, _ = node
    plan = task_plan(config, path, Path(sys.executable))
    tasks = plan["tasks"]
    assert isinstance(tasks, list) and len(tasks) == 6
    typed_tasks = cast(list[dict[str, JsonValue]], tasks)
    for task in typed_tasks:
        assert isinstance(task, dict) and isinstance(task["xml"], str)
        root = ET.fromstring(task["xml"])
        ns = {"t": NS}
        assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
        assert (
            root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns)
            == "InteractiveToken"
        )
        assert config.service_sid in task["xml"] and "ATS_SECRET" not in task["xml"]
    pull = next(task for task in typed_tasks if str(task["name"]).endswith(".PullPlan"))
    pull_root = ET.fromstring(str(pull["xml"]))
    pull_ns = {"t": NS}
    assert (
        pull_root.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=pull_ns)
        == "2026-01-01T14:30:00"
    )
    assert (
        pull_root.findtext(
            "t:Triggers/t:CalendarTrigger/t:Repetition/t:Duration", namespaces=pull_ns
        )
        == "PT26M"
    )
    assert plan["registration_performed"] is False
    output = subprocess.run(
        [
            "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(Path("scripts/windows/install-node-tasks.ps1").resolve()),
            "-Python",
            sys.executable,
            "-ConfigPath",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert output.returncode == 0, output.stderr
    assert json.loads(output.stdout)["registration_performed"] is False


def test_actual_cli_engine_subprocess_duplicate_and_restart(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, path, _ = node
    command = [sys.executable, "-m", "auto_trading_system.apps.node_process", "--config", str(path)]
    previous_boot = None
    for _ in range(2):
        child = subprocess.Popen(
            [*command, "engine"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            assert child.stdout
            with ThreadPoolExecutor(max_workers=1) as pool:
                initial = json.loads(pool.submit(child.stdout.readline).result(timeout=10))
            assert initial["halted"] and not initial["real_trading_enabled"]
            assert initial["boot_id"] != previous_boot
            previous_boot = initial["boot_id"]
            duplicate = subprocess.run(
                [*command, "engine"], capture_output=True, text=True, timeout=15
            )
            assert duplicate.returncode == 2
            assert rpc(config, "operator", "control").invoke("STATUS")["boot_id"] == previous_boot
            assert main(["--config", str(path), "stop", "engine"]) == 0
            assert child.wait(timeout=10) == 0
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()


@contextmanager
def fixture_child(path: Path, component: str) -> Iterator[subprocess.Popen[str]]:
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("node_fixture_process.py")),
            str(path),
            component,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout
        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = json.loads(pool.submit(child.stdout.readline).result(timeout=10))
        assert ready["component"] == component
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()


def test_three_actual_processes_daily_discovery_agent_kill_and_catch_up(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, path, _ = node
    # Engine publishes before any agent exists: recovery must discover this
    # date reference even though the agent never registered an upload job.
    with fixture_child(path, "engine") as engine_process:
        engine = rpc(config, "agent", "engine")
        eventually(lambda: engine.invoke("STATUS")["state"] == "REVIEW_REQUIRED")
        frozen = engine.invoke("EXPORT_DAILY")
        boot = engine.invoke("STATUS")["boot_id"]
        for _ in range(2):
            with fixture_child(path, "agent") as agent_process:
                jobs = rpc(config, "jobrunner", "agent")

                def idle(jobs: RPCClient = jobs) -> bool:
                    return jobs.invoke("STATUS")["status"] == "IDLE"

                eventually(idle)
                assert last_job_status(jobs) == "SYNCED"
                # Third real process is the thin job runner. Daily export
                # reuses the already-frozen reference; it cannot create orders.
                command = [
                    sys.executable,
                    "-m",
                    "auto_trading_system.apps.node_process",
                    "--config",
                    str(path),
                    "job",
                    "daily-export",
                ]
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stdout + result.stderr
                agent_process.terminate()
                assert agent_process.wait(timeout=10) != 0
                assert engine_process.poll() is None
                assert engine.invoke("STATUS")["boot_id"] == boot
                assert engine.invoke("STATUS")["halted"]
                assert engine.invoke("EXPORT_DAILY") == frozen
        rpc(config, "operator", "control").invoke("STOP")
        assert engine_process.wait(timeout=10) == 0


@pytest.mark.parametrize(
    "change",
    ["position", "order_ids", "account", "stale", "unstable", "event", "incoherent", "day"],
)
def test_unassigned_recovery_never_discards_nonempty_or_stale_facts(change: str) -> None:
    account = AccountFact(
        account_id="empty-account",
        snapshot_id="empty-snapshot",
        observed_at=DEMO_NOW,
        cash="100",
        positions=(),
        observed_order_ids=(),
        coherent=True,
    )
    snapshot = BrokerRecoverySnapshot((), (), account, 0, True)
    changes: dict[str, tuple[str, object]] = {
        "position": ("positions", (PositionFact(symbol="000001.SZ", quantity=100, sellable=100),)),
        "order_ids": ("observed_order_ids", ("unowned",)),
        "account": ("account_id", "wrong"),
        "stale": ("observed_at", DEMO_NOW - timedelta(seconds=6)),
        "incoherent": ("coherent", False),
        "day": ("observed_at", DEMO_NOW - timedelta(days=1)),
    }
    if change in changes:
        field, value = changes[change]
        snapshot = BrokerRecoverySnapshot(
            (), (), account.model_copy(update={field: value}), 0, True
        )
    elif change == "unstable":
        snapshot = BrokerRecoverySnapshot((), (), account, 0, False)
    else:
        snapshot = BrokerRecoverySnapshot((), (), account, 1, True)
    with pytest.raises(ValueError):
        check_empty_observation(
            snapshot=snapshot,
            previous=OrderProjection(),
            account_id="empty-account",
            trade_date=DEMO_NOW.date(),
            now=DEMO_NOW,
            event_version=0,
        )


@pytest.mark.parametrize(
    "kind,instant,reason",
    [
        ("pull-plan", DEMO_NOW.replace(hour=10), "OUTSIDE_PLAN_WINDOW"),
        ("pull-plan", DEMO_NOW.replace(hour=14, minute=29, second=59), "OUTSIDE_PLAN_WINDOW"),
        ("daily-export", DEMO_NOW, "OUTSIDE_EXPORT_WINDOW"),
        ("daily-export", DEMO_NOW.replace(day=9), "NON_TRADING_DAY"),
    ],
)
def test_jobs_skip_without_network_or_engine(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope], kind: str, instant: datetime, reason: str
) -> None:
    config, _, _ = node

    def forbidden(_: bytes) -> bytes:
        raise AssertionError("must not reach engine")

    worker = AgentWorker(config, RPCClient(forbidden, "agent", KEY), clock=lambda: instant)
    assert worker.run(kind) == {"status": "SKIPPED", "reason": reason}
    with pytest.raises(IPCError):
        worker.run("invalid")


def test_agent_busy_invalid_jobs_failure_and_stop(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, _ = node
    worker = AgentWorker(config, rpc(config, "agent", "engine"), clock=lambda: DEMO_NOW)
    hold = Event()

    def delayed(_: str) -> dict[str, JsonValue]:
        hold.wait(3)
        raise RuntimeError("sensitive external error")

    monkeypatch.setattr(worker, "run", delayed)
    stop = Event()
    endpoint = AgentEndpoint(worker, stop, "boot")
    try:
        invalid_payloads: tuple[dict[str, JsonValue], ...] = ({}, {"kind": []}, {"kind": "wrong"})
        for payload in invalid_payloads:
            with pytest.raises(IPCError):
                endpoint.handle(request("RUN_JOB", payload=payload))
        accepted = endpoint.handle(request("RUN_JOB", payload={"kind": "catch-up"}))
        assert accepted["status"] == "RUNNING"
        with pytest.raises(IPCError, match="BUSY"):
            endpoint.handle(request("RUN_JOB", payload={"kind": "catch-up"}))
        with pytest.raises(IPCError, match="BUSY"):
            endpoint.catch_up_on_start()
        hold.set()
        eventually(lambda: endpoint.handle(request("STATUS"))["status"] == "IDLE")
        status = endpoint.handle(request("STATUS"))
        assert status["last_result"] == {"status": "PENDING", "reason": "JOB_FAILED_RETRY_REQUIRED"}
        with pytest.raises(IPCError):
            endpoint.handle(request("STATUS", payload={"extra": True}))
        with pytest.raises(IPCError):
            endpoint.handle(request("ARM"))
        assert endpoint.handle(request("STOP")) == {"stopping": True}
        with pytest.raises(IPCError, match="STOPPING"):
            endpoint.handle(request("RUN_JOB", payload={"kind": "catch-up"}))
    finally:
        hold.set()
        endpoint.close()


def test_http_invocation_budget_rejects_before_any_request() -> None:
    def deny() -> None:
        from auto_trading_system.sync.daily_upload import RetryableSyncError

        raise RetryableSyncError("budget")

    client = SignedHttpClient(
        "https://offline.test",
        NodeCredentials("node", "key", KEY),
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(AssertionError("network call"))
        ),
        admission=deny,
    )
    try:
        with pytest.raises(RuntimeError, match="budget"):
            client.request("GET", "/example")
    finally:
        client.close()


def test_catalog_corruption_and_scope_not_silently_skipped(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, _, _ = node
    root = config.data_root / "daily-catalog"
    files = ArtifactFiles(config.data_root / "artifacts")
    catalog = DailyCatalog(
        root, files, node_id=config.node_id, account_id=config.account_id, generation=1
    )
    assert tuple(catalog.discover()) == ()
    (root / "2026-09-07.json").write_bytes(b'{"arbitrary":"content"}')
    with pytest.raises(ValueError, match="invalid daily reference"):
        tuple(catalog.discover())


def test_scheduled_daily_corrections_signed_upload_and_engine_offline_retry(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config, _, plan = node
    instant = [DEMO_NOW.replace(hour=15, minute=31)]
    credentials = NodeCredentials(
        config.node_id, config.quant_key_id, resolve_secret(config.quant_secret)
    )
    remote = BusinessRemote(plan, credentials, lambda: instant[0].timestamp())
    http = SignedHttpClient(
        config.quant_origin,
        credentials,
        transport=httpx.MockTransport(remote.handle),
        clock=lambda: instant[0].timestamp(),
    )
    catalog = DailyCatalog(
        config.data_root / "daily-catalog",
        ArtifactFiles(config.data_root / "artifacts"),
        node_id=config.node_id,
        account_id=config.account_id,
        generation=1,
    )
    try:
        with running_engine(config, lambda: instant[0]) as engine:
            recovered(engine)
            worker = AgentWorker(
                config, engine, clock=lambda: instant[0], remote=HttpQuantTransport(http)
            )
            assert worker.run("daily-export")["status"] == "SYNCED"
            first = catalog.get(instant[0].date())
            assert first is not None and first.manifest.revision == 1
            assert worker.run("daily-export")["status"] == "SYNCED"
            assert catalog.get(instant[0].date()) == first
            # Next closing slot creates a full successor even if all quantities
            # stayed zero: this is new queried evidence, not an inferred change.
            instant[0] = instant[0].replace(minute=46)
            remote.offline = True
            assert worker.run("daily-export")["status"] == "PENDING"
            second = catalog.get(instant[0].date())
            assert second is not None and second.manifest.revision == 2
            assert second.manifest.supersedes_batch_id == first.manifest.batch_id
            assert engine.invoke("EXPORT_DAILY")["batch_id"] == second.manifest.batch_id
            assert engine.invoke("REFRESH_DAILY")["batch_id"] == second.manifest.batch_id
            assert engine.invoke("STATUS")["halted"]
        remote.offline = False

        def unavailable(_: bytes) -> bytes:
            raise IPCError("PIPE_UNAVAILABLE")

        worker = AgentWorker(
            config,
            RPCClient(unavailable, "agent", KEY),
            clock=lambda: instant[0],
            remote=HttpQuantTransport(http),
        )
        for _ in range(3):
            result = worker.run("catch-up")
            if result["status"] == "SYNCED":
                break
        assert result["status"] == "SYNCED"
        current = remote.receiver.current(config.account_id, instant[0].date().isoformat())
        assert current is not None and current.revision == 2
        instant[0] = instant[0].replace(hour=16, minute=1)
        pending = worker.run("daily-export")
        assert pending["status"] == "PENDING" and pending["fresh_export"] == "PENDING"
        assert tuple(catalog.discover()) == (first, second)
        # A corrupt newly discovered reference reports attention without stopping
        # remote receipt verification for already registered good revisions.
        (catalog.root / "2026-09-08.json").write_bytes(b"{}")
        review = worker.run("catch-up")
        assert review["status"] == "NEEDS_ATTENTION"
        assert review["reason_code"] == "DAILY_CATALOG_REVIEW"
        assert review["jobs"]
    finally:
        http.close()


def test_config_schema_sample_and_cli_fail_closed(
    node: tuple[NodeProcessConfig, Path, PlanEnvelope], capsys: pytest.CaptureFixture[str]
) -> None:
    config, path, _ = node
    example = load_node_config(Path("config/v2/node.mock.example.json"))
    assert example.broker == "MOCK"
    schema = json.loads(Path("config/v2/node-config.schema.json").read_text(encoding="utf-8"))
    assert schema == NodeProcessConfig.model_json_schema()
    assert main(["--config", str(path), "task-plan"]) == 0
    assert main(["--config", str(path), "init"]) == 2
    assert main(["--config", str(path.parent / "missing.json"), "check"]) == 2
    assert "ATS_SECRET_NODE" not in capsys.readouterr().out
    with pytest.raises(ValueError):
        task_plan(config, Path("relative"), Path(sys.executable))
    raw = config.model_dump(mode="json")
    raw["quant_secret"] = config.ipc_secrets.agent.model_dump(mode="json")
    with pytest.raises(ValueError):
        NodeProcessConfig.model_validate_json(canonical_bytes(raw))
