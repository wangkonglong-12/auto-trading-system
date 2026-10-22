"""Canonical JSON and digest rules shared by both projects."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any


def canonical_json_bytes(payload: Mapping[str, Any], *, exclude: frozenset[str]) -> bytes:
    """Serialize a top-level mapping with the protocol's deterministic JSON rules."""
    normalized = {key: value for key, value in payload.items() if key not in exclude}
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return serialized.encode("utf-8")


def calculate_plan_hash(payload: Mapping[str, Any]) -> str:
    """Calculate the TradePlan digest while excluding its digest field."""
    digest = hashlib.sha256(canonical_json_bytes(payload, exclude=frozenset({"plan_hash"})))
    return f"sha256:{digest.hexdigest()}"


def verify_plan_hash(payload: Mapping[str, Any]) -> bool:
    """Compare the supplied and calculated plan hashes in constant time."""
    supplied = payload.get("plan_hash")
    return isinstance(supplied, str) and hmac.compare_digest(supplied, calculate_plan_hash(payload))


def calculate_event_hash(payload: Mapping[str, Any]) -> str:
    """Calculate an immutable domain-event digest while excluding its digest field."""
    digest = hashlib.sha256(canonical_json_bytes(payload, exclude=frozenset({"event_hash"})))
    return f"sha256:{digest.hexdigest()}"


def verify_event_hash(payload: Mapping[str, Any]) -> bool:
    """Compare the supplied and calculated event hashes in constant time."""
    supplied = payload.get("event_hash")
    return isinstance(supplied, str) and hmac.compare_digest(
        supplied, calculate_event_hash(payload)
    )
