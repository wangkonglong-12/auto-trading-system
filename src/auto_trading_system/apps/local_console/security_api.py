"""Authenticated session, CSRF, and confirmed-impact HTTP boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from auto_trading_system.control import (
    ConfirmedControlAuthorization,
    ControlPreviewError,
    ControlPreviewFailureKind,
    LocalControlAction,
    LocalControlImpactPreview,
    LocalControlPreviewManager,
    LocalControlRequest,
)
from auto_trading_system.security import (
    AuthenticatedOperatorSession,
    IssuedOperatorSession,
    LocalOperatorSessionManager,
    OperatorSessionError,
    OperatorSessionFailureKind,
)

SESSION_COOKIE_NAME = "ats_local_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
CONTROL_AUTHORIZATION_HEADER_NAME = "X-Control-Authorization"

ReasonCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]
PreviewId = Annotated[str, Field(pattern=r"^preview_[0-9a-f]{32}$")]


class OperatorLoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    password: SecretStr


class OperatorSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    actor_id: str
    issued_at: str
    expires_at: str
    csrf_token: str | None = None


class ControlPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: LocalControlAction
    reason_code: ReasonCode
    cancel_active_orders: bool | None = None


class ControlImpactFactsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_orders: int
    active_buy_orders: int
    active_sell_orders: int
    open_positions: int
    qmt_connected: bool
    central_connected: bool
    reconciliation_complete: bool
    unknown_order: bool


class ControlPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    preview_id: str
    actor_id: str
    node_id: str
    account_id: str
    account_alias: str
    profile: str
    action: LocalControlAction
    reason_code: str
    requested_at: str
    expires_at: str
    expected_state: str
    requested_state: str
    cancel_active_orders: bool | None
    impact_hash: str
    confirmation_phrase: str
    warnings: tuple[str, ...]
    facts: ControlImpactFactsResponse

    @classmethod
    def from_domain(cls, preview: LocalControlImpactPreview) -> ControlPreviewResponse:
        return cls(
            preview_id=preview.preview_id,
            actor_id=preview.actor_id,
            node_id=preview.node_id,
            account_id=preview.account_id,
            account_alias=preview.account_alias,
            profile=preview.profile.value,
            action=preview.action,
            reason_code=preview.reason_code,
            requested_at=preview.requested_at.to_wire(),
            expires_at=preview.expires_at.to_wire(),
            expected_state=preview.expected_state.value,
            requested_state=preview.action.target_state.value,
            cancel_active_orders=preview.cancel_active_orders,
            impact_hash=preview.impact_hash,
            confirmation_phrase=preview.confirmation_phrase,
            warnings=preview.warnings,
            facts=ControlImpactFactsResponse(
                active_orders=preview.facts.active_orders,
                active_buy_orders=preview.facts.active_buy_orders,
                active_sell_orders=preview.facts.active_sell_orders,
                open_positions=preview.facts.open_positions,
                qmt_connected=preview.facts.qmt_connected,
                central_connected=preview.facts.central_connected,
                reconciliation_complete=preview.facts.reconciliation_complete,
                unknown_order=preview.facts.unknown_order,
            ),
        )


class ControlConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=4, max_length=32)


class ConfirmedControlResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    authorization_token: str
    preview_id: str
    command_template: dict[str, object]

    @classmethod
    def from_domain(cls, value: ConfirmedControlAuthorization) -> ConfirmedControlResponse:
        preview = value.preview
        confirmation = value.confirmation
        return cls(
            authorization_token=value.authorization_token,
            preview_id=preview.preview_id,
            command_template={
                "action": preview.action.value,
                "actor_id": preview.actor_id,
                "reason_code": preview.reason_code,
                "requested_at": preview.requested_at.to_wire(),
                "expected_state": preview.expected_state.value,
                "confirmation": {
                    "confirmation_id": confirmation.confirmation_id,
                    "actor_id": confirmation.actor_id,
                    "confirmed_at": confirmation.confirmed_at.to_wire(),
                    "impact_hash": confirmation.impact_hash,
                },
                "cancel_active_orders": preview.cancel_active_orders,
            },
        )


class LocalConsoleSecurityController:
    """Translate browser proofs into authenticated, preview-bound control authority."""

    def __init__(
        self,
        sessions: LocalOperatorSessionManager,
        previews: LocalControlPreviewManager,
        *,
        expected_origin: str,
        session_max_age_seconds: int,
    ) -> None:
        if not isinstance(sessions, LocalOperatorSessionManager):
            raise ValueError("sessions must be a LocalOperatorSessionManager")
        if not isinstance(previews, LocalControlPreviewManager):
            raise ValueError("previews must be a LocalControlPreviewManager")
        if not expected_origin.startswith("http://127.0.0.1:") or expected_origin.endswith("/"):
            raise ValueError("expected_origin must be a loopback HTTP origin")
        if type(session_max_age_seconds) is not int or not 900 <= session_max_age_seconds <= 14_400:
            raise ValueError("session_max_age_seconds has an invalid value")
        self._sessions = sessions
        self._previews = previews
        self._expected_origin = expected_origin
        self._session_max_age_seconds = session_max_age_seconds

    @property
    def session_max_age_seconds(self) -> int:
        return self._session_max_age_seconds

    def login(self, request: Request, password: SecretStr) -> IssuedOperatorSession:
        self._require_same_origin(request)
        return self._sessions.login(password)

    def authenticate(self, request: Request) -> AuthenticatedOperatorSession:
        return self._sessions.authenticate(request.cookies.get(SESSION_COOKIE_NAME))

    def authorize_mutation(self, request: Request) -> AuthenticatedOperatorSession:
        self._require_same_origin(request)
        return self._sessions.authorize_mutation(
            request.cookies.get(SESSION_COOKIE_NAME),
            request.headers.get(CSRF_HEADER_NAME),
        )

    def logout(self, request: Request) -> None:
        session = self.authorize_mutation(request)
        self._previews.revoke_session(session.session_key)
        self._sessions.logout(
            request.cookies.get(SESSION_COOKIE_NAME),
            request.headers.get(CSRF_HEADER_NAME),
        )

    def create_preview(
        self, request: Request, body: ControlPreviewBody
    ) -> LocalControlImpactPreview:
        session = self.authorize_mutation(request)
        return self._previews.create(
            session,
            action=body.action,
            reason_code=body.reason_code,
            cancel_active_orders=body.cancel_active_orders,
        )

    def confirm_preview(
        self,
        request: Request,
        *,
        preview_id: str,
        phrase: str,
    ) -> ConfirmedControlAuthorization:
        session = self.authorize_mutation(request)
        return self._previews.confirm(session, preview_id=preview_id, phrase=phrase)

    def authorize_control(self, request: Request, control: LocalControlRequest) -> None:
        session = self.authorize_mutation(request)
        self._previews.authorize(
            session,
            authorization_token=request.headers.get(CONTROL_AUTHORIZATION_HEADER_NAME),
            request=control,
        )

    def _require_same_origin(self, request: Request) -> None:
        if (
            request.headers.get("origin") != self._expected_origin
            or request.headers.get("sec-fetch-site") != "same-origin"
        ):
            raise OperatorSessionError(
                "OPERATOR_ORIGIN_INVALID",
                "request did not originate from the local operator console",
                kind=OperatorSessionFailureKind.FORBIDDEN,
            )


def build_local_console_security_router(
    security: LocalConsoleSecurityController,
) -> APIRouter:
    if not isinstance(security, LocalConsoleSecurityController):
        raise ValueError("security must be a LocalConsoleSecurityController")
    router = APIRouter(prefix="/api/v1/local", tags=["operator-security"])

    @router.post("/session", response_model=OperatorSessionResponse)
    async def login(
        request: Request,
        response: Response,
        body: OperatorLoginBody,
    ) -> OperatorSessionResponse:
        try:
            issued = security.login(request, body.password)
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        response.set_cookie(
            SESSION_COOKIE_NAME,
            issued.session_token,
            max_age=security.session_max_age_seconds,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        return OperatorSessionResponse(
            actor_id=issued.actor_id,
            issued_at=issued.issued_at.to_wire(),
            expires_at=issued.expires_at.to_wire(),
            csrf_token=issued.csrf_token,
        )

    @router.get("/session", response_model=OperatorSessionResponse)
    async def get_session(request: Request) -> OperatorSessionResponse:
        try:
            session = security.authenticate(request)
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        return OperatorSessionResponse(
            actor_id=session.actor_id,
            issued_at=session.issued_at.to_wire(),
            expires_at=session.expires_at.to_wire(),
        )

    @router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, response: Response) -> None:
        try:
            security.logout(request)
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, samesite="strict")

    @router.post("/control-previews", response_model=ControlPreviewResponse)
    async def create_preview(
        request: Request,
        body: ControlPreviewBody,
    ) -> ControlPreviewResponse:
        try:
            preview = security.create_preview(request, body)
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        except ControlPreviewError as exc:
            raise control_preview_http_error(exc) from None
        return ControlPreviewResponse.from_domain(preview)

    @router.post(
        "/control-previews/{preview_id}/confirm",
        response_model=ConfirmedControlResponse,
    )
    async def confirm_preview(
        request: Request,
        preview_id: PreviewId,
        body: ControlConfirmationBody,
    ) -> ConfirmedControlResponse:
        try:
            confirmed = security.confirm_preview(
                request,
                preview_id=preview_id,
                phrase=body.phrase,
            )
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        except ControlPreviewError as exc:
            raise control_preview_http_error(exc) from None
        return ConfirmedControlResponse.from_domain(confirmed)

    return router


def operator_session_http_error(error: OperatorSessionError) -> HTTPException:
    status_code = {
        OperatorSessionFailureKind.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
        OperatorSessionFailureKind.FORBIDDEN: status.HTTP_403_FORBIDDEN,
        OperatorSessionFailureKind.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        OperatorSessionFailureKind.CAPACITY: status.HTTP_503_SERVICE_UNAVAILABLE,
    }[error.kind]
    headers: dict[str, str] = {}
    if status_code == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = "LocalOperator"
    if error.retry_after_seconds is not None:
        headers["Retry-After"] = str(error.retry_after_seconds)
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.detail},
        headers=headers or None,
    )


def control_preview_http_error(error: ControlPreviewError) -> HTTPException:
    status_code = {
        ControlPreviewFailureKind.NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ControlPreviewFailureKind.FORBIDDEN: status.HTTP_403_FORBIDDEN,
        ControlPreviewFailureKind.CONFLICT: status.HTTP_409_CONFLICT,
        ControlPreviewFailureKind.UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    }[error.kind]
    headers = {"Retry-After": "1"} if status_code == status.HTTP_503_SERVICE_UNAVAILABLE else None
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.detail},
        headers=headers,
    )
