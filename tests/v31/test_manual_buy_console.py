from __future__ import annotations

from typing import Any

import test_node_processes as node_helpers
from fastapi.testclient import TestClient

from auto_trading_system.apps.local_console import node_app
from auto_trading_system.apps.local_console.node_bridge import NodeConsoleBridge
from auto_trading_system.control.ipc_protocol import IPCError

node_fixture = node_helpers.node
ORIGIN = "http://127.0.0.1:8780"
HEADERS = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-csrf-token": "local-csrf"}


def test_manual_routes_require_session_csrf_origin_and_exact_frozen_fields(
    node_fixture: Any, monkeypatch: Any
) -> None:
    config = node_fixture[0]
    bridge = NodeConsoleBridge(config)
    calls: list[Any] = []

    def invoke(channel: Any, action: Any, payload: Any) -> Any:
        assert channel == "authorization"
        assert payload["session_token"] == "local-session"
        if payload["csrf_token"] != "local-csrf":
            raise IPCError("OPERATOR_SESSION_INVALID")
        calls.append((action, payload))
        return {"status": "PERSISTING"}

    monkeypatch.setattr(bridge, "invoke", invoke)
    with TestClient(
        node_app.build_node_console_app(config, bridge=bridge), base_url=ORIGIN
    ) as client:
        path = node_app.PREFIX + "/orders/manual-buy/"
        purchase = {"symbol": "000001", "limit_price": "10.00", "quantity": 100}
        assert client.post(path + "preview", json=purchase, headers=HEADERS).status_code == 401
        client.cookies.set(node_app.SESSION_COOKIE, "local-session")
        client.cookies.set(node_app.CSRF_COOKIE, "local-csrf")
        assert (
            client.post(
                path + "preview", json=purchase, headers={**HEADERS, "x-csrf-token": "wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                path + "preview", json=purchase, headers={**HEADERS, "origin": "http://other.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                path + "preview", json={**purchase, "max_buy_value": "999999"}, headers=HEADERS
            ).status_code
            == 400
        )
        assert client.post(path + "preview", json=purchase, headers=HEADERS).status_code == 200
        assert calls[-1][0] == "PREVIEW_MANUAL_BUY" and calls[-1][1]["symbol"] == "000001.SZ"
        confirmation = {"preview_id": "manual-" + "a" * 32}
        assert (
            client.post(
                path + "confirm", json={**confirmation, "quantity": 200}, headers=HEADERS
            ).status_code
            == 400
        )
        assert client.post(path + "confirm", json=confirmation, headers=HEADERS).status_code == 200
        assert calls[-1][0] == "CONFIRM_MANUAL_BUY"
        assert (
            client.post(
                path + "result", json={"client_order_id": "ord_manual0001"}, headers=HEADERS
            ).status_code
            == 200
        )
        assert calls[-1][0] == "MANUAL_BUY_RESULT"
