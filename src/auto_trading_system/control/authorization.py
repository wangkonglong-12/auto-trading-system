"""Server-authored impact previews and one-time control authorization proofs."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from threading import Lock

from auto_trading_system.config.models import EnvironmentProfile
from auto_trading_system.control.models import (
    ControlConfirmation,
    LocalControlAction,
    LocalControlRequest,
)
from auto_trading_system.domain import Clock, MonotonicClock, ShanghaiTimestamp
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import RuntimeState
from auto_trading_system.security import AuthenticatedOperatorSession

_IDENTIFIER_PATTERN = re.compile(r"(?:preview|confirm)_[0-9a-f]{32}")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


class ControlPreviewFailureKind(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    CONFLICT = "CONFLICT"
    UNAVAILABLE = "UNAVAILABLE"


class ControlPreviewError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: ControlPreviewFailureKind,
    ) -> None:
        if _REASON_PATTERN.fullmatch(code) is None:
            raise ValueError("code has an invalid format")
        if not isinstance(kind, ControlPreviewFailureKind):
            raise ValueError("kind must be a ControlPreviewFailureKind")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.kind = kind


@dataclass(frozen=True, slots=True)
class LocalControlImpactFacts:
    runtime_state: RuntimeState
    active_orders: int
    active_buy_orders: int
    active_sell_orders: int
    open_positions: int
    qmt_connected: bool
    central_connected: bool
    reconciliation_complete: bool
    unknown_order: bool

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_state, RuntimeState):
            raise ValueError("runtime_state must be a RuntimeState")
        counts = (
            self.active_orders,
            self.active_buy_orders,
            self.active_sell_orders,
            self.open_positions,
        )
        if any(type(value) is not int or not 0 <= value <= 1_000_000 for value in counts):
            raise ValueError("impact counts must be non-negative integers")
        if self.active_buy_orders + self.active_sell_orders > self.active_orders:
            raise ValueError("active side counts cannot exceed active_orders")
        flags = (
            self.qmt_connected,
            self.central_connected,
            self.reconciliation_complete,
            self.unknown_order,
        )
        if any(type(value) is not bool for value in flags):
            raise ValueError("impact health facts must be booleans")


@dataclass(frozen=True, slots=True)
class LocalControlImpactPreview:
    preview_id: str
    session_key: str
    actor_id: str
    node_id: str
    account_id: str
    account_alias: str
    profile: EnvironmentProfile
    action: LocalControlAction
    reason_code: str
    requested_at: ShanghaiTimestamp
    expires_at: ShanghaiTimestamp
    expected_state: RuntimeState
    cancel_active_orders: bool | None
    facts: LocalControlImpactFacts
    impact_hash: str
    confirmation_phrase: str
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if _IDENTIFIER_PATTERN.fullmatch(self.preview_id) is None:
            raise ValueError("preview_id has an invalid format")
        if re.fullmatch(r"[0-9a-f]{64}", self.session_key) is None:
            raise ValueError("session_key has an invalid format")
        if not isinstance(self.profile, EnvironmentProfile) or not isinstance(
            self.action, LocalControlAction
        ):
            raise ValueError("preview profile or action is invalid")
        if _REASON_PATTERN.fullmatch(self.reason_code) is None:
            raise ValueError("reason_code has an invalid format")
        if self.expires_at <= self.requested_at:
            raise ValueError("preview expiry must follow request time")
        if self.expected_state is not self.facts.runtime_state:
            raise ValueError("preview expected_state must match its facts")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.impact_hash):
            raise ValueError("impact_hash has an invalid format")
        if not self.confirmation_phrase or not self.warnings:
            raise ValueError("preview explanation is incomplete")

    def impact_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "preview_id": self.preview_id,
            "actor_id": self.actor_id,
            "node_id": self.node_id,
            "account_id": self.account_id,
            "account_alias": self.account_alias,
            "profile": self.profile.value,
            "action": self.action.value,
            "reason_code": self.reason_code,
            "requested_at": self.requested_at.to_wire(),
            "expected_state": self.expected_state.value,
            "requested_state": self.action.target_state.value,
            "cancel_active_orders": self.cancel_active_orders,
            "active_orders": self.facts.active_orders,
            "active_buy_orders": self.facts.active_buy_orders,
            "active_sell_orders": self.facts.active_sell_orders,
            "open_positions": self.facts.open_positions,
            "qmt_connected": self.facts.qmt_connected,
            "central_connected": self.facts.central_connected,
            "reconciliation_complete": self.facts.reconciliation_complete,
            "unknown_order": self.facts.unknown_order,
        }


@dataclass(frozen=True, slots=True)
class ConfirmedControlAuthorization:
    authorization_token: str
    preview: LocalControlImpactPreview
    confirmation: ControlConfirmation

    def __post_init__(self) -> None:
        if _TOKEN_PATTERN.fullmatch(self.authorization_token) is None:
            raise ValueError("authorization_token has an invalid format")


@dataclass(frozen=True, slots=True)
class _PreviewRecord:
    preview: LocalControlImpactPreview
    expires_ns: int
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class _AuthorizationRecord:
    preview: LocalControlImpactPreview
    confirmation: ControlConfirmation
    expires_ns: int
    bound_request: LocalControlRequest | None = None


class LocalControlPreviewManager:
    """Bind typed confirmation to one authenticated session and exact impact snapshot."""

    def __init__(
        self,
        *,
        node_id: str,
        account_id: str,
        account_alias: str,
        profile: EnvironmentProfile,
        clock: Clock,
        monotonic_clock: MonotonicClock,
        facts_supplier: Callable[[], LocalControlImpactFacts],
        ttl_seconds: int = 90,
        capacity: int = 256,
        identifier_factory: Callable[[str], str] = lambda prefix: (
            f"{prefix}_{secrets.token_hex(16)}"
        ),
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if re.fullmatch(r"node-[a-z0-9][a-z0-9-]{2,47}", node_id) is None:
            raise ValueError("node_id has an invalid format")
        if re.fullmatch(r"acct_[A-Za-z0-9_-]{3,48}", account_id) is None:
            raise ValueError("account_id has an invalid format")
        if re.fullmatch(r"[A-Za-z0-9_-]{2,32}", account_alias) is None:
            raise ValueError("account_alias has an invalid format")
        if not isinstance(profile, EnvironmentProfile):
            raise ValueError("profile must be an EnvironmentProfile")
        if not callable(getattr(clock, "now", None)) or not callable(
            getattr(monotonic_clock, "monotonic_ns", None)
        ):
            raise ValueError("preview clocks are invalid")
        if (
            not callable(facts_supplier)
            or not callable(identifier_factory)
            or not callable(token_factory)
        ):
            raise ValueError("preview factories must be callable")
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 120:
            raise ValueError("ttl_seconds must be between 30 and 120")
        if type(capacity) is not int or not 1 <= capacity <= 10_000:
            raise ValueError("capacity must be between 1 and 10000")
        self._node_id = node_id
        self._account_id = account_id
        self._account_alias = account_alias
        self._profile = profile
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._facts_supplier = facts_supplier
        self._ttl_seconds = ttl_seconds
        self._capacity = capacity
        self._identifier_factory = identifier_factory
        self._token_factory = token_factory
        self._previews: OrderedDict[str, _PreviewRecord] = OrderedDict()
        self._authorizations: OrderedDict[str, _AuthorizationRecord] = OrderedDict()
        self._lock = Lock()

    def create(
        self,
        session: AuthenticatedOperatorSession,
        *,
        action: LocalControlAction,
        reason_code: str,
        cancel_active_orders: bool | None,
    ) -> LocalControlImpactPreview:
        _validate_session(session)
        if not isinstance(action, LocalControlAction):
            raise ValueError("action must be a LocalControlAction")
        if _REASON_PATTERN.fullmatch(reason_code) is None:
            raise ValueError("reason_code has an invalid format")
        _validate_order_disposition(action, cancel_active_orders)
        facts = self._facts_supplier()
        if not isinstance(facts, LocalControlImpactFacts):
            raise ControlPreviewError(
                "CONTROL_PREVIEW_FACTS_UNAVAILABLE",
                "control impact facts are unavailable",
                kind=ControlPreviewFailureKind.UNAVAILABLE,
            )
        now = self._clock.now()
        now_ns = self._monotonic_clock.monotonic_ns()
        preview_id = self._new_identifier("preview")
        expires_at = ShanghaiTimestamp.from_instant(
            now.value + timedelta(seconds=self._ttl_seconds)
        )
        provisional = LocalControlImpactPreview(
            preview_id=preview_id,
            session_key=session.session_key,
            actor_id=session.actor_id,
            node_id=self._node_id,
            account_id=self._account_id,
            account_alias=self._account_alias,
            profile=self._profile,
            action=action,
            reason_code=reason_code,
            requested_at=now,
            expires_at=expires_at,
            expected_state=facts.runtime_state,
            cancel_active_orders=cancel_active_orders,
            facts=facts,
            impact_hash="sha256:" + "0" * 64,
            confirmation_phrase=_confirmation_phrase(action),
            warnings=_impact_warnings(action, facts, cancel_active_orders),
        )
        impact_hash = (
            "sha256:"
            + hashlib.sha256(
                canonical_json_bytes(provisional.impact_mapping(), exclude=frozenset())
            ).hexdigest()
        )
        preview = replace(provisional, impact_hash=impact_hash)
        with self._lock:
            self._expire(now_ns)
            if len(self._previews) >= self._capacity:
                raise ControlPreviewError(
                    "CONTROL_PREVIEW_CAPACITY_EXHAUSTED",
                    "all control preview slots are active",
                    kind=ControlPreviewFailureKind.UNAVAILABLE,
                )
            self._previews[preview_id] = _PreviewRecord(
                preview,
                now_ns + self._ttl_seconds * 1_000_000_000,
            )
        return preview

    def confirm(
        self,
        session: AuthenticatedOperatorSession,
        *,
        preview_id: str,
        phrase: str,
    ) -> ConfirmedControlAuthorization:
        _validate_session(session)
        _validate_preview_id(preview_id)
        now_ns = self._monotonic_clock.monotonic_ns()
        with self._lock:
            self._expire(now_ns)
            record = self._previews.get(preview_id)
            if record is None:
                raise ControlPreviewError(
                    "CONTROL_PREVIEW_NOT_FOUND",
                    "control preview is missing or expired",
                    kind=ControlPreviewFailureKind.NOT_FOUND,
                )
            if record.preview.session_key != session.session_key:
                raise ControlPreviewError(
                    "CONTROL_PREVIEW_SESSION_MISMATCH",
                    "control preview belongs to a different session",
                    kind=ControlPreviewFailureKind.FORBIDDEN,
                )
            if record.confirmed:
                raise ControlPreviewError(
                    "CONTROL_PREVIEW_ALREADY_CONFIRMED",
                    "control preview was already confirmed",
                    kind=ControlPreviewFailureKind.CONFLICT,
                )
            if not isinstance(phrase, str) or not hmac.compare_digest(
                phrase.encode("utf-8"), record.preview.confirmation_phrase.encode("utf-8")
            ):
                raise ControlPreviewError(
                    "CONTROL_CONFIRMATION_PHRASE_INVALID",
                    "typed confirmation phrase did not match",
                    kind=ControlPreviewFailureKind.FORBIDDEN,
                )
            current_facts = self._facts_supplier()
            if current_facts != record.preview.facts:
                raise ControlPreviewError(
                    "CONTROL_PREVIEW_STALE",
                    "runtime impact changed after the preview",
                    kind=ControlPreviewFailureKind.CONFLICT,
                )
            authorization_token = self._new_token()
            confirmation = ControlConfirmation(
                self._new_identifier("confirm"),
                session.actor_id,
                self._clock.now(),
                record.preview.impact_hash,
            )
            self._previews[preview_id] = replace(record, confirmed=True)
            self._authorizations[_token_hash(authorization_token)] = _AuthorizationRecord(
                record.preview,
                confirmation,
                record.expires_ns,
            )
        return ConfirmedControlAuthorization(authorization_token, record.preview, confirmation)

    def authorize(
        self,
        session: AuthenticatedOperatorSession,
        *,
        authorization_token: str | None,
        request: LocalControlRequest,
    ) -> None:
        _validate_session(session)
        if not isinstance(request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        token = _validate_authorization_token(authorization_token)
        now_ns = self._monotonic_clock.monotonic_ns()
        with self._lock:
            self._expire(now_ns)
            token_key = _token_hash(token)
            record = self._authorizations.get(token_key)
            if record is None:
                raise ControlPreviewError(
                    "CONTROL_AUTHORIZATION_INVALID",
                    "control authorization is missing or expired",
                    kind=ControlPreviewFailureKind.FORBIDDEN,
                )
            if record.preview.session_key != session.session_key:
                raise ControlPreviewError(
                    "CONTROL_AUTHORIZATION_SESSION_MISMATCH",
                    "control authorization belongs to a different session",
                    kind=ControlPreviewFailureKind.FORBIDDEN,
                )
            if not _request_matches_authorization(request, record):
                raise ControlPreviewError(
                    "CONTROL_AUTHORIZATION_MISMATCH",
                    "control request does not match its confirmed impact",
                    kind=ControlPreviewFailureKind.CONFLICT,
                )
            current_facts = self._facts_supplier()
            if current_facts != record.preview.facts:
                raise ControlPreviewError(
                    "CONTROL_PREVIEW_STALE",
                    "runtime impact changed after confirmation",
                    kind=ControlPreviewFailureKind.CONFLICT,
                )
            if record.bound_request is not None and record.bound_request != request:
                raise ControlPreviewError(
                    "CONTROL_AUTHORIZATION_ALREADY_USED",
                    "control authorization is bound to another command",
                    kind=ControlPreviewFailureKind.CONFLICT,
                )
            self._authorizations[token_key] = replace(record, bound_request=request)

    def revoke_session(self, session_key: str) -> int:
        if re.fullmatch(r"[0-9a-f]{64}", session_key) is None:
            raise ValueError("session_key has an invalid format")
        with self._lock:
            preview_ids = [
                key
                for key, record in self._previews.items()
                if record.preview.session_key == session_key
            ]
            authorization_ids = [
                key
                for key, record in self._authorizations.items()
                if record.preview.session_key == session_key
            ]
            for key in preview_ids:
                del self._previews[key]
            for key in authorization_ids:
                del self._authorizations[key]
            return len(preview_ids) + len(authorization_ids)

    def _expire(self, now_ns: int) -> None:
        expired_previews = [
            key for key, record in self._previews.items() if now_ns > record.expires_ns
        ]
        expired_authorizations = [
            key for key, record in self._authorizations.items() if now_ns > record.expires_ns
        ]
        for key in expired_previews:
            del self._previews[key]
        for key in expired_authorizations:
            del self._authorizations[key]

    def _new_identifier(self, prefix: str) -> str:
        value = self._identifier_factory(prefix)
        if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise ControlPreviewError(
                "CONTROL_PREVIEW_RANDOM_FAILED",
                "secure preview identity could not be generated",
                kind=ControlPreviewFailureKind.UNAVAILABLE,
            )
        return value

    def _new_token(self) -> str:
        value = self._token_factory()
        if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
            raise ControlPreviewError(
                "CONTROL_AUTHORIZATION_RANDOM_FAILED",
                "secure control authorization could not be generated",
                kind=ControlPreviewFailureKind.UNAVAILABLE,
            )
        return value


def _request_matches_authorization(
    request: LocalControlRequest,
    record: _AuthorizationRecord,
) -> bool:
    preview = record.preview
    return (
        request.action is preview.action
        and request.actor_id == preview.actor_id
        and request.reason_code == preview.reason_code
        and request.requested_at == preview.requested_at
        and request.expected_state is preview.expected_state
        and request.cancel_active_orders is preview.cancel_active_orders
        and request.confirmation == record.confirmation
    )


def _impact_warnings(
    action: LocalControlAction,
    facts: LocalControlImpactFacts,
    cancel_active_orders: bool | None,
) -> tuple[str, ...]:
    common = f"当前有{facts.active_orders}笔活动委托、{facts.open_positions}个持仓标的。"
    action_warning = {
        LocalControlAction.PAUSE_BUY: "系统将停止创建新的买入委托。",
        LocalControlAction.SELL_ONLY: "系统将只保留降低持仓风险的卖出能力。",
        LocalControlAction.MANUAL_TAKEOVER: "自动策略将退出订单控制并转为人工处置。",
        LocalControlAction.EMERGENCY_HALT: (
            "系统将立即关闭全部自动报单能力。系统会请求撤销活动委托。"
            if cancel_active_orders
            else "系统将立即关闭全部自动报单能力。活动委托保留并进入人工核对。"
        ),
        LocalControlAction.CLEAR_HALT: "系统将解除HALT并停在READY_PAUSED。报单能力仍保持关闭。",
        LocalControlAction.REQUEST_AUTO: "系统将在健康、对账和双重许可满足后恢复自动报单。",
    }[action]
    uncertainty = (
        ("当前存在UNKNOWN委托。确认后仍由Runtime安全规则决定结果。",) if facts.unknown_order else ()
    )
    return (common, action_warning, *uncertainty)


def _confirmation_phrase(action: LocalControlAction) -> str:
    return {
        LocalControlAction.PAUSE_BUY: "确认暂停买入",
        LocalControlAction.SELL_ONLY: "确认切换只卖",
        LocalControlAction.MANUAL_TAKEOVER: "确认人工接管",
        LocalControlAction.EMERGENCY_HALT: "确认紧急停止",
        LocalControlAction.CLEAR_HALT: "确认解除停止",
        LocalControlAction.REQUEST_AUTO: "确认恢复自动",
    }[action]


def _validate_session(session: AuthenticatedOperatorSession) -> None:
    if not isinstance(session, AuthenticatedOperatorSession):
        raise ValueError("session must be an AuthenticatedOperatorSession")


def _validate_order_disposition(
    action: LocalControlAction,
    cancel_active_orders: bool | None,
) -> None:
    if action is LocalControlAction.EMERGENCY_HALT:
        if type(cancel_active_orders) is not bool:
            raise ValueError("EMERGENCY_HALT requires an active-order disposition")
    elif cancel_active_orders is not None:
        raise ValueError("only EMERGENCY_HALT can set an active-order disposition")


def _validate_preview_id(preview_id: str) -> None:
    if not isinstance(preview_id, str) or _IDENTIFIER_PATTERN.fullmatch(preview_id) is None:
        raise ValueError("preview_id has an invalid format")


def _validate_authorization_token(value: str | None) -> str:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ControlPreviewError(
            "CONTROL_AUTHORIZATION_INVALID",
            "control authorization is missing or expired",
            kind=ControlPreviewFailureKind.FORBIDDEN,
        )
    return value


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()
