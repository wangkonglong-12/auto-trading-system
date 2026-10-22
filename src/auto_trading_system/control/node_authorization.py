"""Interactive local approvals, isolated from the emergency-control pipe.

Password work runs on the authorization pipe worker. Only short, authenticated
operations cross the owner mailbox. A preview freezes the exact request; neither
the pipe role, an actor supplied by a caller, nor a password alone can arm trading.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic, time_ns
from typing import Literal, cast
from uuid import uuid4

from pydantic import Field, JsonValue, SecretStr, model_validator

from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.control.ipc_protocol import IPCError, Request
from auto_trading_system.control.owner_mailbox import OwnerMailbox
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.execution_scope import execution_scope, execution_scopes
from auto_trading_system.security.operator_session import (
    AuthenticatedOperatorSession,
    LocalOperatorSessionManager,
    OperatorSessionError,
)

AUTH_ACTIONS = frozenset(
    {"HELLO", "LOGIN", "SESSION", "LOGOUT", "PREVIEW_CONTROL", "CONFIRM_CONTROL", "CONTROL_RESULT"}
)


class GrantProposal(WireModel):
    directions: tuple[Literal["BUY", "SELL"], ...] = Field(
        min_length=1, max_length=2, description="申请方向"
    )
    max_buy_value: MoneyText = Field(description="会话累计买入金额上限，元")
    max_order_value: MoneyText = Field(description="单笔金额上限，元")
    max_positions: int = Field(ge=0, le=15, description="账户持仓标的上限")
    duration_seconds: int = Field(ge=1, le=900, description="本次交易授权有效秒数，最多15分钟")

    @model_validator(mode="after")
    def unique(self) -> GrantProposal:
        if len(set(self.directions)) != len(self.directions):
            raise ValueError("duplicate grant direction")
        return self


class PreviewProposal(WireModel):
    action: Literal["CONFIRM_RECOVERY", "ARM", "RESUME"] = Field(description="待人工确认的具体动作")
    grant: GrantProposal | None = Field(default=None, description="ARM/RESUME的额度与期限申请")

    @model_validator(mode="after")
    def match(self) -> PreviewProposal:
        if (self.action != "CONFIRM_RECOVERY") != (self.grant is not None):
            raise ValueError("grant must match the proposed action")
        return self


@dataclass
class _Preview:
    request: LocalControlRequest
    session_key: str
    actor: str
    phrase: str
    permission_epoch: int
    expires: float
    confirmed: bool = False


class NodeAuthorization:
    """One pipe worker owns authentication; one engine owner owns all previews."""

    def __init__(
        self,
        engine: EngineKernel,
        mailbox: OwnerMailbox[object],
        sessions: LocalOperatorSessionManager,
        *,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
        capacity: int = 128,
    ) -> None:
        if not isinstance(engine.approvals, OneShotApprovals) or not 1 <= capacity <= 256:
            raise ValueError("authorization composition invalid")
        self.engine, self.mailbox, self.sessions = engine, mailbox, sessions
        self.approvals = engine.approvals
        self.clock, self.elapsed, self.capacity = clock, elapsed, capacity
        self._previews: dict[str, _Preview] = {}

    def handle(self, message: Request) -> dict[str, JsonValue]:
        if message.sender_role != "operator":
            raise IPCError("ROLE_DENIED")
        value = dict(message.payload)
        try:
            if message.action == "LOGIN":
                if set(value) != {"password"} or not isinstance(value["password"], str):
                    raise IPCError("INVALID_ARGUMENTS")
                password = value["password"]
                if len(password) > 128:
                    raise IPCError("INVALID_ARGUMENTS")
                issued = self.sessions.login(SecretStr(password))
                return {
                    "session_token": issued.session_token,
                    "csrf_token": issued.csrf_token,
                    "actor_id": issued.actor_id,
                    "expires_at": issued.expires_at.to_wire(),
                    "issued_at": issued.issued_at.to_wire(),
                }
            if message.action == "SESSION":
                if set(value) != {"session_token", "csrf_token"} or not all(
                    isinstance(item, str) for item in value.values()
                ):
                    raise IPCError("INVALID_ARGUMENTS")
                session = self.sessions.authorize_mutation(
                    str(value["session_token"]), str(value["csrf_token"])
                )
                return {
                    "actor_id": session.actor_id,
                    "issued_at": session.issued_at.to_wire(),
                    "expires_at": session.expires_at.to_wire(),
                }
            token, csrf = value.pop("session_token", None), value.pop("csrf_token", None)
            if not isinstance(token, str) or not isinstance(csrf, str):
                raise IPCError("OPERATOR_SESSION_REQUIRED")
            # Validate once on receipt, then again when the queued owner operation
            # executes. A timeout/expired login never becomes a delayed grant.
            self.sessions.authorize_mutation(token, csrf)
            if message.action == "LOGOUT":
                if value:
                    raise IPCError("INVALID_ARGUMENTS")
                self.sessions.logout(token, csrf)
                return {"logged_out": True}

            def operation() -> dict[str, JsonValue]:
                session = self.sessions.authorize_mutation(token, csrf)
                if message.action == "PREVIEW_CONTROL":
                    proposal = PreviewProposal.model_validate_json(canonical_bytes(value))
                    return self._preview(session, proposal)
                if message.action == "CONFIRM_CONTROL":
                    if set(value) != {"preview_id", "phrase"} or not all(
                        isinstance(v, str) for v in value.values()
                    ):
                        raise IPCError("INVALID_ARGUMENTS")
                    return self._confirm(session, str(value["preview_id"]), str(value["phrase"]))
                if message.action == "CONTROL_RESULT":
                    if set(value) != {"request_id"} or not isinstance(value["request_id"], str):
                        raise IPCError("INVALID_ARGUMENTS")
                    return self._result(session.actor_id, value["request_id"])
                raise IPCError("ACTION_DENIED")

            remaining = min(4.0, (message.deadline_ms - time_ns() // 1_000_000) / 1000)
            if remaining <= 0:
                raise IPCError("REQUEST_EXPIRED")
            return cast(dict[str, JsonValue], self.mailbox.call(operation, timeout=remaining))
        except OperatorSessionError as exc:
            raise IPCError(exc.code) from None

    def authorize_control_channel(self, message: Request) -> str:
        """Fast existing-session check for the independent emergency pipe.

        No password hashing or trading permission is performed on this channel.
        Both session and CSRF proofs are checked by the engine, not trusted from
        an HTTP proxy's declaration that a user has logged in.
        """
        if message.sender_role != "operator" or set(message.payload) != {
            "session_token",
            "csrf_token",
        }:
            raise IPCError("INVALID_ARGUMENTS")
        token, csrf = message.payload["session_token"], message.payload["csrf_token"]
        if not isinstance(token, str) or not isinstance(csrf, str):
            raise IPCError("INVALID_ARGUMENTS")
        try:
            return self.sessions.authorize_mutation(token, csrf).actor_id
        except OperatorSessionError as exc:
            raise IPCError(exc.code) from None

    def _preview(
        self, session: AuthenticatedOperatorSession, proposal: PreviewProposal
    ) -> dict[str, JsonValue]:
        snapshot = self.engine.snapshot()  # also checks owner identity
        self._previews = {
            k: v for k, v in self._previews.items() if v.confirmed or v.expires > self.elapsed()
        }
        if len(self._previews) >= self.capacity:
            raise IPCError("APPROVAL_CAPACITY_EXHAUSTED")
        report = snapshot.recovery
        if not report:
            raise IPCError("RECOVERY_REQUIRED")
        now = self.clock()
        deadline = min(now + timedelta(seconds=20), report.observed_at + timedelta(seconds=30))
        grant = None
        scope = execution_scope(self.engine.plans.plans.loaded(), self.engine.exit_context)
        scopes = execution_scopes(self.engine.plans.plans.loaded(), self.engine.exit_context)
        if proposal.grant:
            if not scopes or self.engine.limits.mode not in {"PAPER", "LIVE"}:
                raise IPCError("EXECUTION_SCOPE_REQUIRED")
            p = proposal.grant
            mode = self.engine.limits.mode
            relevant = tuple(
                item for item in scopes if set(item.directions).intersection(p.directions)
            )
            if not relevant:
                raise IPCError("EXECUTION_SCOPE_REQUIRED")
            grant = SessionGrant(
                grant_id="grant-" + uuid4().hex,
                account_id=self.engine.account_id,
                engine_boot_id=self.engine.boot_id,
                session_generation=self.engine.next_session_generation,
                mode=mode,
                directions=p.directions,
                max_buy_value=p.max_buy_value,
                max_order_value=p.max_order_value,
                max_positions=p.max_positions,
                valid_from=now,
                valid_until=min(
                    now + timedelta(seconds=p.duration_seconds),
                    *(item.valid_until for item in relevant),
                ),
                confirmed_by=session.actor_id,
                recovery_report_hash=report.payload_hash,
            )
            deadline = min(deadline, grant.valid_until)
        request = LocalControlRequest(
            request_id="control-" + uuid4().hex,
            engine_boot_id=self.engine.boot_id,
            sender_role="agent",
            deadline=deadline,
            expected_state_version=snapshot.state_version,
            action=proposal.action,
            grant=grant,
            artifact_hash=report.payload_hash if proposal.action == "CONFIRM_RECOVERY" else None,
            operator_authorization_id="approval-" + uuid4().hex,
        )
        self.engine.preview_control(request)
        identity = "preview-" + uuid4().hex
        phrase = f"CONFIRM {self.engine.limits.mode} {proposal.action} {identity[-8:]}"
        self._previews[identity] = _Preview(
            request,
            session.session_key,
            session.actor_id,
            phrase,
            snapshot.permission_epoch,
            self.elapsed() + (deadline - now).total_seconds(),
        )
        return {
            "preview_id": identity,
            "confirmation_phrase": phrase,
            "actor_id": session.actor_id,
            "request": request.model_dump(mode="json"),
            "account_id": self.engine.account_id,
            "trade_date": self.engine.trade_date.isoformat(),
            "permission_epoch": snapshot.permission_epoch,
            "execution_scope": {
                "kind": scope.kind,
                "payload_hash": scope.payload_hash,
                "strategy_id": scope.strategy_id,
                "trade_date": scope.trade_date.isoformat(),
                "valid_until": scope.valid_until.isoformat(),
                "directions": list(scope.directions),
            }
            if scope
            else None,
            "real_trading_enabled": self.engine.limits.mode == "LIVE",
            "installed_limits": self.engine.limits.model_dump(mode="json"),
            "recovery": report.model_dump(mode="json"),
        }

    def _confirm(
        self, session: AuthenticatedOperatorSession, identity: str, phrase: str
    ) -> dict[str, JsonValue]:
        snapshot = self.engine.snapshot()
        preview = self._previews.get(identity)
        if preview is None or preview.session_key != session.session_key:
            raise IPCError("PREVIEW_NOT_FOUND")
        if not hmac.compare_digest(phrase.encode("utf-8"), preview.phrase.encode("utf-8")):
            raise IPCError("CONFIRMATION_MISMATCH")
        if preview.confirmed:
            return self._result(session.actor_id, preview.request.request_id)
        if (
            self.elapsed() >= preview.expires
            or snapshot.permission_epoch != preview.permission_epoch
        ):
            raise IPCError("PREVIEW_EXPIRED_OR_FENCED")
        self.engine.preview_control(preview.request)
        self.approvals.approve(preview.request, authenticated_actor=session.actor_id)
        # Reserve this identity before entering the durable-control path. Errors
        # remain uncertain and require result lookup, never a second approval.
        preview.confirmed = True
        return self.engine.control(preview.request).model_dump(mode="json")

    def _result(self, actor: str, request_id: str) -> dict[str, JsonValue]:
        if not any(
            p.actor == actor and p.confirmed and p.request.request_id == request_id
            for p in self._previews.values()
        ):
            raise IPCError("CONTROL_RESULT_UNKNOWN")
        result = self.engine.control_result(request_id)
        if result is None:
            raise IPCError("CONTROL_RESULT_UNCERTAIN")
        return result.model_dump(mode="json")
