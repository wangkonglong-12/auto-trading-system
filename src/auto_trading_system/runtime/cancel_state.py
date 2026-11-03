"""Durable, request-bound proof of a failed cancellation; uncertainty is not failure."""

from pydantic import AwareDatetime, Field

from auto_trading_system.journal.critical_models import RecoveryRecord, Submission
from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest


class CancelFailure(WireModel):
    cancel_request_id: str
    submission_hash: Sha256
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    occurred_at: AwareDatetime


def failure_key(identity: str) -> str:
    return "cancel-failure:" + identity


def load_failures(
    store: CriticalStorePort, records: tuple[RecoveryRecord, ...]
) -> dict[str, CancelFailure]:
    failures = {}
    for record in records:
        item: Submission = record.submission
        if item.action_type != "CANCEL":
            continue
        body = store.read_evidence("broker_facts", failure_key(item.client_order_id))
        if body is None:
            continue
        failure = CancelFailure.model_validate_json(body)
        if (
            failure.cancel_request_id != item.client_order_id
            or failure.submission_hash != digest(canonical_bytes(item.model_dump(mode="json")))
            or failure.occurred_at.date() != item.trade_date
        ):
            raise ValueError("cancellation failure does not match its durable request")
        failures[item.client_order_id] = failure
    return failures
