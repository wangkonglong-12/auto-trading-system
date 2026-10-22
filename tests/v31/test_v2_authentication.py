from __future__ import annotations

import pytest

from auto_trading_system.protocol.v2.authentication import (
    RequestIdentity,
    canonical_target,
    request_preimage,
    response_preimage,
    sign_message,
    verify_request,
)


def test_request_signature_binds_all_fields_and_clock_window() -> None:
    identity = RequestIdentity("test-node", "test-key", 1000, "test-nonce", "test-request")
    target = "/api/v2/trading/plans/current?strategy_id=tail&account_id=simulation"
    secret = bytes(range(32))  # Public deterministic test key; never a runtime credential.
    signature = sign_message(secret, request_preimage("GET", target, identity, b""))
    verify_request(
        secret,
        signature,
        method="GET",
        target=target,
        identity=identity,
        body=b"",
        now_utc_seconds=1030,
    )
    with pytest.raises(ValueError, match="clock"):
        verify_request(
            secret,
            signature,
            method="GET",
            target=target,
            identity=identity,
            body=b"",
            now_utc_seconds=1031,
        )
    with pytest.raises(ValueError, match="signature"):
        verify_request(
            secret,
            signature,
            method="POST",
            target=target,
            identity=identity,
            body=b"",
            now_utc_seconds=1000,
        )
    assert (
        sign_message(secret, response_preimage(identity.request_id, 200, 1000, b"{}")) != signature
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://host/api/v2/trading/plans/current",
        "/api/v1/trading/plans",
        "/api/v2/trading/%2e%2e/plans",
        "/api/v2/trading/a/../b",
        "/api/v2/trading/plans/current?a=1&a=2",
        "/api/v2/trading/plans/current#fragment",
    ],
)
def test_ambiguous_signing_targets_are_rejected(target: str) -> None:
    with pytest.raises(ValueError):
        canonical_target(target)


def test_canonical_query_encoding_and_invalid_identity() -> None:
    assert canonical_target("/api/v2/trading/plans/current?b=hello+world&a=1").endswith(
        "?a=1&b=hello%20world"
    )
    with pytest.raises(ValueError):
        RequestIdentity("bad\nnode", "key", 1, "nonce", "request")
    with pytest.raises(ValueError):
        sign_message(b"short", b"message")
    with pytest.raises(ValueError):
        request_preimage("DELETE", "/api/v2/trading/a", RequestIdentity("n", "k", 1, "n", "r"), b"")
