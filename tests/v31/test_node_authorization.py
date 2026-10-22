"""Real named-pipe approvals plus deterministic owner/expiry/credential boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from pathlib import Path
from secrets import token_hex
from threading import Event
from time import monotonic, sleep
from typing import Any

import pytest
import test_node_processes as node_helpers
from pydantic import JsonValue
from test_node_processes import eventually, recovered, rpc, running_engine

from auto_trading_system.apps.node_process.__main__ import main
from auto_trading_system.apps.node_process.authorization_cli import authorize, query_result
from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.ipc_protocol import IPCError, RPCClient
from auto_trading_system.control.node_authorization import NodeAuthorization, PreviewProposal
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.runtime import node_engine
from auto_trading_system.security.operator_password import OperatorPasswordHasher
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.node_agent import EnginePlanClient

node_fixture = node_helpers.node


@pytest.fixture
def access(
    node_fixture: tuple[NodeProcessConfig, Path, PlanEnvelope], monkeypatch: pytest.MonkeyPatch
) -> tuple[NodeProcessConfig, Path, PlanEnvelope, str]:
    config, path, plan = node_fixture
    password = token_hex(24)
    verifier = OperatorPasswordHasher().hash_password(password)
    monkeypatch.setenv("ATS_SECRET_NODE_OPERATOR_PASSWORD", verifier)
    raw = config.model_dump(mode="json")
    raw["operator_access"] = {
        "actor_id": "test-operator",
        "password_verifier": {
            "source": "ENVIRONMENT",
            "variable": "ATS_SECRET_NODE_OPERATOR_PASSWORD",
        },
    }
    config = NodeProcessConfig.model_validate_json(canonical_bytes(raw))
    path.write_bytes(canonical_bytes(raw))
    return config, path, plan, password


def load_plan(config: NodeProcessConfig, plan: PlanEnvelope, agent: RPCClient) -> None:
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
    assert EnginePlanClient(files, agent).apply(lookup, artifact).status == "LOADED"
    control = rpc(config, "operator", "control")
    requested = control.invoke("REQUEST_RECOVERY")
    version = requested["state_version"]
    assert type(version) is int
    recovered(agent, minimum_version=version)


@contextmanager
def prepared(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> Iterator[tuple[RPCClient, RPCClient, RPCClient]]:
    config, _, plan, _ = access
    with running_engine(config, lambda: DEMO_NOW) as agent:
        recovered(agent)
        load_plan(config, plan, agent)
        yield rpc(config, "operator", "authorization"), rpc(config, "operator", "control"), agent


def login(client: RPCClient, password: str) -> dict[str, JsonValue]:
    result = client.invoke("LOGIN", {"password": password})
    assert result["actor_id"] == "test-operator"
    return {"session_token": result["session_token"], "csrf_token": result["csrf_token"]}


def preview(
    client: RPCClient,
    session: dict[str, JsonValue],
    action: str = "CONFIRM_RECOVERY",
    **limits: Any,
) -> dict[str, JsonValue]:
    proposal: dict[str, JsonValue] = {"action": action}
    if action != "CONFIRM_RECOVERY":
        proposal["grant"] = {
            "directions": ["BUY"],
            "max_buy_value": "0.00",
            "max_order_value": "0.00",
            "max_positions": 0,
            "duration_seconds": 60,
            **limits,
        }
    return client.invoke("PREVIEW_CONTROL", {**session, **proposal})


def confirm(
    client: RPCClient, session: dict[str, JsonValue], proposal: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    result = client.invoke(
        "CONFIRM_CONTROL",
        {
            **session,
            "preview_id": proposal["preview_id"],
            "phrase": proposal["confirmation_phrase"],
        },
    )
    assert result["status"] in ("PENDING", "APPLIED")
    until = monotonic() + 5
    while result["status"] == "PENDING" and monotonic() < until:
        sleep(0.005)
        result = client.invoke("CONTROL_RESULT", {**session, "request_id": result["request_id"]})
    assert result["status"] == "APPLIED", result
    return result


def test_real_pipe_recovery_confirmation_arm_resume_and_audit(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    config, _, _, password = access
    with prepared(access) as (client, control, agent):
        session = login(client, password)
        proposal = preview(client, session)
        assert proposal["installed_limits"] == {
            "mode": "PAPER",
            "max_buy_value": "0",
            "max_order_value": "0",
            "max_positions": 0,
            "max_order_quantity": 1,
        }
        assert proposal["real_trading_enabled"] is False
        scope = proposal["execution_scope"]
        assert isinstance(scope, dict) and scope["strategy_id"] == config.strategy_id
        assert scope["payload_hash"] == access[2].envelope.payload_hash
        assert type(proposal["permission_epoch"]) is int
        receipt = confirm(client, session, proposal)
        assert confirm(client, session, proposal) == receipt  # ACK lost / duplicate confirmation
        arm = preview(client, session, "ARM")
        request = arm["request"]
        assert isinstance(request, dict) and isinstance(request["grant"], dict)
        assert request["grant"]["confirmed_by"] == "test-operator"
        assert request["grant"]["mode"] == "PAPER"
        confirm(client, session, arm)
        eventually(lambda: control.invoke("STATUS")["state"] == "ARMED")
        assert control.invoke("HALT")["halt_latched"]
        requested = control.invoke("REQUEST_RECOVERY")
        version = requested["state_version"]
        assert type(version) is int
        recovered(agent, minimum_version=version)
        confirm(client, session, preview(client, session))
        resume = preview(client, session, "RESUME")
        resumed = resume["request"]
        assert isinstance(resumed, dict) and isinstance(resumed["grant"], dict)
        assert resumed["grant"]["session_generation"] == 2
        confirm(client, session, resume)
        assert control.invoke("STATUS")["real_trading_enabled"] is False
        client.invoke("LOGOUT", session)
    # Audit verification opens only the fixture's dedicated DB, after the owner exits.
    import apsw

    connection = apsw.Connection(
        str(config.data_root / "critical/journal-v2.db"), flags=apsw.SQLITE_OPEN_READONLY
    )
    try:
        rows = list(connection.execute("SELECT payload_json FROM control_records"))
        assert len(rows) == 4
        assert all(password not in str(row[0]) for row in rows)
        assert all("test-operator" in str(row[0]) for row in rows)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "change",
    [
        "missing-session",
        "wrong-csrf",
        "wrong-token",
        "actor-injection",
        "grant-injection",
        "wrong-action",
        "missing-grant",
        "negative-limit",
        "duplicate-direction",
        "live-mode",
        "excess-duration",
    ],
)
def test_authorization_rejects_invalid_identity_or_payload(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str], change: str
) -> None:
    with prepared(access) as (client, control, _):
        session = login(client, access[3])
        payload: dict[str, JsonValue] = {**session, "action": "CONFIRM_RECOVERY"}
        if change == "missing-session":
            payload.pop("session_token")
        elif change == "wrong-csrf":
            payload["csrf_token"] = "x" * 43
        elif change == "wrong-token":
            payload["session_token"] = "y" * 43
        elif change == "actor-injection":
            payload["actor_id"] = "forged-actor"
        elif change == "grant-injection":
            payload["grant"] = {}
        elif change == "wrong-action":
            payload["action"] = "LOAD_PLAN"
        elif change == "missing-grant":
            payload["action"] = "ARM"
        else:
            grant: dict[str, JsonValue] = {
                "directions": ["BUY"],
                "max_buy_value": "0",
                "max_order_value": "0",
                "max_positions": 0,
                "duration_seconds": 60,
            }
            changes: dict[str, tuple[str, JsonValue]] = {
                "negative-limit": ("max_buy_value", "-1"),
                "duplicate-direction": ("directions", ["BUY", "BUY"]),
                "live-mode": ("mode", "LIVE"),
                "excess-duration": ("duration_seconds", 901),
            }
            field, value = changes[change]
            grant[field] = value
            payload.update(action="ARM", grant=grant)
        with pytest.raises(IPCError):
            client.invoke("PREVIEW_CONTROL", payload)
        assert control.invoke("STATUS")["halted"]


@pytest.mark.parametrize(
    "change",
    [
        "wrong-phrase",
        "other-session",
        "logout",
        "halt",
        "recovery",
        "mutated-confirm",
        "unknown-result",
    ],
)
def test_preview_is_bound_to_session_epoch_and_exact_payload(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str], change: str
) -> None:
    with prepared(access) as (client, control, _):
        session = login(client, access[3])
        proposal = preview(client, session)
        payload = {
            **session,
            "preview_id": proposal["preview_id"],
            "phrase": proposal["confirmation_phrase"],
        }
        if change == "wrong-phrase":
            payload["phrase"] = "CONFIRM"
        elif change == "other-session":
            payload.update(login(client, access[3]))
        elif change == "logout":
            client.invoke("LOGOUT", session)
        elif change == "halt":
            control.invoke("HALT")
        elif change == "recovery":
            control.invoke("REQUEST_RECOVERY")
        elif change == "mutated-confirm":
            payload["grant"] = {"max_buy_value": "100000"}
        with pytest.raises(IPCError):
            if change == "unknown-result":
                client.invoke("CONTROL_RESULT", {**session, "request_id": "not-a-request"})
            else:
                client.invoke("CONFIRM_CONTROL", payload)
        assert control.invoke("STATUS")["halted"]


@pytest.mark.parametrize(
    "limits", [{"max_buy_value": "1"}, {"max_order_value": "1"}, {"max_positions": 1}]
)
def test_proposal_cannot_raise_installed_zero_ceiling(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str], limits: dict[str, JsonValue]
) -> None:
    with prepared(access) as (client, control, _):
        session = login(client, access[3])
        confirm(client, session, preview(client, session))
        with pytest.raises(IPCError):
            preview(client, session, "ARM", **limits)
        assert control.invoke("STATUS")["halted"]


def test_login_rate_limit_does_not_block_emergency_pipe(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    with prepared(access) as (client, control, _):
        for _ in range(5):
            with pytest.raises(IPCError, match="OPERATOR_LOGIN_FAILED"):
                client.invoke("LOGIN", {"password": "wrong"})
        with pytest.raises(IPCError, match="OPERATOR_LOGIN_RATE_LIMITED"):
            login(client, access[3])
        assert control.invoke("HALT")["halt_latched"]


def test_slow_password_verification_cannot_occupy_halt_lane(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = OperatorPasswordHasher.verify

    def slow(self: OperatorPasswordHasher, password: str, verifier: str) -> bool:
        entered.set()
        assert release.wait(5)
        return original(self, password, verifier)

    monkeypatch.setattr(OperatorPasswordHasher, "verify", slow)
    with prepared(access) as (client, control, _), ThreadPoolExecutor(1) as pool:
        pending = pool.submit(login, client, access[3])
        try:
            assert entered.wait(3)
            assert not pending.done()
            assert control.invoke("HALT")["halt_latched"]
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=5)


def test_preview_expiry_and_restart_invalidate_old_session(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    config, _, _, password = access
    instant = [DEMO_NOW]
    with running_engine(config, lambda: instant[0]) as agent:
        recovered(agent)
        client = rpc(config, "operator", "authorization")
        session = login(client, password)
        proposal = preview(client, session)
        instant[0] += timedelta(seconds=21)
        with pytest.raises(IPCError):
            confirm(client, session, proposal)
    with running_engine(config, lambda: instant[0]) as restarted:
        client = rpc(config, "operator", "authorization")
        with pytest.raises(IPCError, match="OPERATOR_SESSION_INVALID"):
            preview(client, session)
        assert restarted.invoke("STATUS")["halted"]


def test_sync_and_jobrunner_roles_cannot_login_or_mint_approval(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    with prepared(access) as (_, _, agent):
        with pytest.raises(IPCError):
            agent.invoke("LOGIN", {"password": access[3]})
        for role in ("agent", "jobrunner"):
            with pytest.raises((IPCError, OSError)):
                rpc(access[0], role, "authorization").invoke("LOGIN", {"password": access[3]})


@pytest.mark.parametrize("cancel", [False, True])
def test_interactive_workflow_hides_credentials_and_logs_out(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str], cancel: bool
) -> None:
    with prepared(access) as (client, control, _):
        shown: list[dict[str, JsonValue]] = []
        status = authorize(
            client,
            PreviewProposal(action="CONFIRM_RECOVERY"),
            password_reader=lambda _: access[3],
            confirmation_reader=lambda _: "" if cancel else str(shown[0]["confirmation_phrase"]),
            output=shown.append,
        )
        assert status == (1 if cancel else 0)
        text = json.dumps(shown)
        assert access[3] not in text and "session_token" not in text and "csrf_token" not in text
        assert shown[-1]["status"] == ("CANCELLED" if cancel else "APPLIED")
        assert control.invoke("STATUS")["halted"]


def test_cli_refuses_noninteractive_authorization(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["--config", str(access[1]), "authorize", "arm"]) == 2
    output = capsys.readouterr().out
    assert access[3] not in output and "INTERACTIVE_OPERATOR_REQUIRED" in output


def test_result_lookup_after_new_login_does_not_repeat_authorization(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    with prepared(access) as (client, _, _):
        session = login(client, access[3])
        result = confirm(client, session, preview(client, session))
        client.invoke("LOGOUT", session)
        shown: list[dict[str, JsonValue]] = []
        assert (
            query_result(
                client,
                str(result["request_id"]),
                password_reader=lambda _: access[3],
                output=shown.append,
            )
            == 0
        )
        assert shown == [result]


def test_capacity_and_monotonic_deadline_are_independent_of_wall_clock(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = [0.0]
    monkeypatch.setattr(
        node_engine,
        "NodeAuthorization",
        partial(NodeAuthorization, elapsed=lambda: elapsed[0], capacity=1),
    )
    with prepared(access) as (client, control, _):
        session = login(client, access[3])
        original = preview(client, session)
        with pytest.raises(IPCError, match="APPROVAL_CAPACITY_EXHAUSTED"):
            preview(client, session)
        elapsed[0] = 21.0
        with pytest.raises(IPCError, match="PREVIEW_EXPIRED_OR_FENCED"):
            confirm(client, session, original)
        # Expired, unconfirmed entries may be reclaimed, completed receipts stay.
        confirm(client, session, preview(client, session))
        with pytest.raises(IPCError, match="APPROVAL_CAPACITY_EXHAUSTED"):
            preview(client, session, "ARM")
        assert control.invoke("STATUS")["halted"]


@pytest.mark.parametrize(
    "action,payload",
    [
        ("LOGIN", {}),
        ("LOGIN", {"password": 42}),
        ("LOGIN", {"password": "x" * 129}),
        ("LOGIN", {"password": "value", "actor_id": "forged"}),
        ("LOGOUT", {"extra": True}),
        ("CONFIRM_CONTROL", {"preview_id": 1, "phrase": "value"}),
        ("CONTROL_RESULT", {}),
        ("CONTROL_RESULT", {"request_id": 3}),
    ],
)
def test_authentication_boundary_rejects_malformed_fields(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    action: str,
    payload: dict[str, JsonValue],
) -> None:
    with prepared(access) as (client, control, _):
        session = {} if action == "LOGIN" else login(client, access[3])
        with pytest.raises(IPCError, match="INVALID_ARGUMENTS"):
            client.invoke(action, {**session, **payload})
        assert control.invoke("STATUS")["halted"]


def test_configured_invalid_password_verifier_fails_before_ready(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATS_SECRET_NODE_OPERATOR_PASSWORD", "not-a-verifier-" + "x" * 32)
    ready: list[dict[str, JsonValue]] = []
    with pytest.raises(ValueError, match="verifier"):
        node_engine.run_engine(access[0], clock=lambda: DEMO_NOW, ready=ready.append)
    assert ready == []


def test_arm_requires_previously_confirmed_recovery(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    with prepared(access) as (client, control, _):
        session = login(client, access[3])
        with pytest.raises(IPCError):
            preview(client, session, "ARM")
        assert control.invoke("STATUS")["halted"]


def test_authorization_reference_cannot_reuse_ipc_secret(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    value = access[0].model_dump(mode="json")
    value["operator_access"]["password_verifier"] = value["ipc_secrets"]["operator"]
    with pytest.raises(ValueError):
        NodeProcessConfig.model_validate_json(canonical_bytes(value))


def test_missing_optional_access_keeps_authorization_disabled(
    node_fixture: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    with running_engine(node_fixture[0], lambda: DEMO_NOW) as agent:
        assert agent.invoke("STATUS")["operator_authorization_enabled"] is False


def test_real_process_confirmation_and_restart_remain_paused(
    access: tuple[NodeProcessConfig, Path, PlanEnvelope, str],
) -> None:
    config, path, _, password = access
    command = [sys.executable, "tests/v31/node_fixture_process.py", str(path), "engine"]
    previous_session: dict[str, JsonValue] = {}
    for round_number in range(2):
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            control = rpc(config, "operator", "control")

            def available(
                child: subprocess.Popen[str] = process, target: RPCClient = control
            ) -> bool:
                if child.poll() is not None:
                    raise AssertionError(child.communicate(timeout=3))
                try:
                    return target.invoke("STATUS")["state"] == "REVIEW_REQUIRED"
                except (OSError, IPCError):
                    return False

            eventually(available)
            client = rpc(config, "operator", "authorization")
            if round_number:
                with pytest.raises(IPCError):
                    preview(client, previous_session)
            session = login(client, password)
            confirm(client, session, preview(client, session))

            def settled(target: RPCClient = control) -> bool:
                return target.invoke("STATUS")["state"] == "READY_PAUSED"

            eventually(settled)
            assert control.invoke("STATUS")["halted"]
            previous_session = session
            control.invoke("STOP")
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
