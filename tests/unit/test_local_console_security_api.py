from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from auto_trading_system.apps.local_console import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    LocalConsoleSecurityController,
    build_local_console_security_router,
)
from auto_trading_system.config.models import EnvironmentProfile
from auto_trading_system.control import LocalControlImpactFacts, LocalControlPreviewManager
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.runtime import RuntimeState
from auto_trading_system.security import (
    LocalOperatorSessionManager,
    OperatorPasswordHasher,
    OperatorSessionPolicy,
)

ORIGIN = "http://127.0.0.1:8765"
PASSWORD = "Correct-Horse-Battery-2026"
VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


@dataclass
class MutableClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00")
    )
    elapsed_ns: int = 0

    def now(self) -> ShanghaiTimestamp:
        return self.value

    def monotonic_ns(self) -> int:
        return self.elapsed_ns


@dataclass
class FactSource:
    value: LocalControlImpactFacts = field(
        default_factory=lambda: LocalControlImpactFacts(
            RuntimeState.AUTO,
            2,
            1,
            1,
            3,
            True,
            True,
            True,
            False,
        )
    )

    def __call__(self) -> LocalControlImpactFacts:
        return self.value


class StubPasswordHasher(OperatorPasswordHasher):
    def verify(self, password: str, verifier: str) -> bool:
        return password == PASSWORD and verifier == VERIFIER


def security_controller(
    clock: MutableClock,
    source: FactSource,
) -> LocalConsoleSecurityController:
    sessions = LocalOperatorSessionManager(
        SecretStr(VERIFIER),
        actor_id="operator_demo",
        clock=clock,
        monotonic_clock=clock,
        policy=OperatorSessionPolicy(max_failures=3),
        password_hasher=StubPasswordHasher(),
    )
    previews = LocalControlPreviewManager(
        node_id="node-dev-01",
        account_id="acct_demo_001",
        account_alias="demo",
        profile=EnvironmentProfile.READ_ONLY,
        clock=clock,
        monotonic_clock=clock,
        facts_supplier=source,
    )
    return LocalConsoleSecurityController(
        sessions,
        previews,
        expected_origin=ORIGIN,
        session_max_age_seconds=3600,
    )


def api_client() -> tuple[TestClient, FactSource]:
    source = FactSource()
    app = FastAPI()
    app.include_router(
        build_local_console_security_router(security_controller(MutableClock(), source))
    )
    return (
        TestClient(
            app,
            base_url="http://127.0.0.1",
            headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"},
        ),
        source,
    )


def login(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/local/session", json={"password": PASSWORD})
    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    csrf_token = body["csrf_token"]
    assert isinstance(csrf_token, str)
    client.headers[CSRF_HEADER_NAME] = csrf_token
    return body


def test_login_cookie_session_lookup_and_logout_are_bounded() -> None:
    client, _ = api_client()

    login_response = client.post("/api/v1/local/session", json={"password": PASSWORD})
    assert login_response.status_code == 200
    body = login_response.json()
    client.headers[CSRF_HEADER_NAME] = body["csrf_token"]

    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None
    set_cookie = login_response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Max-Age=3600" in set_cookie
    assert body["actor_id"] == "operator_demo"
    assert isinstance(body["csrf_token"], str)

    current = client.get("/api/v1/local/session")
    assert current.status_code == 200
    assert current.json()["actor_id"] == "operator_demo"
    assert current.json()["csrf_token"] is None

    logged_out = client.delete("/api/v1/local/session")
    assert logged_out.status_code == 204
    assert logged_out.content == b""
    assert client.get("/api/v1/local/session").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "", "sec-fetch-site": "same-origin"},
        {"origin": "http://127.0.0.1:9999", "sec-fetch-site": "same-origin"},
        {"origin": ORIGIN, "sec-fetch-site": "cross-site"},
    ],
)
def test_login_requires_exact_loopback_same_origin(headers: dict[str, str]) -> None:
    client, _ = api_client()

    response = client.post(
        "/api/v1/local/session",
        json={"password": PASSWORD},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "OPERATOR_ORIGIN_INVALID"


def test_failed_login_is_generic_then_rate_limited_without_secret_leakage() -> None:
    client, _ = api_client()
    wrong = "Definitely-Wrong-Password-2026"

    for _ in range(3):
        rejected = client.post("/api/v1/local/session", json={"password": wrong})
        assert rejected.status_code == 401
        assert rejected.json()["detail"]["code"] == "OPERATOR_LOGIN_FAILED"
        assert wrong not in rejected.text

    limited = client.post("/api/v1/local/session", json={"password": PASSWORD})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "300"


def test_preview_confirmation_returns_only_server_authored_command_fields() -> None:
    client, _ = api_client()
    login(client)

    missing_csrf = client.post(
        "/api/v1/local/control-previews",
        json={"action": "PAUSE_BUY", "reason_code": "OPERATOR_REQUEST"},
        headers={CSRF_HEADER_NAME: ""},
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "OPERATOR_CSRF_REQUIRED"

    preview_response = client.post(
        "/api/v1/local/control-previews",
        json={
            "action": "EMERGENCY_HALT",
            "reason_code": "LOCAL_EMERGENCY_HALT",
            "cancel_active_orders": True,
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["account_id"] == "acct_demo_001"
    assert preview["expected_state"] == "AUTO"
    assert preview["facts"]["active_orders"] == 2
    assert preview["impact_hash"].startswith("sha256:")

    wrong = client.post(
        f"/api/v1/local/control-previews/{preview['preview_id']}/confirm",
        json={"phrase": "确认其他操作"},
    )
    assert wrong.status_code == 403

    confirmed = client.post(
        f"/api/v1/local/control-previews/{preview['preview_id']}/confirm",
        json={"phrase": preview["confirmation_phrase"]},
    )
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["authorization_token"]
    assert result["command_template"]["actor_id"] == "operator_demo"
    assert result["command_template"]["confirmation"]["impact_hash"] == preview["impact_hash"]
    assert "command_id" not in result["command_template"]

    repeated = client.post(
        f"/api/v1/local/control-previews/{preview['preview_id']}/confirm",
        json={"phrase": preview["confirmation_phrase"]},
    )
    assert repeated.status_code == 409


def test_confirmation_rejects_stale_impact_and_request_models_are_strict() -> None:
    client, source = api_client()
    login(client)
    preview = client.post(
        "/api/v1/local/control-previews",
        json={"action": "PAUSE_BUY", "reason_code": "OPERATOR_REQUEST"},
    ).json()
    source.value = replace(source.value, open_positions=4)

    stale = client.post(
        f"/api/v1/local/control-previews/{preview['preview_id']}/confirm",
        json={"phrase": preview["confirmation_phrase"]},
    )
    strict = client.post(
        "/api/v1/local/control-previews",
        json={
            "action": "PAUSE_BUY",
            "reason_code": "OPERATOR_REQUEST",
            "unexpected": "value",
        },
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "CONTROL_PREVIEW_STALE"
    assert strict.status_code == 422


def test_security_controller_rejects_non_loopback_origin_and_unsafe_cookie_age() -> None:
    clock = MutableClock()
    source = FactSource()
    sessions = LocalOperatorSessionManager(
        SecretStr(VERIFIER),
        actor_id="operator_demo",
        clock=clock,
        monotonic_clock=clock,
        password_hasher=StubPasswordHasher(),
    )
    previews = LocalControlPreviewManager(
        node_id="node-dev-01",
        account_id="acct_demo_001",
        account_alias="demo",
        profile=EnvironmentProfile.READ_ONLY,
        clock=clock,
        monotonic_clock=clock,
        facts_supplier=source,
    )

    with pytest.raises(ValueError, match="loopback"):
        LocalConsoleSecurityController(
            sessions,
            previews,
            expected_origin="https://example.test",
            session_max_age_seconds=3600,
        )
    with pytest.raises(ValueError, match="session_max_age"):
        LocalConsoleSecurityController(
            sessions,
            previews,
            expected_origin=ORIGIN,
            session_max_age_seconds=100,
        )
