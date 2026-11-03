import test_node_processes as helpers
from fastapi.testclient import TestClient

from auto_trading_system.apps.local_console import node_app
from auto_trading_system.apps.local_console.node_bridge import NodeConsoleBridge
from auto_trading_system.control.ipc_protocol import IPCError

node_fixture = helpers.node
ORIGIN = "http://127.0.0.1:8780"
HEADERS = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-csrf-token": "csrf"}


def test_debug_routes_require_local_session_csrf_origin_and_reject_order_fields(
    node_fixture, monkeypatch
):
    config = node_fixture[0]
    bridge = NodeConsoleBridge(config)
    calls = []

    def invoke(channel, action, payload):
        if payload["csrf_token"] != "csrf":
            raise IPCError("OPERATOR_SESSION_INVALID")
        calls.append((channel, action, payload))
        return {"status": "STARTED"}

    monkeypatch.setattr(bridge, "invoke", invoke)
    with TestClient(
        node_app.build_node_console_app(config, bridge=bridge), base_url=ORIGIN
    ) as client:
        path = node_app.PREFIX + "/debug-pick/"
        assert (
            client.post(path + "preview", json={"symbol": "000001"}, headers=HEADERS).status_code
            == 401
        )
        client.cookies.set(node_app.SESSION_COOKIE, "session")
        client.cookies.set(node_app.CSRF_COOKIE, "csrf")
        assert (
            client.post(
                path + "preview",
                json={"symbol": "000001"},
                headers={**HEADERS, "x-csrf-token": "bad"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                path + "preview",
                json={"symbol": "000001"},
                headers={**HEADERS, "origin": "http://evil.test"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                path + "preview", json={"symbol": "000001", "quantity": 100}, headers=HEADERS
            ).status_code
            == 400
        )
        assert (
            client.post(path + "preview", json={"symbol": "000001"}, headers=HEADERS).status_code
            == 200
        )
        assert calls[-1][0:2] == ("authorization", "PREVIEW_DEBUG_PICK")
        assert calls[-1][2]["symbol"] == "000001.SZ"
        value = {"preview_id": "debug-" + "a" * 32, "phrase": "确认真实自动买入 000001.SZ"}
        assert (
            client.post(
                path + "confirm", json={**value, "max_buy_value": "9999999"}, headers=HEADERS
            ).status_code
            == 400
        )
        assert client.post(path + "confirm", json=value, headers=HEADERS).status_code == 200
        assert calls[-1][0:2] == ("authorization", "CONFIRM_DEBUG_PICK")
