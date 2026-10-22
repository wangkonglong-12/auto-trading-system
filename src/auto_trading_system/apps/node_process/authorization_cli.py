"""Prompt-only operator workflow; passwords/tokens never become CLI arguments.

The preview is safe to display and includes the exact scope, limits and expiry.
The session stays in memory for this invocation and is logged out on every exit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from time import monotonic, sleep

from pydantic import JsonValue

from auto_trading_system.control.ipc_protocol import IPCError, RPCClient
from auto_trading_system.control.node_authorization import PreviewProposal


@contextmanager
def operator_session(
    client: RPCClient,
    password_reader: Callable[[str], str],
) -> Iterator[dict[str, JsonValue]]:
    login = client.invoke("LOGIN", {"password": password_reader("Operator password: ")})
    token, csrf = login.get("session_token"), login.get("csrf_token")
    if not isinstance(token, str) or not isinstance(csrf, str):
        raise IPCError("INVALID_LOGIN_RESPONSE")
    credentials: dict[str, JsonValue] = {"session_token": token, "csrf_token": csrf}
    try:
        yield credentials
    finally:
        # A lost logout acknowledgement cannot change a durable control result.
        # Tokens are not saved and expire/revoke with the process-local session.
        with suppress(IPCError, OSError):
            client.invoke("LOGOUT", credentials)


def query_result(
    client: RPCClient,
    request_id: str,
    *,
    password_reader: Callable[[str], str],
    output: Callable[[dict[str, JsonValue]], None],
) -> int:
    with operator_session(client, password_reader) as credentials:
        result = client.invoke("CONTROL_RESULT", {**credentials, "request_id": request_id})
        output(result)
        return 0 if result.get("status") == "APPLIED" else 1


def authorize(
    client: RPCClient,
    proposal: PreviewProposal,
    *,
    password_reader: Callable[[str], str],
    confirmation_reader: Callable[[str], str],
    output: Callable[[dict[str, JsonValue]], None],
) -> int:
    with operator_session(client, password_reader) as credentials:
        preview = client.invoke(
            "PREVIEW_CONTROL", {**credentials, **proposal.model_dump(mode="json")}
        )
        identity, phrase, request = (
            preview.get("preview_id"),
            preview.get("confirmation_phrase"),
            preview.get("request"),
        )
        if (
            not isinstance(identity, str)
            or not isinstance(phrase, str)
            or not isinstance(request, dict)
            or not isinstance(request.get("request_id"), str)
        ):
            raise IPCError("INVALID_PREVIEW_RESPONSE")
        output(preview)
        answer = confirmation_reader("Type the exact confirmation phrase (empty cancels): ")
        if answer != phrase:
            output({"status": "CANCELLED", "real_trading_enabled": False})
            return 1
        result = client.invoke(
            "CONFIRM_CONTROL", {**credentials, "preview_id": identity, "phrase": answer}
        )
        until = monotonic() + 8
        while result.get("status") == "PENDING" and monotonic() < until:
            sleep(0.05)
            result = client.invoke(
                "CONTROL_RESULT", {**credentials, "request_id": request["request_id"]}
            )
        output(result)
        return 0 if result.get("status") == "APPLIED" else 1
