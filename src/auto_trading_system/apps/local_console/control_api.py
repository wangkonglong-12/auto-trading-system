"""Versioned loopback HTTP boundary for local operator controls."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from auto_trading_system.control import (
    ControlConfirmation,
    ControlPreviewError,
    LocalControlAction,
    LocalControlCommandReceipt,
    LocalControlRequest,
    LocalControlSubmissionError,
    LocalControlSubmissionFailureKind,
    LocalControlSubmissionService,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.runtime import RuntimeState
from auto_trading_system.security import OperatorSessionError

from .security_api import (
    LocalConsoleSecurityController,
    control_preview_http_error,
    operator_session_http_error,
)

CommandId = Annotated[str, Field(pattern=r"^cmd_[A-Za-z0-9_-]{8,64}$")]
CorrelationId = Annotated[str, Field(pattern=r"^corr_[A-Za-z0-9_-]{8,64}$")]
ConfirmationId = Annotated[str, Field(pattern=r"^confirm_[A-Za-z0-9_-]{8,64}$")]
ActorId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{2,95}$")]
ReasonCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]
ContentHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class LocalControlConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: ConfirmationId
    actor_id: ActorId
    confirmed_at: str
    impact_hash: ContentHash

    def to_domain(self) -> ControlConfirmation:
        return ControlConfirmation(
            self.confirmation_id,
            self.actor_id,
            ShanghaiTimestamp.parse(self.confirmed_at),
            self.impact_hash,
        )


class LocalControlCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: CommandId
    action: LocalControlAction
    actor_id: ActorId
    reason_code: ReasonCode
    requested_at: str
    expected_state: RuntimeState
    correlation_id: CorrelationId
    confirmation: LocalControlConfirmationBody | None = None
    cancel_active_orders: bool | None = None

    @model_validator(mode="after")
    def validate_domain_invariants(self) -> LocalControlCommandBody:
        self.to_domain()
        return self

    def to_domain(self) -> LocalControlRequest:
        return LocalControlRequest(
            self.command_id,
            self.action,
            self.actor_id,
            self.reason_code,
            ShanghaiTimestamp.parse(self.requested_at),
            self.expected_state,
            self.correlation_id,
            None if self.confirmation is None else self.confirmation.to_domain(),
            self.cancel_active_orders,
        )


class LocalControlReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    command_id: str
    action: LocalControlAction
    actor_id: str
    status: str
    final: bool
    requested_at: str
    updated_at: str
    expected_state: RuntimeState
    requested_state: RuntimeState
    previous_state: RuntimeState | None
    current_state: RuntimeState | None
    transition_sequence: int | None
    rejection_code: str | None
    blockers: tuple[str, ...]
    error_code: str | None
    retryable: bool
    requires_reconciliation: bool
    status_url: str
    poll_after_ms: int | None

    @classmethod
    def from_domain(cls, receipt: LocalControlCommandReceipt) -> LocalControlReceiptResponse:
        request = receipt.request
        return cls(
            command_id=request.command_id,
            action=request.action,
            actor_id=request.actor_id,
            status=receipt.status.value,
            final=receipt.status.terminal,
            requested_at=request.requested_at.to_wire(),
            updated_at=receipt.updated_at.to_wire(),
            expected_state=request.expected_state,
            requested_state=request.action.target_state,
            previous_state=receipt.previous_state,
            current_state=receipt.current_state,
            transition_sequence=receipt.transition_sequence,
            rejection_code=receipt.rejection_code,
            blockers=receipt.blockers,
            error_code=receipt.error_code,
            retryable=receipt.retryable,
            requires_reconciliation=receipt.requires_reconciliation,
            status_url=f"/api/v1/local/controls/{request.command_id}",
            poll_after_ms=None if receipt.status.terminal else 250,
        )


def build_local_control_router(
    service: LocalControlSubmissionService,
    security: LocalConsoleSecurityController,
) -> APIRouter:
    if not isinstance(service, LocalControlSubmissionService):
        raise ValueError("service must be a LocalControlSubmissionService")
    if not isinstance(security, LocalConsoleSecurityController):
        raise ValueError("security must be a LocalConsoleSecurityController")
    router = APIRouter(prefix="/api/v1/local/controls", tags=["local-controls"])

    @router.post(
        "", response_model=LocalControlReceiptResponse, status_code=status.HTTP_202_ACCEPTED
    )
    async def submit_control(
        request: Request,
        body: LocalControlCommandBody,
        response: Response,
    ) -> LocalControlReceiptResponse:
        control = body.to_domain()
        try:
            security.authorize_control(request, control)
            receipt = await service.submit(control)
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        except ControlPreviewError as exc:
            raise control_preview_http_error(exc) from None
        except LocalControlSubmissionError as exc:
            raise _http_error(exc) from None
        response.status_code = (
            status.HTTP_200_OK if receipt.status.terminal else status.HTTP_202_ACCEPTED
        )
        return LocalControlReceiptResponse.from_domain(receipt)

    @router.get("/{command_id}", response_model=LocalControlReceiptResponse)
    async def get_control_receipt(
        request: Request, command_id: CommandId
    ) -> LocalControlReceiptResponse:
        try:
            security.authenticate(request)
            receipt = await service.get_receipt(command_id)
        except OperatorSessionError as exc:
            raise operator_session_http_error(exc) from None
        except LocalControlSubmissionError as exc:
            raise _http_error(exc) from None
        return LocalControlReceiptResponse.from_domain(receipt)

    return router


def _http_error(error: LocalControlSubmissionError) -> HTTPException:
    status_code = {
        LocalControlSubmissionFailureKind.CONFLICT: status.HTTP_409_CONFLICT,
        LocalControlSubmissionFailureKind.NOT_FOUND: status.HTTP_404_NOT_FOUND,
        LocalControlSubmissionFailureKind.UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    }[error.kind]
    headers = {"Retry-After": "1"} if status_code == status.HTTP_503_SERVICE_UNAVAILABLE else None
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.detail},
        headers=headers,
    )
