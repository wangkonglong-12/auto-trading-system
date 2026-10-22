"""Executable HTTP contract for the implemented plan and daily-sync endpoints.

Authentication verifies raw bytes before parsing. Manual request streaming must
not result in missing request-body documentation in the generated FastAPI spec.
Statement import is intentionally not advertised until its wire API is present.
"""

from __future__ import annotations

from typing import Any

from auto_trading_system.protocol.v2.api import (
    ApiError,
    CapabilityManifest,
    CompleteDaily,
    DailyProgress,
    PlanLookup,
    PlanReceipt,
)
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.daily_sync import AppliedReceipt, DailyManifest, DailyRecord
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.protocol.v2.records import RECORD_MODELS


def document() -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    models: tuple[type[WireModel], ...] = (
        ApiError,
        CapabilityManifest,
        CompleteDaily,
        DailyProgress,
        PlanLookup,
        PlanReceipt,
        AppliedReceipt,
        DailyManifest,
        DailyRecord,
        PlanEnvelope,
        *RECORD_MODELS.values(),
    )
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            if name in schemas and schemas[name] != definition:
                raise ValueError(f"conflicting schema: {name}")
            schemas[name] = definition
        schemas[model.__name__] = schema

    def reference(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    def json_content(schema: dict[str, Any]) -> dict[str, Any]:
        return {"application/json": {"schema": schema}}

    paths: dict[str, Any] = {}
    endpoints = (
        ("GET", "/manifest", None, "CapabilityManifest", 200),
        ("GET", "/plans/current", None, "PlanLookup", 200),
        ("GET", "/plans/{plan_id}", None, "PlanEnvelope", 200),
        ("POST", "/plans/{plan_id}/receipts", "PlanReceipt", "PlanReceipt", 200),
        ("POST", "/daily-syncs", "DailyManifest", "DailyManifest", 201),
        ("GET", "/daily-syncs/{batch_id}", None, "DailyProgress", 200),
        ("PUT", "/daily-syncs/{batch_id}/parts/{part_id}", "DailyRecord[]", "CompleteDaily", 200),
        ("POST", "/daily-syncs/{batch_id}/complete", "CompleteDaily", "AppliedReceipt", 200),
        ("GET", "/daily-syncs/{batch_id}/receipt", None, "AppliedReceipt", 200),
    )
    for method, path, request_model, response_model, status in endpoints:
        parameters = [
            {
                "name": segment[1:-1],
                "in": "path",
                "required": True,
                "schema": {"type": "string", "pattern": r"^[A-Za-z0-9_.:-]{1,96}$"},
            }
            for segment in path.split("/")
            if segment.startswith("{")
        ]
        if path == "/plans/current":
            parameters.extend(
                {"name": name, "in": "query", "required": True, "schema": schema}
                for name, schema in (
                    ("account_id", {"type": "string"}),
                    ("strategy_id", {"type": "string"}),
                    ("trade_date", {"type": "string", "format": "date"}),
                )
            )
            parameters.append(
                {"name": "If-None-Match", "in": "header", "schema": {"type": "string"}}
            )
        operation: dict[str, Any] = {
            "operationId": method.lower()
            + path.replace("/", "_").replace("{", "").replace("}", ""),
            "tags": ["自动交易V2"],
            "security": [{"NodeHmac": []}],
            "parameters": parameters,
            "responses": {
                str(status): {
                    "description": "Authenticated result",
                    "content": json_content(reference(response_model)),
                }
            },
            "x-maximum-request-bytes": 512 * 1024,
        }
        for code in (400, 401, 403, 404, 409, 413, 415, 422, 426, 429, 503):
            operation["responses"][str(code)] = {
                "description": (
                    "Signed error after authentication; "
                    "early transport/auth rejection may have no body"
                ),
                "content": json_content(reference("ApiError")),
            }
        if path == "/plans/current":
            operation["responses"].update(
                {
                    "204": {"description": "No current plan"},
                    "304": {"description": "Unchanged published plan"},
                }
            )
        if request_model is not None:
            body = (
                {"type": "array", "minItems": 1, "maxItems": 200, "items": reference("DailyRecord")}
                if request_model == "DailyRecord[]"
                else reference(request_model)
            )
            operation["requestBody"] = {"required": True, "content": json_content(body)}
        paths["/api/v2/trading" + path] = {method.lower(): operation}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Automatic trading V2 plan and daily sync", "version": "2.0"},
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "NodeHmac": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-ATS-Signature",
                    "description": (
                        "HMAC-SHA256 over method, canonical target, node/key, time, nonce, "
                        "request ID and body hash; 30s skew, 10min nonce retention. HTTPS required."
                    ),
                },
            },
        },
        "x-response-signing": "Request ID, status, timestamp and body hash are authenticated.",
        "x-dataset-schemas": {
            name: reference(model.__name__) for name, model in RECORD_MODELS.items()
        },
    }
