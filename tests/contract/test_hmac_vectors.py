from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from auto_trading_system.protocol import (
    InMemoryNonceStore,
    MappingKeyResolver,
    canonical_request_string,
    canonical_response_string,
    sign_request,
    sign_response,
    verify_request,
    verify_response,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = PROJECT_ROOT / "contracts" / "v1"
VECTOR_PATH = CONTRACT_ROOT / "examples" / "security" / "hmac-sha256-v1.vector.json"
SCHEMA_PATH = CONTRACT_ROOT / "signature-test-vector.schema.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def test_hmac_vector_matches_schema_and_independent_digest() -> None:
    schema = load_json(SCHEMA_PATH)
    vector = load_json(VECTOR_PATH)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(vector)

    secret = vector["secret_utf8"].encode("utf-8")
    for message_type in ("request", "response"):
        expected = hmac.new(
            secret,
            vector[message_type]["canonical"].encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert vector[message_type]["headers"]["X-ATS-Signature"] == f"v1={expected}"


def test_request_vector_is_reproduced_and_verified() -> None:
    vector = load_json(VECTOR_PATH)
    request = vector["request"]
    secret = vector["secret_utf8"].encode("utf-8")
    body = request["body_utf8"].encode("utf-8")

    headers = sign_request(
        secret=secret,
        key_id=request["key_id"],
        timestamp=request["timestamp"],
        nonce=request["nonce"],
        request_id=request["request_id"],
        method=request["method"],
        request_target=request["request_target"],
        body=body,
    )

    assert headers == request["headers"]
    assert (
        canonical_request_string(
            key_id=request["key_id"],
            timestamp=request["timestamp"],
            nonce=request["nonce"],
            request_id=request["request_id"],
            method=request["method"],
            request_target=request["request_target"],
            content_sha256=headers["X-ATS-Content-SHA256"],
        )
        == request["canonical"]
    )
    verified = verify_request(
        headers=headers,
        method=request["method"],
        request_target=request["request_target"],
        body=body,
        key_resolver=MappingKeyResolver({request["key_id"]: secret}),
        nonce_store=InMemoryNonceStore(),
        now_epoch_seconds=request["timestamp"],
    )
    assert verified.request_id == request["request_id"]
    assert verified.nonce == request["nonce"]


def test_response_vector_is_request_bound_and_verified() -> None:
    vector = load_json(VECTOR_PATH)
    request = vector["request"]
    response = vector["response"]
    secret = vector["secret_utf8"].encode("utf-8")
    body = response["body_utf8"].encode("utf-8")

    headers = sign_response(
        secret=secret,
        key_id=response["key_id"],
        timestamp=response["timestamp"],
        request_nonce=request["nonce"],
        request_id=request["request_id"],
        status_code=response["status_code"],
        body=body,
    )

    assert headers == response["headers"]
    assert (
        canonical_response_string(
            key_id=response["key_id"],
            timestamp=response["timestamp"],
            request_nonce=request["nonce"],
            request_id=request["request_id"],
            status_code=response["status_code"],
            content_sha256=headers["X-ATS-Content-SHA256"],
        )
        == response["canonical"]
    )
    verified = verify_response(
        headers=headers,
        status_code=response["status_code"],
        body=body,
        expected_request_id=request["request_id"],
        expected_request_nonce=request["nonce"],
        key_resolver=MappingKeyResolver({response["key_id"]: secret}),
        now_epoch_seconds=response["timestamp"],
    )
    assert verified.request_id == request["request_id"]
    assert verified.request_nonce == request["nonce"]
