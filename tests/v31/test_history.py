import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from auto_trading_system.apps.local_console import history

TOKEN = b"test-history-integration-" + b"x" * 32


def console(monkeypatch, respond, scheme="http", authorize=None, token=TOKEN):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        history.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(history, "read_access_token", lambda root: token)
    app = FastAPI()

    async def local_session(request, restore):
        assert restore is True

    history.register_history(
        app,
        SimpleNamespace(
            quant_origin=f"{scheme}://quant.test:8001",
            quant_ca_file=None,
            account_id="account-a",
            strategy_id="TAIL_CLOSE",
            data_root=Path("test-node"),
        ),
        authorize or local_session,
    )
    return TestClient(app)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_proxy_uses_configured_api_token_and_fixed_scope_without_admin_login(monkeypatch, scheme):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/auto-trading/execution-history"
        assert request.url.scheme == scheme
        assert request.headers["authorization"] == "Bearer " + TOKEN.decode()
        return httpx.Response(200, json={"items": [], "has_more": False})

    with console(monkeypatch, respond, scheme) as client:
        # A previously cached admin cookie must not select authentication.
        client.cookies.set("ats_quant_history", "old-admin-jwt")
        reply = client.get(history.PATH + "?symbol=000001&buy_from=2026-10-01")
        assert reply.status_code == 200 and TOKEN.decode() not in reply.text
        assert dict(calls[-1].url.params) == {
            "symbol": "000001",
            "buy_from": "2026-10-01",
            "account_id": "account-a",
            "strategy_id": "TAIL_CLOSE",
        }
        assert "set-cookie" not in reply.headers
        for query in ("environment=TEST", "account_id=other", "strategy_id=other"):
            assert client.get(history.PATH + "?" + query).status_code == 400
        assert len(calls) == 1
        assert client.post(history.PATH + "/session", json={}).status_code == 404
        assert len(calls) == 1


def test_missing_api_token_does_not_request_admin_password_or_call_backend(monkeypatch):
    calls = []
    with console(monkeypatch, lambda request: calls.append(request), token=None) as client:
        reply = client.get(history.PATH)
        assert reply.status_code == 503
        assert reply.json()["code"] == "QUANT_ACCESS_REQUIRED"
        assert not calls


def test_local_session_is_still_required(monkeypatch):
    calls = []

    async def deny(request, restore):
        raise HTTPException(401)

    with console(monkeypatch, lambda request: calls.append(request), authorize=deny) as client:
        assert client.get(history.PATH).status_code == 401
        assert not calls


@pytest.mark.parametrize("status", [401, 403])
def test_backend_rejected_token_is_not_a_local_session_expiration(monkeypatch, status):
    with console(monkeypatch, lambda request: httpx.Response(status)) as client:
        reply = client.get(history.PATH)
        assert reply.status_code == 503
        assert reply.json()["code"] == "HISTORY_ACCESS_REJECTED"


def test_xlsx_export_and_errors_do_not_become_empty_success(monkeypatch):
    def respond(request):
        if request.url.params.get("export") == "true":
            return httpx.Response(200, content=b"PK-xlsx")
        return httpx.Response(422, json={"error": {"code": "EXPORT_TOO_LARGE"}})

    with console(monkeypatch, respond) as client:
        reply = client.get(history.PATH + "?export=true")
        assert reply.content == b"PK-xlsx"
        assert "spreadsheetml" in reply.headers["content-type"]
        reply = client.get(history.PATH)
        assert reply.status_code == 400
        assert json.loads(reply.content)["code"] == "EXPORT_TOO_LARGE"
