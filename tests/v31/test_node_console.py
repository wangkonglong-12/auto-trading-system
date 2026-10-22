"""V2 console boundaries: real engine pipes, no real orders or remote service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, cast

import pytest
import test_node_authorization as auth_helpers
import test_node_processes as node_helpers
import uvicorn
from fastapi.testclient import TestClient
from pydantic import JsonValue

from auto_trading_system.apps.local_console import node_app
from auto_trading_system.apps.local_console.app import LocalConsoleAssetError
from auto_trading_system.apps.local_console.node_app import (
    PREFIX,
    _rule_response,
    build_node_console_app,
)
from auto_trading_system.apps.local_console.node_bridge import NodeConsoleBridge
from auto_trading_system.apps.local_console.node_status import NodeStatusCache, project_status
from auto_trading_system.apps.node_process.__main__ import main
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.strategies.sell_rule_drafts import SellRuleDraft
from auto_trading_system.strategies.sell_rules import SellRules

node_fixture = node_helpers.node
access = auth_helpers.access
ORIGIN = "http://127.0.0.1:8780"
HEADERS = {"origin": ORIGIN, "sec-fetch-site": "same-origin"}
Node = tuple[NodeProcessConfig, Path, PlanEnvelope]
Access = tuple[NodeProcessConfig, Path, PlanEnvelope, str]


def observation(config: NodeProcessConfig) -> dict[str, JsonValue]:
    return {
        "component": "engine",
        "node_id": config.node_id,
        "account_id": config.account_id,
        "node_generation": config.node_generation,
        "broker": "MOCK",
        "real_trading_enabled": False,
        "boot_id": "test-boot",
        "owner_sequence": 1,
        "state": "READY_PAUSED",
        "plan": {
            "plan_id": "p1",
            "strategy_id": "s1",
            "trade_date": "2026-09-24",
            "symbols": ["600000.SH"],
            "private": "must-not-leak",
        },
        "installed_limits": {"max_buy_value": "0", "private": "must-not-leak"},
        "secret": "must-not-leak",
    }


def test_status_freshness_is_owner_progress_not_pipe_response(node_fixture: Node) -> None:
    config = node_fixture[0]
    tick = [0.0]
    source = observation(config)
    source["paper_session"] = {
        "mode": "FICTIONAL_ACCEPTANCE_ONLY",
        "phase": "EXIT",
        "rule_id": "COST_EXIT_V1",
        "sell_rules_hash": "sha256:" + "a" * 64,
        "driver_state": "READY",
        "secret": "must-not-leak",
    }
    cache = NodeStatusCache(
        config,
        {
            "engine": lambda: deepcopy(source),
            "agent": lambda: {
                "component": "agent",
                "last_result": {"status": "PENDING", "secret": "private"},
            },
            "supervisor": lambda: {"component": "supervisor"},
        },
        elapsed=lambda: tick[0],
    )

    def state() -> Any:
        return cache.snapshot()["components"]

    assert state()["engine"]["availability"] == "UNAVAILABLE"
    for name in ("engine", "agent", "supervisor"):
        cache.probe_once(name)
    assert "must-not-leak" not in str(state())
    assert "private" not in str(state())
    assert state()["engine"]["data"]["paper_session"]["rule_id"] == "COST_EXIT_V1"
    assert state()["engine"]["availability"] == "CURRENT"
    state()["engine"]["data"]["state"] = "caller-cannot-mutate-cache"
    assert state()["engine"]["data"]["state"] == "READY_PAUSED"
    tick[0] = 5
    cache.probe_once("engine")
    assert state()["engine"]["availability"] == "STALE"
    assert state()["agent"]["availability"] == "STALE"
    source["owner_sequence"] = 2
    cache.probe_once("engine")
    assert state()["engine"]["availability"] == "CURRENT"
    source["owner_sequence"] = 1
    cache.probe_once("engine")
    assert state()["engine"]["error"] == "PROGRESS_REGRESSED"
    assert state()["engine"]["data"]["owner_sequence"] == 2
    source["boot_id"] = "new-boot"
    cache.probe_once("engine")
    assert state()["engine"]["availability"] == "CURRENT"
    source["account_id"] = "different-account"
    cache.probe_once("engine")
    assert state()["engine"]["availability"] == "UNAVAILABLE"
    assert state()["engine"]["data"]["account_id"] == config.account_id


def test_rule_status_requires_matching_installed_exit_rules_and_armed_state() -> None:
    rules = SellRules(
        take_profit_pct=Decimal("5"), stop_loss_pct=Decimal("3"), sell_by=time(14, 30)
    )
    draft = SellRuleDraft(version=2, saved_at=datetime.now(UTC), rules=rules)
    paper: dict[str, JsonValue] = {
        "phase": "EXIT",
        "rule_id": "COST_EXIT_V1",
        "sell_rules_hash": digest(canonical_bytes(rules.model_dump(mode="json"))),
    }
    snapshot: dict[str, JsonValue] = {
        "components": {"engine": {"data": {"state": "ARMED", "paper_session": paper}}}
    }
    assert _rule_response(draft, snapshot) == {
        "draft": draft.model_dump(mode="json"),
        "simulation_ready": True,
        "active": True,
        "active_version": 2,
    }
    cast(dict[str, Any], cast(dict[str, Any], snapshot["components"])["engine"])["data"] = {
        "state": "READY_PAUSED",
        "paper_session": paper,
    }
    paused = _rule_response(draft, snapshot)
    assert paused["simulation_ready"] is True and paused["active"] is False
    paper["sell_rules_hash"] = "sha256:" + "0" * 64
    assert _rule_response(draft, snapshot)["simulation_ready"] is False


def test_market_subscription_and_signal_status_reaches_ui_without_private_fields(
    node_fixture: Node,
) -> None:
    config = node_fixture[0]
    value = observation(config)
    value["plan_loaded"] = True
    value["exit_context_loaded"] = False
    value["market_session"] = {
        "subscription_count": 1,
        "driver_state": "WAITING_MARKET",
        "secret": "private-value",
        "entry_progress": [
            {"symbol": "600000.SH", "state": "WAITING_MARKET", "secret": "private-value"}
        ],
        "entry_targets": [],
        "recent_activity": [{"state": "ACKED", "category": "ORDER", "secret": "private-value"}],
    }
    result = project_status("engine", value, config)
    assert result["plan_loaded"] is True
    assert result["market_session"] == {
        "subscription_count": 1,
        "driver_state": "WAITING_MARKET",
        "entry_progress": [{"symbol": "600000.SH", "state": "WAITING_MARKET"}],
        "entry_targets": [],
        "recent_activity": [{"state": "ACKED", "category": "ORDER"}],
    }
    assert "private-value" not in str(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("component", "agent"),
        ("account_id", "wrong"),
        ("node_id", "wrong"),
        ("node_generation", True),
        ("broker", "QMT"),
        ("real_trading_enabled", 0),
        ("owner_sequence", True),
        ("owner_sequence", 0),
        ("owner_sequence", "1"),
        ("boot_id", None),
        ("boot_id", ""),
    ],
)
def test_reject_status_identity_or_progress(
    node_fixture: Node, field: str, value: JsonValue
) -> None:
    source = observation(node_fixture[0])
    source[field] = value
    with pytest.raises(IPCError):
        project_status("engine", source, node_fixture[0])


def test_cache_lifecycle(node_fixture: Node) -> None:
    config = node_fixture[0]
    with pytest.raises(ValueError):
        NodeStatusCache(config, {})
    cache = NodeStatusCache(
        config,
        {
            "engine": lambda: observation(config),
            "agent": lambda: {"component": "agent"},
            "supervisor": lambda: {"component": "supervisor"},
        },
    )
    cache.start()
    with pytest.raises(RuntimeError):
        cache.start()
    cache.close()
    with pytest.raises(RuntimeError):
        cache.start()


@pytest.mark.parametrize("port", [0, 1023, 65536, True])
def test_invalid_console_port(node_fixture: Node, port: int) -> None:
    with pytest.raises(ValueError):
        build_node_console_app(node_fixture[0], port=port)


def test_http_static_status_and_same_origin_boundaries(node_fixture: Node) -> None:
    config = node_fixture[0]
    with TestClient(build_node_console_app(config), base_url=ORIGIN) as client:
        response = client.get("/")
        assert response.status_code == 200 and "node-" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/", headers={"host": "attacker.test"}).status_code == 403
        assert client.get("/docs").status_code == 404
        assert client.get("/index.html").status_code == 404
        snapshot = client.get(PREFIX + "/status").json()
        assert snapshot["strategy_status"] == "UNCONFIRMED"
        assert snapshot["real_trading_enabled"] is False
        assert snapshot["components"]["engine"]["availability"] == "UNAVAILABLE"
        for headers in (
            {},
            {"origin": "https://foreign.test", "sec-fetch-site": "same-origin"},
            {"origin": ORIGIN, "sec-fetch-site": "same-site"},
        ):
            assert (
                client.post(
                    PREFIX + "/control", json={"action": "HALT"}, headers=headers
                ).status_code
                == 403
            )
        response = client.post(PREFIX + "/control", json={"action": "HALT"}, headers=HEADERS)
        assert response.status_code == 401
        assert client.get(PREFIX + "/session").status_code == 401
        assert client.get(PREFIX + "/trades").status_code == 401


def test_qmt_connection_link_only_accepts_launcher_loopback_url(
    node_fixture: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATS_DESKTOP_SETUP_URL", raising=False)
    with TestClient(build_node_console_app(node_fixture[0]), base_url=ORIGIN) as client:
        assert client.get("/connect-qmt").status_code == 503
        for invalid in (
            "https://127.0.0.1:61000/",
            "http://localhost:61000/",
            "http://127.0.0.1:61000/path",
            "http://user@127.0.0.1:61000/",
        ):
            monkeypatch.setenv("ATS_DESKTOP_SETUP_URL", invalid)
            assert client.get("/connect-qmt").status_code == 503
        monkeypatch.setenv("ATS_DESKTOP_SETUP_URL", "http://127.0.0.1:61000/")
        response = client.get("/connect-qmt", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "http://127.0.0.1:61000/"


@pytest.mark.parametrize(
    "path,body",
    [
        ("session", '{"password":"secret","password":"other"}'),
        ("session", '{"password":123,"unknown":"must-not-leak"}'),
        ("preview", '{"action":"ARM","grant":{"max_buy_value":"100000"}}'),
        ("control", '{"action":"ORDER"}'),
        ("result", '{"request_id":"bad"}'),
        ("confirm", '{"preview_id":"bad","phrase":"must-not-leak"}'),
        ("session", '{"password":"' + "a" * 9000 + '"}'),
    ],
)
def test_http_input_validation_is_bounded_and_redacted(
    node_fixture: Node, path: str, body: str
) -> None:
    with TestClient(build_node_console_app(node_fixture[0]), base_url=ORIGIN) as client:
        response = client.post(
            PREFIX + "/" + path,
            content=body,
            headers={**HEADERS, "content-type": "application/json"},
        )
        assert response.status_code == 400
        assert "must-not-leak" not in response.text and "password" not in response.text
        assert (
            client.post(PREFIX + "/session", content="password=x", headers=HEADERS).status_code
            == 400
        )


def test_real_http_pipe_controls_and_session_restoration(access: Access) -> None:
    config, _, plan, password = access
    with auth_helpers.prepared(access) as (_, control, _):
        bridge = NodeConsoleBridge(config)
        with TestClient(build_node_console_app(config, bridge=bridge), base_url=ORIGIN) as client:
            rejected = client.post(
                PREFIX + "/session", json={"password": "incorrect"}, headers=HEADERS
            )
            assert rejected.status_code == 401
            response = client.post(
                PREFIX + "/session", json={"password": password}, headers=HEADERS
            )
            assert response.status_code == 200
            assert "session_token" not in response.json()
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "SameSite=strict" in response.headers["set-cookie"]
            csrf = response.json()["csrf_token"]
            mutation = {**HEADERS, "x-csrf-token": csrf}
            restored = client.get(PREFIX + "/session").json()
            assert restored["csrf_token"] == csrf
            assert restored["actor_id"] == "test-operator"
            details = client.get(PREFIX + "/trades")
            assert details.status_code == 200
            assert details.json()["available"] in (True, False)
            assert set(details.json()) >= {"positions", "orders", "trades"}
            missing_cancel = client.post(
                PREFIX + "/orders/cancel",
                json={"client_order_id": "ord_missing0001"},
                headers=mutation,
            )
            assert missing_cancel.status_code == 409
            assert missing_cancel.json() == {"code": "ORDER_NOT_CANCELABLE"}
            bridge.status.probe_once("engine")
            status = client.get(PREFIX + "/status").json()
            assert status["components"]["engine"]["data"]["plan"]["plan_id"] == plan.payload.plan_id
            assert "password" not in str(status) and "secret" not in str(status)
            denied = client.post(
                PREFIX + "/control",
                json={"action": "HALT"},
                headers={**HEADERS, "x-csrf-token": "x" * 40},
            )
            assert denied.status_code == 403
            preview = client.post(
                PREFIX + "/preview", json={"action": "CONFIRM_RECOVERY"}, headers=mutation
            ).json()
            confirmation = {
                "preview_id": preview["preview_id"],
                "phrase": preview["confirmation_phrase"],
            }
            receipt = client.post(PREFIX + "/confirm", json=confirmation, headers=mutation).json()
            request_id = preview["request"]["request_id"]
            assert receipt["request_id"] == request_id
            node_helpers.eventually(
                lambda: client.post(
                    PREFIX + "/result", json={"request_id": request_id}, headers=mutation
                ).json()["status"]
                == "APPLIED"
            )
            for action in ("ARM", "RESUME"):
                missing_settings = client.post(
                    PREFIX + "/preview", json={"action": action}, headers=mutation
                )
                assert missing_settings.status_code == 409
                assert missing_settings.json() == {"code": "TRADING_SETTINGS_REQUIRED"}
            settings = {
                "buy": {
                    "order_method": "ASK1_PROTECTED_LIMIT",
                    "max_daily_buy_value": "0",
                    "max_symbol_buy_value": "0",
                    "max_order_value": "0",
                    "cancel_after_seconds": 10,
                    "max_retries": 3,
                },
                "sell": {
                    "take_profit_pct": "5",
                    "stop_loss_pct": "3",
                    "sell_by": "14:30:00",
                },
                "safety": {
                    "max_daily_loss": "0",
                    "max_positions": 0,
                    "emergency_liquidation_enabled": False,
                },
                "advanced": {
                    "max_order_quantity": 10000,
                    "max_chase_bps": 30,
                    "max_spread_bps": 20,
                    "quote_max_age_ms": 1000,
                },
            }
            saved_settings = client.post(
                PREFIX + "/trading-settings",
                json={"expected_version": 0, "settings": settings},
                headers=mutation,
            )
            assert saved_settings.status_code == 200
            arm = client.post(PREFIX + "/preview", json={"action": "ARM"}, headers=mutation).json()
            assert arm["request"]["grant"]["max_order_value"] == "0.00"
            assert arm["request"]["grant"]["max_positions"] == 0
            halt = client.post(PREFIX + "/control", json={"action": "HALT"}, headers=mutation)
            assert halt.status_code == 200 and halt.json()["halt_latched"]
            recovery = client.post(
                PREFIX + "/control", json={"action": "RECOVER"}, headers=mutation
            )
            assert recovery.status_code == 200 and recovery.json()["status"] == "REQUESTED"
            logout = client.delete(PREFIX + "/session", headers=mutation)
            assert logout.status_code == 200
            assert client.get(PREFIX + "/session").status_code == 401
        # Console shutdown owns neither engine lifecycle nor trade authority.
        assert control.invoke("STATUS")["real_trading_enabled"] is False


def test_sell_rule_draft_and_preview_are_authenticated_and_do_not_trade(access: Access) -> None:
    config, _, _, password = access
    with (
        auth_helpers.prepared(access),
        TestClient(build_node_console_app(config), base_url=ORIGIN) as client,
    ):
        assert client.get(PREFIX + "/sell-rules").status_code == 401
        login = client.post(PREFIX + "/session", json={"password": password}, headers=HEADERS)
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        rules = {
            "take_profit_pct": "5.00",
            "stop_loss_pct": "3.00",
            "sell_by": "14:30:00",
        }
        body = {"expected_version": 0, "rules": rules}
        denied = client.post(PREFIX + "/sell-rules", json=body, headers=HEADERS)
        assert denied.status_code == 401
        headers = {**HEADERS, "x-csrf-token": csrf}
        saved = client.post(PREFIX + "/sell-rules", json=body, headers=headers)
        assert saved.status_code == 200 and saved.json()["active"] is False
        assert saved.json()["draft"]["version"] == 1
        assert client.get(PREFIX + "/sell-rules").json()["draft"]["version"] == 1
        conflict = client.post(PREFIX + "/sell-rules", json=body, headers=headers)
        assert conflict.status_code == 409
        trial = client.post(
            PREFIX + "/sell-rules/test",
            json={
                "rules": rules,
                "buy_cost": "10.00",
                "current_price": "9.70",
                "observed_at": "2026-10-20T10:00:00+08:00",
            },
            headers=headers,
        )
        assert trial.status_code == 200
        assert trial.json()["reasons"] == ["STOP_LOSS"]
        assert trial.json()["actual_order_sent"] is False
        (config.data_root / "operator" / "sell_rules.json").write_text("invalid", encoding="utf-8")
        broken = client.get(PREFIX + "/sell-rules")
        assert broken.status_code == 503
        assert broken.json() == {"code": "RULE_STORE_INVALID"}


def test_trading_settings_page_api_is_authenticated_versioned_and_paused(access: Access) -> None:
    config, _, _, password = access
    with (
        auth_helpers.prepared(access),
        TestClient(build_node_console_app(config), base_url=ORIGIN) as client,
    ):
        assert client.get(PREFIX + "/trading-settings").status_code == 401
        login = client.post(PREFIX + "/session", json={"password": password}, headers=HEADERS)
        csrf = login.json()["csrf_token"]
        headers = {**HEADERS, "x-csrf-token": csrf}
        settings = {
            "buy": {
                "order_method": "ASK1_PROTECTED_LIMIT",
                "max_daily_buy_value": "50000",
                "max_symbol_buy_value": "10000",
                "max_order_value": "5000",
                "cancel_after_seconds": 10,
                "max_retries": 3,
            },
            "sell": {
                "take_profit_pct": "5",
                "stop_loss_pct": "3",
                "sell_by": "14:30:00",
            },
            "safety": {
                "max_daily_loss": "2000",
                "max_positions": 5,
                "emergency_liquidation_enabled": True,
            },
            "advanced": {
                "max_order_quantity": 10000,
                "max_chase_bps": 30,
                "max_spread_bps": 20,
                "quote_max_age_ms": 1000,
            },
        }
        body = {"expected_version": 0, "settings": settings}
        denied = client.post(PREFIX + "/trading-settings", json=body, headers=HEADERS)
        assert denied.status_code == 401
        saved = client.post(PREFIX + "/trading-settings", json=body, headers=headers)
        assert saved.status_code == 200
        assert saved.json()["draft"]["version"] == 1
        assert saved.json()["buy_configured"] is True
        assert saved.json()["active"] is False
        assert client.get(PREFIX + "/trading-settings").json()["draft"]["settings"] == settings
        conflict = client.post(PREFIX + "/trading-settings", json=body, headers=headers)
        assert conflict.status_code == 409
        invalid = deepcopy(settings)
        cast(dict[str, Any], invalid["buy"])["max_symbol_buy_value"] = "0"
        rejected = client.post(
            PREFIX + "/trading-settings",
            json={"expected_version": 1, "settings": invalid},
            headers=headers,
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "INVALID_ARGUMENTS"
        assert "三个买入金额" in rejected.json()["issues"][0]["message"]
        assert client.get(PREFIX + "/trading-settings").json()["draft"]["version"] == 1


def test_engine_control_checks_session_itself(access: Access) -> None:
    with auth_helpers.prepared(access) as (auth, control, _):
        payloads: tuple[dict[str, JsonValue], ...] = (
            {},
            {"session_token": "x", "csrf_token": "y"},
            {"session_token": 1, "csrf_token": "x"},
        )
        for payload in payloads:
            with pytest.raises(IPCError):
                control.invoke("OPERATOR_HALT", payload)
        with pytest.raises(IPCError, match="INVALID_ARGUMENTS"):
            auth.invoke("SESSION", {"session_token": "x"})
        proof = auth_helpers.login(auth, access[3])
        assert auth.invoke("SESSION", proof)["actor_id"] == "test-operator"
        auth.invoke("LOGOUT", proof)
        with pytest.raises(IPCError, match="OPERATOR_SESSION"):
            control.invoke("OPERATOR_RECOVER", proof)


def test_bridge_capacity_has_separate_emergency_lane(
    node_fixture: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = NodeConsoleBridge(node_fixture[0])

    class Client:
        def invoke(self, action: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            return {"action": action}

    monkeypatch.setattr(bridge, "_client", lambda *_: Client())
    for _ in range(2):
        assert bridge._gates["authorization"].acquire(False)
    with pytest.raises(IPCError, match="CONSOLE_BUSY"):
        bridge.invoke("authorization", "LOGIN", {})
    assert bridge._gates["query"].acquire(False)
    with pytest.raises(IPCError, match="CONSOLE_BUSY"):
        bridge.invoke("query", "OPERATOR_DETAILS", {})
    assert bridge.invoke("control", "OPERATOR_HALT", {})["action"] == "OPERATOR_HALT"


@pytest.mark.parametrize(
    "code,status",
    [
        ("OPERATOR_SESSION_INVALID", 401),
        ("OPERATOR_CSRF_REQUIRED", 403),
        ("CONSOLE_BUSY", 429),
        ("OPERATOR_LOGIN_RATE_LIMITED", 429),
        ("PIPE_UNAVAILABLE", 503),
        ("INVALID_ARGUMENTS", 400),
        ("PREVIEW_EXPIRED_OR_FENCED", 409),
        ("TRADING_SETTINGS_REQUIRED", 409),
        ("raw secret invalid error", 503),
    ],
)
def test_error_codes_have_safe_http_mapping(code: str, status: int) -> None:
    result = node_app._error(code)
    assert result.status_code == status
    assert b"raw secret" not in result.body


def test_console_cli_binding_and_missing_assets(
    node_fixture: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def serve(app: object, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", serve)
    assert main(["--config", str(node_fixture[1]), "console", "--port", "8781"]) == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8781
    assert captured["proxy_headers"] is False and captured["limit_concurrency"] == 32
    assert main(["--config", str(node_fixture[1]), "console", "--port", "80"]) == 2
    assert "NODE_COMMAND_FAILED" in capsys.readouterr().out
    with pytest.raises(LocalConsoleAssetError):
        build_node_console_app(node_fixture[0], asset_root=tmp_path)


def test_bridge_errors_are_redacted_and_cached_status_is_nonblocking(
    node_fixture: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = NodeConsoleBridge(node_fixture[0])

    def broken(*args: object) -> dict[str, JsonValue]:
        raise OSError("sensitive-credential-or-path")

    monkeypatch.setattr(bridge, "invoke", broken)
    # A failed mutation must not prevent a fast read of the separate status cache.
    with TestClient(
        build_node_console_app(node_fixture[0], bridge=bridge), base_url=ORIGIN
    ) as client:
        response = client.post(PREFIX + "/session", json={"password": "test"}, headers=HEADERS)
        assert response.status_code == 503 and "sensitive" not in response.text
        assert client.get(PREFIX + "/status").status_code == 200


def test_authenticated_halt_does_not_wait_for_owner_preview(
    access: Access, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = EngineKernel.preview_control

    def stalled(self: EngineKernel, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    with auth_helpers.prepared(access) as (auth, control, _):
        proof = auth_helpers.login(auth, access[3])
        monkeypatch.setattr(EngineKernel, "preview_control", stalled)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(auth_helpers.preview, auth, proof)
            try:
                assert entered.wait(2)
                start = monotonic()
                assert control.invoke("OPERATOR_HALT", proof)["halt_latched"] is True
                assert monotonic() - start < 1
            finally:
                release.set()
            # The halt may fence the queued preview; it never grants authority.
            with suppress(IPCError):
                future.result(timeout=5)
