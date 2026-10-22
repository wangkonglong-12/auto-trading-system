"""Boot-local one-use action approvals, populated by an authenticated local adapter.

This store is not a login mechanism. A wire sender role or a supplied actor name
cannot mint an approval. The node authorization adapter authenticates and binds
an exact interactive preview before calling approve(); offline compositions use
only a named test actor.
Nothing is restored from an old boot, and a consumed ID cannot be reused.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import get_ident
from time import monotonic
from typing import Protocol

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest


class ApprovalPort(Protocol):
    def consume(self, request: LocalControlRequest) -> str: ...


@dataclass(frozen=True)
class _Approval:
    request_hash: str
    actor: str
    expires: float


class OneShotApprovals:
    def __init__(self, *, boot_id: str, clock: Callable[[], datetime], capacity: int = 256) -> None:
        if not 1 <= capacity <= 2048:
            raise ValueError("approval capacity invalid")
        self._owner = get_ident()
        self._boot = boot_id
        self._clock = clock
        self._capacity = capacity
        self._issued: dict[str, _Approval] = {}
        self._used: set[str] = set()

    def _owner_only(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("approval state has a single owner")

    def approve(self, request: LocalControlRequest, *, authenticated_actor: str) -> None:
        self._owner_only()
        key = request.operator_authorization_id
        if not key or request.engine_boot_id != self._boot or not authenticated_actor:
            raise ValueError("approval identity invalid")
        if request.grant and request.grant.confirmed_by != authenticated_actor:
            raise ValueError("grant actor differs from authenticated operator")
        if (
            key in self._issued
            or key in self._used
            or len(self._issued) + len(self._used) >= self._capacity
        ):
            raise ValueError("approval ID reused or capacity exhausted")
        seconds = (request.deadline - self._clock()).total_seconds()
        if not 0 < seconds <= 60:
            raise ValueError("local approval requires a future deadline within 60 seconds")
        self._issued[key] = _Approval(
            digest(canonical_bytes(request.model_dump(mode="json"))),
            authenticated_actor,
            monotonic() + seconds,
        )

    def consume(self, request: LocalControlRequest) -> str:
        self._owner_only()
        key = request.operator_authorization_id or ""
        proof = self._issued.pop(key, None)
        if proof is None:
            raise ValueError("local operator approval unavailable")
        self._used.add(key)
        if (
            proof.expires <= monotonic()
            or request.deadline <= self._clock()
            or proof.request_hash != digest(canonical_bytes(request.model_dump(mode="json")))
        ):
            raise ValueError("local operator approval expired or payload changed")
        return proof.actor
