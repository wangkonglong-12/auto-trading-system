from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest

from auto_trading_system.config.models import EnvironmentProfile
from auto_trading_system.control import (
    ConfirmedControlAuthorization,
    ControlPreviewError,
    ControlPreviewFailureKind,
    LocalControlAction,
    LocalControlImpactFacts,
    LocalControlImpactPreview,
    LocalControlPreviewManager,
    LocalControlRequest,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.runtime import RuntimeState
from auto_trading_system.security import AuthenticatedOperatorSession


@dataclass
class MutableClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00")
    )
    elapsed_ns: int = 0

    def now(self) -> ShanghaiTimestamp:
        return self.value

    def monotonic_ns(self) -> int:
        return self.elapsed_ns

    def advance(self, seconds: int) -> None:
        self.elapsed_ns += seconds * 1_000_000_000
        self.value = ShanghaiTimestamp.from_instant(self.value.value + timedelta(seconds=seconds))


@dataclass
class FactSource:
    value: LocalControlImpactFacts

    def __call__(self) -> LocalControlImpactFacts:
        return self.value


class IdentifierSequence:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}_{self.value:032x}"


def facts(*, state: RuntimeState = RuntimeState.AUTO) -> LocalControlImpactFacts:
    return LocalControlImpactFacts(
        runtime_state=state,
        active_orders=3,
        active_buy_orders=2,
        active_sell_orders=1,
        open_positions=4,
        qmt_connected=True,
        central_connected=True,
        reconciliation_complete=True,
        unknown_order=True,
    )


def session(value: str = "a") -> AuthenticatedOperatorSession:
    issued = ShanghaiTimestamp.parse("2026-09-04T09:55:00+08:00")
    expires = ShanghaiTimestamp.parse("2026-09-04T11:00:00+08:00")
    return AuthenticatedOperatorSession(value * 64, "operator_demo", issued, expires)


def manager(
    clock: MutableClock,
    source: FactSource,
    *,
    capacity: int = 8,
) -> LocalControlPreviewManager:
    return LocalControlPreviewManager(
        node_id="node-dev-01",
        account_id="acct_demo_001",
        account_alias="demo",
        profile=EnvironmentProfile.READ_ONLY,
        clock=clock,
        monotonic_clock=clock,
        facts_supplier=source,
        ttl_seconds=30,
        capacity=capacity,
        identifier_factory=IdentifierSequence(),
        token_factory=lambda: "authorization_token_" + "x" * 32,
    )


def create_halt(
    previews: LocalControlPreviewManager,
    owner: AuthenticatedOperatorSession | None = None,
) -> LocalControlImpactPreview:
    return previews.create(
        owner or session(),
        action=LocalControlAction.EMERGENCY_HALT,
        reason_code="LOCAL_EMERGENCY_HALT",
        cancel_active_orders=True,
    )


def request_from_confirmation(
    confirmed: ConfirmedControlAuthorization,
    *,
    command_id: str = "cmd_authorized_00000001",
) -> LocalControlRequest:
    return LocalControlRequest(
        command_id,
        confirmed.preview.action,
        confirmed.preview.actor_id,
        confirmed.preview.reason_code,
        confirmed.preview.requested_at,
        confirmed.preview.expected_state,
        "corr_authorized_00000001",
        confirmed.confirmation,
        confirmed.preview.cancel_active_orders,
    )


def test_preview_is_server_authored_hash_bound_and_explains_live_impact() -> None:
    clock = MutableClock()
    source = FactSource(facts())
    preview = create_halt(manager(clock, source))
    comparison = create_halt(manager(MutableClock(), FactSource(facts())))

    assert preview.impact_hash == comparison.impact_hash
    assert preview.confirmation_phrase == "确认紧急停止"
    assert preview.expected_state is RuntimeState.AUTO
    assert preview.expires_at.to_wire() == "2026-09-04T10:00:30+08:00"
    assert preview.warnings == (
        "当前有3笔活动委托、4个持仓标的。",
        "系统将立即关闭全部自动报单能力。系统会请求撤销活动委托。",
        "当前存在UNKNOWN委托。确认后仍由Runtime安全规则决定结果。",
    )
    assert preview.impact_mapping()["account_id"] == "acct_demo_001"
    assert preview.impact_mapping()["cancel_active_orders"] is True


def test_confirmation_authorizes_one_exact_request_and_allows_exact_retry() -> None:
    clock = MutableClock()
    source = FactSource(facts())
    previews = manager(clock, source)
    preview = create_halt(previews)
    confirmed = previews.confirm(
        session(),
        preview_id=preview.preview_id,
        phrase=preview.confirmation_phrase,
    )
    request = request_from_confirmation(confirmed)

    previews.authorize(
        session(),
        authorization_token=confirmed.authorization_token,
        request=request,
    )
    previews.authorize(
        session(),
        authorization_token=confirmed.authorization_token,
        request=request,
    )

    altered = replace(request, correlation_id="corr_authorized_00000002")
    with pytest.raises(ControlPreviewError) as captured:
        previews.authorize(
            session(),
            authorization_token=confirmed.authorization_token,
            request=altered,
        )
    assert captured.value.code == "CONTROL_AUTHORIZATION_ALREADY_USED"


def test_confirmation_rejects_wrong_phrase_cross_session_and_repeat() -> None:
    previews = manager(MutableClock(), FactSource(facts()))
    preview = create_halt(previews)

    with pytest.raises(ControlPreviewError) as wrong_phrase:
        previews.confirm(session(), preview_id=preview.preview_id, phrase="确认别的操作")
    assert wrong_phrase.value.kind is ControlPreviewFailureKind.FORBIDDEN

    with pytest.raises(ControlPreviewError) as cross_session:
        previews.confirm(
            session("b"),
            preview_id=preview.preview_id,
            phrase=preview.confirmation_phrase,
        )
    assert cross_session.value.code == "CONTROL_PREVIEW_SESSION_MISMATCH"

    previews.confirm(
        session(),
        preview_id=preview.preview_id,
        phrase=preview.confirmation_phrase,
    )
    with pytest.raises(ControlPreviewError, match="CONTROL_PREVIEW_ALREADY_CONFIRMED"):
        previews.confirm(
            session(),
            preview_id=preview.preview_id,
            phrase=preview.confirmation_phrase,
        )


def test_preview_and_authorization_fail_when_facts_change_or_ttl_expires() -> None:
    clock = MutableClock()
    source = FactSource(facts())
    previews = manager(clock, source)
    stale_preview = create_halt(previews)
    source.value = replace(source.value, active_orders=4)
    with pytest.raises(ControlPreviewError, match="CONTROL_PREVIEW_STALE"):
        previews.confirm(
            session(),
            preview_id=stale_preview.preview_id,
            phrase=stale_preview.confirmation_phrase,
        )

    source.value = facts()
    expired_preview = create_halt(previews)
    clock.advance(31)
    with pytest.raises(ControlPreviewError) as expired:
        previews.confirm(
            session(),
            preview_id=expired_preview.preview_id,
            phrase=expired_preview.confirmation_phrase,
        )
    assert expired.value.kind is ControlPreviewFailureKind.NOT_FOUND


def test_authorization_checks_current_facts_session_and_token_then_revokes() -> None:
    source = FactSource(facts())
    previews = manager(MutableClock(), source)
    preview = create_halt(previews)
    confirmed = previews.confirm(
        session(),
        preview_id=preview.preview_id,
        phrase=preview.confirmation_phrase,
    )
    request = request_from_confirmation(confirmed)

    with pytest.raises(ControlPreviewError, match="CONTROL_AUTHORIZATION_INVALID"):
        previews.authorize(session(), authorization_token=None, request=request)
    with pytest.raises(ControlPreviewError, match="CONTROL_AUTHORIZATION_SESSION_MISMATCH"):
        previews.authorize(
            session("b"),
            authorization_token=confirmed.authorization_token,
            request=request,
        )

    source.value = replace(source.value, open_positions=5)
    with pytest.raises(ControlPreviewError, match="CONTROL_PREVIEW_STALE"):
        previews.authorize(
            session(),
            authorization_token=confirmed.authorization_token,
            request=request,
        )

    assert previews.revoke_session(session().session_key) == 2
    assert previews.revoke_session(session().session_key) == 0


def test_preview_validates_order_disposition_capacity_and_secure_factories() -> None:
    clock = MutableClock()
    source = FactSource(facts())
    previews = manager(clock, source, capacity=1)

    with pytest.raises(ValueError, match="active-order disposition"):
        previews.create(
            session(),
            action=LocalControlAction.EMERGENCY_HALT,
            reason_code="LOCAL_EMERGENCY_HALT",
            cancel_active_orders=None,
        )
    previews.create(
        session(),
        action=LocalControlAction.PAUSE_BUY,
        reason_code="OPERATOR_REQUEST",
        cancel_active_orders=None,
    )
    with pytest.raises(ControlPreviewError, match="CONTROL_PREVIEW_CAPACITY_EXHAUSTED"):
        previews.create(
            session(),
            action=LocalControlAction.PAUSE_BUY,
            reason_code="OPERATOR_REQUEST",
            cancel_active_orders=None,
        )

    invalid = LocalControlPreviewManager(
        node_id="node-dev-01",
        account_id="acct_demo_001",
        account_alias="demo",
        profile=EnvironmentProfile.READ_ONLY,
        clock=clock,
        monotonic_clock=clock,
        facts_supplier=source,
        identifier_factory=lambda _: "bad",
    )
    with pytest.raises(ControlPreviewError, match="CONTROL_PREVIEW_RANDOM_FAILED"):
        invalid.create(
            session(),
            action=LocalControlAction.PAUSE_BUY,
            reason_code="OPERATOR_REQUEST",
            cancel_active_orders=None,
        )
