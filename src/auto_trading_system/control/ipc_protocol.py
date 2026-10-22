"""Authenticated local RPC, separate from HTTP credentials and trading grants.

Boot-bound requests expire in eight seconds. A retry of an identical request
returns the cached result; reuse with different content is rejected. Business
idempotence across process restarts remains in plan receipts / immutable jobs.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue

from auto_trading_system.protocol.v2.base import WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes

MAX_FRAME = 65536
Role = Literal["agent", "jobrunner", "operator"]


class IPCError(RuntimeError):
    """Fixed error codes only; never expose payloads, credentials or OS messages."""


class Request(WireModel):
    protocol_major: Literal[1] = Field(default=1, description="本地通信主版本")
    request_id: WireId = Field(description="一次调用身份；相同身份保持正文不变")
    engine_boot_id: WireId = Field(description="目标进程启动身份；hello仅用于握手")
    sender_role: Role = Field(description="由独立角色密钥认证的调用者角色")
    deadline_ms: int = Field(ge=1, description="UTC毫秒截止时间，上限为当前时间加8秒")
    action: WireId = Field(description="服务端按角色白名单分派的动作")
    payload: dict[str, JsonValue] = Field(default_factory=dict, description="动作参数")


class Response(WireModel):
    protocol_major: Literal[1] = 1
    request_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    engine_boot_id: WireId
    ok: bool
    code: WireId
    result: dict[str, JsonValue] = Field(default_factory=dict)


def sign_packet(body: dict[str, JsonValue], key: bytes, *, response: bool = False) -> bytes:
    if len(key) < 32:
        raise ValueError("IPC key must contain at least 32 bytes")
    domain = b"ATS/IPC/RESPONSE/1\n" if response else b"ATS/IPC/REQUEST/1\n"
    signature = hmac.new(key, domain + canonical_bytes(body), hashlib.sha256).hexdigest()
    packet = canonical_bytes({"body": body, "signature": signature})
    if len(packet) > MAX_FRAME:
        raise IPCError("FRAME_TOO_LARGE")
    return packet


def verify_packet(data: bytes, key: bytes, *, response: bool = False) -> dict[str, JsonValue]:
    value = parse_bytes(data, max_bytes=MAX_FRAME)
    if not isinstance(value, dict) or set(value) != {"body", "signature"}:
        raise IPCError("INVALID_PACKET")
    body, signature = value["body"], value["signature"]
    if (
        not isinstance(body, dict)
        or not isinstance(signature, str)
        or len(signature) != 64
        or not signature.isascii()
    ):
        raise IPCError("INVALID_PACKET")
    expected = parse_bytes(sign_packet(body, key, response=response))
    assert isinstance(expected, dict)
    if not hmac.compare_digest(signature, str(expected["signature"])):
        raise IPCError("AUTHENTICATION_FAILED")
    return body


@dataclass(frozen=True)
class _Result:
    fingerprint: str
    expires: int
    response: bytes


class RPCService:
    """One pipe worker owns this bounded replay cache and dispatches serially."""

    def __init__(
        self,
        boot_id: str,
        keys: Mapping[Role, bytes],
        permissions: Mapping[Role, frozenset[str]],
        handler: Callable[[Request], dict[str, JsonValue]],
        *,
        clock: Callable[[], float] = time,
        capacity: int = 512,
    ) -> None:
        if not 1 <= capacity <= 4096 or any(len(key) < 32 for key in keys.values()):
            raise ValueError("invalid IPC authentication budget")
        self.boot_id, self.keys, self.permissions = boot_id, dict(keys), dict(permissions)
        self.handler, self.clock, self.capacity = handler, clock, capacity
        self._results: dict[tuple[Role, str], _Result] = {}

    def handle(self, data: bytes) -> bytes:
        # Parsing a role selects a key, never a permission: authenticate first.
        outer = parse_bytes(data, max_bytes=MAX_FRAME)
        if not isinstance(outer, dict) or not isinstance(outer.get("body"), dict):
            raise IPCError("INVALID_PACKET")
        request = Request.model_validate_json(canonical_bytes(outer["body"]))
        key = self.keys.get(request.sender_role)
        if key is None:
            raise IPCError("ROLE_DENIED")
        verify_packet(data, key)
        now = int(self.clock() * 1000)
        if not now < request.deadline_ms <= now + 8000:
            raise IPCError("REQUEST_EXPIRED")
        if request.action not in self.permissions.get(request.sender_role, frozenset()):
            raise IPCError("ACTION_DENIED")
        if request.engine_boot_id != ("hello" if request.action == "HELLO" else self.boot_id):
            raise IPCError("BOOT_CHANGED")
        fingerprint = digest(canonical_bytes(request.model_dump(mode="json")))
        identity = (request.sender_role, request.request_id)
        self._results = {k: v for k, v in self._results.items() if v.expires > now}
        previous = self._results.get(identity)
        if previous:
            if previous.fingerprint != fingerprint:
                raise IPCError("REQUEST_ID_CONFLICT")
            return previous.response
        if len(self._results) >= self.capacity:
            raise IPCError("REPLAY_CACHE_FULL")
        try:
            result = {} if request.action == "HELLO" else self.handler(request)
            reply = Response(
                request_hash=fingerprint,
                engine_boot_id=self.boot_id,
                ok=True,
                code="OK",
                result=result,
            )
        except IPCError as exc:
            reply = Response(
                request_hash=fingerprint,
                engine_boot_id=self.boot_id,
                ok=False,
                code=str(exc),
            )
        except (ValueError, OSError, RuntimeError):
            reply = Response(
                request_hash=fingerprint,
                engine_boot_id=self.boot_id,
                ok=False,
                code="OPERATION_REJECTED",
            )
        encoded = sign_packet(reply.model_dump(mode="json"), key, response=True)
        self._results[identity] = _Result(fingerprint, request.deadline_ms, encoded)
        return encoded


class RPCClient:
    def __init__(
        self,
        exchange: Callable[[bytes], bytes],
        role: Role,
        key: bytes,
        *,
        clock: Callable[[], float] = time,
    ) -> None:
        self.exchange, self.role, self.key, self.clock = exchange, role, key, clock

    def invoke(
        self, action: str, payload: dict[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        boot = self._call("hello", "HELLO", {}).engine_boot_id
        return self._call(boot, action, payload or {}).result

    def _call(self, boot: str, action: str, payload: dict[str, JsonValue]) -> Response:
        request = Request(
            request_id=uuid4().hex,
            engine_boot_id=boot,
            sender_role=self.role,
            deadline_ms=int(self.clock() * 1000) + 7500,
            action=action,
            payload=payload,
        )
        body = request.model_dump(mode="json")
        raw = self.exchange(sign_packet(body, self.key))
        response = Response.model_validate_json(
            canonical_bytes(verify_packet(raw, self.key, response=True))
        )
        if response.request_hash != digest(canonical_bytes(body)) or (
            boot != "hello" and response.engine_boot_id != boot
        ):
            raise IPCError("RESPONSE_BINDING_FAILED")
        if int(self.clock() * 1000) >= request.deadline_ms:
            raise IPCError("RESPONSE_EXPIRED")
        if not response.ok:
            raise IPCError(response.code)
        return response
