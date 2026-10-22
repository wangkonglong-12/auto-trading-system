"""V2 canonical JSON: NFC strings, integer quantities and decimal text only.

Reject duplicate keys (including keys that collide after NFC normalization) and
binary floating point. Never silently change a signed amount or discard a field.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import cast

from pydantic import JsonValue


def _normalize(value: JsonValue, depth: int = 0) -> JsonValue:
    if depth > 32:
        raise ValueError("JSON nesting exceeds 32 levels")
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(item, depth + 1) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            normalized = unicodedata.normalize("NFC", key)
            if normalized in result:
                raise ValueError("duplicate normalized JSON key")
            result[normalized] = _normalize(item, depth + 1)
        return result
    raise ValueError("V2 numbers use integers or plain decimal strings, never floats")


def canonical_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_bytes(data: bytes, *, max_bytes: int = 512 * 1024) -> JsonValue:
    if len(data) > max_bytes:
        raise ValueError("JSON body exceeds byte limit")
    try:
        value = cast(JsonValue, json.loads(data, object_pairs_hook=_unique_object))
        return _normalize(value)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid or excessive JSON encoding") from exc
