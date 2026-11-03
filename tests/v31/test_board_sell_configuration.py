"""Independent saved sell configuration, never trading execution."""

import json

import pytest
import test_node_processes as node_helpers
from fastapi.testclient import TestClient

from auto_trading_system.apps.local_console import node_app
from auto_trading_system.apps.local_console.node_app import build_node_console_app
from auto_trading_system.apps.local_console.node_bridge import NodeConsoleBridge
from auto_trading_system.strategies.board_sell_configuration import (
    BoardSellConfiguration,
    BoardSellConfigurationError,
    BoardSellConfigurationStore,
)

node_fixture = node_helpers.node
ORIGIN = "http://127.0.0.1:8780"
PATH = "/api/v2/node/sell-configuration"
HEADERS = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-csrf-token": "local-csrf"}


def test_login_forwards_automatic_sell_intent_only_when_requested(node_fixture, monkeypatch):
    config = node_fixture[0]
    bridge = NodeConsoleBridge(config)
    calls = []

    def invoke(channel, action, payload):
        calls.append((channel, action, payload))
        return {"session_token": "local-session", "csrf_token": "local-csrf"}

    monkeypatch.setattr(bridge, "invoke", invoke)
    with TestClient(build_node_console_app(config, bridge=bridge), base_url=ORIGIN) as client:
        path = "/api/v2/node/session"
        assert (
            client.post(
                path, json={"password": "test", "start_board_sell": True}, headers=HEADERS
            ).status_code
            == 200
        )
        assert calls[-1] == (
            "authorization",
            "LOGIN",
            {"password": "test", "start_board_sell": True},
        )
        assert client.post(path, json={"password": "test"}, headers=HEADERS).status_code == 200
        assert calls[-1][2] == {"password": "test"}
        count = len(calls)
        assert (
            client.post(
                path, json={"password": "test", "start_board_sell": "true"}, headers=HEADERS
            ).status_code
            == 400
        )
        assert len(calls) == count


def test_authenticated_save_is_independent_and_survives_reload(node_fixture, monkeypatch):
    config = node_fixture[0]
    bridge = NodeConsoleBridge(config)
    calls = []

    def invoke(channel, action, payload):
        calls.append((channel, action))
        assert channel == "authorization" and action == "SESSION"
        assert payload["session_token"] == "local-session"
        return {}

    monkeypatch.setattr(bridge, "invoke", invoke)
    untouched = [
        config.data_root / "operator/trading_settings.json",
        config.data_root / "critical/daily-auto-consent.json",
    ]
    for p in untouched:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"original-buy-configuration")
    with TestClient(build_node_console_app(config, bridge=bridge), base_url=ORIGIN) as client:
        assert client.get(PATH).status_code == 401
        client.cookies.set(node_app.SESSION_COOKIE, "local-session")
        client.cookies.set(node_app.CSRF_COOKIE, "local-csrf")
        initial = client.get(PATH).json()
        assert initial["draft"] is None and initial["execution_supported"] is True
        assert not BoardSellConfigurationStore(config.data_root).path.exists()
        values = initial["defaults"]
        values["chinext"] = {
            "mode": "TAKE_PROFIT_OR_TIME",
            "take_profit_pct": "12.50",
            "sell_at": "10:50",
        }
        body = {"expected_version": 0, "settings": values}
        assert (
            client.post(
                PATH, json=body, headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"}
            ).status_code
            == 401
        )
        result = client.post(PATH, json=body, headers=HEADERS)
        assert result.status_code == 200 and result.json()["draft"]["version"] == 1
        assert not result.json()["save_enables_execution"]
        assert client.post(PATH, json=body, headers=HEADERS).status_code == 409
        assert (
            client.post(
                PATH, json=body, headers={**HEADERS, "origin": "https://other.test"}
            ).status_code
            == 403
        )
        assert client.get(PATH).json()["draft"]["settings"] == values
    restored = BoardSellConfigurationStore(config.data_root).read()
    assert restored.settings.main.take_profit_pct == "7.00"
    assert restored.settings.chinext.take_profit_pct == "12.50"
    assert all(p.read_bytes() == b"original-buy-configuration" for p in untouched)
    assert calls and all(action == "SESSION" for _, action in calls)


@pytest.mark.parametrize(
    "change",
    [
        "repost-negative",
        "repost-large",
        "repost-fraction",
        "zero",
        "large",
        "precision",
        "missing",
        "unused",
        "lunch",
        "late",
        "seconds",
        "enable",
        "extra",
        "mode",
    ],
)
def test_invalid_settings_do_not_create_draft(tmp_path, change):
    body = BoardSellConfiguration().model_dump(mode="json")
    if change.startswith("repost-"):
        body["max_reposts"] = {"repost-negative": -1, "repost-large": 101, "repost-fraction": 1.5}[
            change
        ]
    elif change == "zero":
        body["main"]["take_profit_pct"] = "0"
    elif change == "large":
        body["main"]["take_profit_pct"] = "100.01"
    elif change == "precision":
        body["main"]["take_profit_pct"] = "1.234"
    elif change == "missing":
        body["main"]["take_profit_pct"] = None
    elif change == "unused":
        body["chinext"]["take_profit_pct"] = "5"
    elif change == "lunch":
        body["main"]["sell_at"] = "12:00"
    elif change == "late":
        body["chinext"]["sell_at"] = "14:51"
    elif change == "seconds":
        body["cancel_after_seconds"] = 0
    elif change == "enable":
        body["enabled"] = True
    elif change == "mode":
        body["chinext"]["mode"] = "UNKNOWN"
    else:
        body["scope"] = "all-account-positions"
    with pytest.raises(ValueError):
        BoardSellConfiguration.model_validate_json(json.dumps(body))
    assert not BoardSellConfigurationStore(tmp_path).path.exists()


def test_store_preserves_last_version_on_failure_and_rejects_corruption(tmp_path, monkeypatch):
    store = BoardSellConfigurationStore(tmp_path)
    saved = store.save(BoardSellConfiguration(), 0)
    original = store.path.read_bytes()

    def fail(*_):
        raise OSError("test disk failure")

    monkeypatch.setattr("auto_trading_system.strategies.board_sell_configuration.os.replace", fail)
    with pytest.raises(BoardSellConfigurationError, match="SAVE_FAILED"):
        store.save(saved.settings, 1)
    assert store.path.read_bytes() == original and store.read() == saved
    assert not list(store.path.parent.glob("*.tmp"))
    store.path.write_text("invalid", encoding="utf-8")
    with pytest.raises(BoardSellConfigurationError, match="STORE_INVALID"):
        store.read()


def test_engine_endpoints_require_session_csrf_and_exact_board(node_fixture, monkeypatch):
    config = node_fixture[0]
    bridge = NodeConsoleBridge(config)
    calls = []

    def invoke(channel, action, payload):
        calls.append((action, payload))
        return {"supported": True} if action != "SESSION" else {}

    monkeypatch.setattr(bridge, "invoke", invoke)
    with TestClient(build_node_console_app(config, bridge=bridge), base_url=ORIGIN) as client:
        route = "/api/v2/node/sell-engines"
        assert client.get(route).status_code == 401
        assert (
            client.post(route + "/start", json={"board": "MAIN"}, headers=HEADERS).status_code
            == 401
        )
        client.cookies.set(node_app.SESSION_COOKIE, "local-session")
        client.cookies.set(node_app.CSRF_COOKIE, "local-csrf")
        assert client.get(route).status_code == 200
        assert calls[-1][0] == "BOARD_SELL_STATUS"
        assert client.post(route + "/start", json={"board": "MAIN"}).status_code in {401, 403}
        assert (
            client.post(route + "/start", json={"board": "MAIN"}, headers=HEADERS).status_code
            == 200
        )
        assert calls[-1][0] == "START_BOARD_SELL"
        assert calls[-1][1]["session_token"] == "local-session"
        assert (
            client.post(route + "/start", json={"board": "STAR"}, headers=HEADERS).status_code
            == 400
        )
        assert client.post(route + "/prepare", json={"board": "MAIN"}).status_code in {401, 403}
        assert (
            client.post(route + "/prepare", json={"board": "MAIN"}, headers=HEADERS).status_code
            == 200
        )
        assert calls[-1][0] == "PREPARE_BOARD_SELL"
        assert calls[-1][1]["board"] == "MAIN"
        assert (
            client.post(route + "/pause", json={"board": "CHINEXT"}, headers=HEADERS).status_code
            == 200
        )
        assert calls[-1][0] == "PAUSE_BOARD_SELL"
        assert (
            client.post(route + "/prepare", json={"board": "STAR"}, headers=HEADERS).status_code
            == 400
        )
        assert (
            client.post(
                route + "/prepare", json={"board": "MAIN", "grant": {}}, headers=HEADERS
            ).status_code
            == 400
        )
        assert (
            client.post(route + "/unknown", json={"board": "MAIN"}, headers=HEADERS).status_code
            == 400
        )
        assert (
            client.post(
                route + "/pause",
                json={"board": "MAIN"},
                headers={**HEADERS, "origin": "https://other.test"},
            ).status_code
            == 403
        )
