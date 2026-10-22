from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from auto_trading_system.domain import (
    SHANGHAI_TIMEZONE,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.monitoring.qmt_order_preflight import (
    QmtOrderPocApproval,
    QmtOrderPreflightError,
    QmtOrderPreflightReport,
    QmtOrderSide,
    evaluate_qmt_order_preflight,
    load_qmt_order_poc_approval,
    write_qmt_order_preflight_report,
)
from auto_trading_system.monitoring.qmt_readonly import QmtProbeOutputError

HOST_FINGERPRINT = "0123456789abcdef"
ACCOUNT_ID = "A123456789"
ACCOUNT_FINGERPRINT = hashlib.sha256(ACCOUNT_ID.encode("ascii")).hexdigest()[:16]
APPROVAL_HASH = "a" * 64
CONFIG_HASH = f"sha256:{'b' * 64}"


@dataclass
class FakeClock:
    wall_time: datetime = datetime(2026, 9, 4, 14, 50, tzinfo=SHANGHAI_TIMEZONE)

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.wall_time)


def approval_payload() -> dict[str, Any]:
    return {
        "schema": "qmt-order-poc-approval/v1",
        "work_item": "QMT-004",
        "approval_id": "QMT004-20260904-0001",
        "revision": 1,
        "single_use_nonce": "0123456789abcdef0123456789abcdef",
        "target_host_fingerprint": HOST_FINGERPRINT,
        "target_account_fingerprint": ACCOUNT_FINGERPRINT,
        "qmt001_report_sha256": "1" * 64,
        "qmt002_report_sha256": "2" * 64,
        "qmt003_report_sha256": "3" * 64,
        "symbol": "600000.SH",
        "side": "BUY",
        "quantity": 100,
        "limit_price": "10.00",
        "maximum_notional": "1000.00",
        "cancel_after_seconds": 30,
        "valid_from": "2026-09-04T14:45:00+08:00",
        "valid_until": "2026-09-04T15:00:00+08:00",
        "operator_id": "operator.one",
        "reviewer_id": "reviewer.two",
        "change_ticket": "CHANGE/QMT004/20260904-0001",
        "order_remark": "Q4_20260904_0001",
        "manual_supervision_confirmed": True,
        "qmt_emergency_control_confirmed": True,
        "single_order_only_confirmed": True,
    }


def write_approval(path: Path, payload: object | None = None) -> tuple[Path, str]:
    raw = json.dumps(
        approval_payload() if payload is None else payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(raw)
    return path.resolve(), hashlib.sha256(raw).hexdigest()


def load_valid_approval(tmp_path: Path) -> QmtOrderPocApproval:
    path, digest = write_approval(tmp_path / "approval.json")
    return load_qmt_order_poc_approval(path, expected_sha256=digest)[0]


def evaluate(
    approval: QmtOrderPocApproval,
    **overrides: object,
) -> QmtOrderPreflightReport:
    arguments: dict[str, object] = {
        "approval_sha256": APPROVAL_HASH,
        "config_hash": CONFIG_HASH,
        "broker_account_id": ACCOUNT_ID,
        "host_fingerprint": HOST_FINGERPRINT,
        "runtime_maximum_notional": Money.parse("5000.00"),
        "clock": FakeClock(),
    }
    arguments.update(overrides)
    return evaluate_qmt_order_preflight(approval, **arguments)  # type: ignore[arg-type]


def test_loads_closed_hashed_approval_into_domain_values(tmp_path: Path) -> None:
    path, digest = write_approval(tmp_path / "approval.json")

    approval, actual_digest = load_qmt_order_poc_approval(path, expected_sha256=digest)

    assert actual_digest == digest
    assert approval.side is QmtOrderSide.BUY
    assert approval.symbol == SecurityCode.parse("600000.SH")
    assert approval.quantity == Quantity(100)
    assert approval.limit_price == Price.parse("10.00")
    assert approval.calculated_notional == Money.parse("1000.00").amount


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(schema="wrong"), "QMT_ORDER_APPROVAL_PROTOCOL_INVALID"),
        (lambda value: value.pop("reviewer_id"), "QMT_ORDER_APPROVAL_SHAPE_INVALID"),
        (lambda value: value.update(extra=True), "QMT_ORDER_APPROVAL_SHAPE_INVALID"),
        (lambda value: value.update(revision=True), "QMT_ORDER_APPROVAL_FIELD_TYPE_INVALID"),
        (lambda value: value.update(side="HOLD"), "QMT_ORDER_APPROVAL_SIDE_INVALID"),
        (lambda value: value.update(quantity=0), "QMT_ORDER_APPROVAL_QUANTITY_INVALID"),
        (lambda value: value.update(limit_price="0"), "QMT_ORDER_APPROVAL_VALUE_INVALID"),
        (
            lambda value: value.update(maximum_notional="999.99"),
            "QMT_ORDER_APPROVAL_NOTIONAL_INCONSISTENT",
        ),
        (
            lambda value: value.update(cancel_after_seconds=4),
            "QMT_ORDER_APPROVAL_CANCEL_WINDOW_INVALID",
        ),
        (
            lambda value: value.update(valid_until="2026-09-04T14:45:59+08:00"),
            "QMT_ORDER_APPROVAL_TIME_WINDOW_INVALID",
        ),
        (
            lambda value: value.update(valid_until="2026-09-04T15:15:01+08:00"),
            "QMT_ORDER_APPROVAL_TIME_WINDOW_INVALID",
        ),
        (
            lambda value: value.update(reviewer_id="operator.one"),
            "QMT_ORDER_APPROVAL_REVIEW_INVALID",
        ),
        (
            lambda value: value.update(order_remark="含中文"),
            "QMT_ORDER_APPROVAL_REMARK_INVALID",
        ),
        (
            lambda value: value.update(manual_supervision_confirmed=False),
            "QMT_ORDER_APPROVAL_ACKNOWLEDGEMENT_MISSING",
        ),
        (
            lambda value: value.update(target_host_fingerprint="BAD"),
            "QMT_ORDER_APPROVAL_FINGERPRINT_INVALID",
        ),
        (
            lambda value: value.update(qmt003_report_sha256="BAD"),
            "QMT_ORDER_APPROVAL_EVIDENCE_HASH_INVALID",
        ),
    ],
)
def test_rejects_invalid_approval_values(
    tmp_path: Path,
    mutation: Any,
    code: str,
) -> None:
    payload = approval_payload()
    mutation(payload)
    path, digest = write_approval(tmp_path / "invalid.json", payload)

    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(path, expected_sha256=digest)

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "QMT_ORDER_APPROVAL_SIZE_INVALID"),
        (b"not-json", "QMT_ORDER_APPROVAL_JSON_INVALID"),
        (b"[]", "QMT_ORDER_APPROVAL_SHAPE_INVALID"),
        (b'{"schema":"a","schema":"b"}', "QMT_ORDER_APPROVAL_DUPLICATE_KEY"),
        (b"\xff", "QMT_ORDER_APPROVAL_JSON_INVALID"),
    ],
)
def test_rejects_malformed_approval_files(tmp_path: Path, raw: bytes, code: str) -> None:
    path = (tmp_path / "malformed.json").resolve()
    path.write_bytes(raw)

    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(path, expected_sha256=hashlib.sha256(raw).hexdigest())

    assert exc_info.value.code == code


def test_rejects_bad_path_hash_and_oversized_approval(tmp_path: Path) -> None:
    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(Path("relative.json"), expected_sha256="0" * 64)
    assert exc_info.value.code == "QMT_ORDER_APPROVAL_PATH_INVALID"

    missing = (tmp_path / "missing.json").resolve()
    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(missing, expected_sha256="BAD")
    assert exc_info.value.code == "QMT_ORDER_APPROVAL_EXPECTED_HASH_INVALID"
    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(missing, expected_sha256="0" * 64)
    assert exc_info.value.code == "QMT_ORDER_APPROVAL_READ_FAILED"

    path, _ = write_approval(tmp_path / "approval.json")
    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(path, expected_sha256="0" * 64)
    assert exc_info.value.code == "QMT_ORDER_APPROVAL_HASH_MISMATCH"

    oversized = (tmp_path / "oversized.json").resolve()
    oversized.write_bytes(b"x" * 65_537)
    with pytest.raises(QmtOrderPreflightError) as exc_info:
        load_qmt_order_poc_approval(
            oversized,
            expected_sha256=hashlib.sha256(oversized.read_bytes()).hexdigest(),
        )
    assert exc_info.value.code == "QMT_ORDER_APPROVAL_SIZE_INVALID"


def test_preflight_passes_and_emits_only_bound_fingerprinted_evidence(tmp_path: Path) -> None:
    approval = load_valid_approval(tmp_path)

    report = evaluate(approval)

    assert report.outcome.value == "PASS"
    assert report.exit_code == 0
    assert report.failure_code is None
    assert len(report.execution_binding_sha256) == 64
    payload = report.to_payload()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert payload["live_order_submitted"] is False
    assert ACCOUNT_ID not in rendered
    assert approval.symbol.to_wire() not in rendered
    assert approval.single_use_nonce not in rendered
    assert approval.order_remark not in rendered


@pytest.mark.parametrize(
    ("overrides", "code", "exit_code"),
    [
        ({"host_fingerprint": "f" * 16}, "QMT_ORDER_HOST_MISMATCH", 2),
        ({"broker_account_id": "B987654321"}, "QMT_ORDER_ACCOUNT_MISMATCH", 2),
        (
            {"runtime_maximum_notional": Money.parse("999.99")},
            "QMT_ORDER_RUNTIME_NOTIONAL_EXCEEDED",
            2,
        ),
        (
            {"clock": FakeClock(datetime(2026, 9, 4, 14, 44, tzinfo=SHANGHAI_TIMEZONE))},
            "QMT_ORDER_APPROVAL_NOT_YET_VALID",
            2,
        ),
        (
            {"clock": FakeClock(datetime(2026, 9, 4, 15, 1, tzinfo=SHANGHAI_TIMEZONE))},
            "QMT_ORDER_APPROVAL_EXPIRED",
            2,
        ),
        ({"host_fingerprint": "invalid"}, "QMT_ORDER_HOST_FINGERPRINT_INVALID", 3),
    ],
)
def test_preflight_fails_closed_for_binding_and_time_mismatches(
    tmp_path: Path,
    overrides: dict[str, object],
    code: str,
    exit_code: int,
) -> None:
    report = evaluate(load_valid_approval(tmp_path), **overrides)

    assert report.failure_code == code
    assert report.exit_code == exit_code


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"approval_sha256": "BAD"}, "QMT_ORDER_PREFLIGHT_APPROVAL_HASH_INVALID"),
        ({"config_hash": "BAD"}, "QMT_ORDER_PREFLIGHT_CONFIG_HASH_INVALID"),
        ({"broker_account_id": "BAD-ACCOUNT"}, "QMT_ORDER_PREFLIGHT_ACCOUNT_INVALID"),
        ({"runtime_maximum_notional": "5000"}, "QMT_ORDER_PREFLIGHT_RUNTIME_LIMIT_INVALID"),
    ],
)
def test_preflight_rejects_invalid_evaluation_inputs(
    tmp_path: Path,
    overrides: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(QmtOrderPreflightError) as exc_info:
        evaluate(load_valid_approval(tmp_path), **overrides)

    assert exc_info.value.code == code


def test_direct_approval_construction_rejects_wrong_domain_types(tmp_path: Path) -> None:
    approval = load_valid_approval(tmp_path)

    with pytest.raises(QmtOrderPreflightError) as exc_info:
        replace(approval, symbol="600000.SH")  # type: ignore[arg-type]
    assert exc_info.value.code == "QMT_ORDER_APPROVAL_SYMBOL_INVALID"


def test_report_writer_is_immutable_and_checks_forbidden_values(tmp_path: Path) -> None:
    approval = load_valid_approval(tmp_path)
    report = evaluate(approval)
    destination = (tmp_path / "report.json").resolve()

    digest = write_qmt_order_preflight_report(destination, report)

    assert digest == hashlib.sha256(destination.read_bytes()).hexdigest()
    with pytest.raises(QmtProbeOutputError) as exc_info:
        write_qmt_order_preflight_report(destination, report)
    assert exc_info.value.code == "QMT_PROBE_OUTPUT_EXISTS"

    with pytest.raises(QmtProbeOutputError) as exc_info:
        write_qmt_order_preflight_report(
            (tmp_path / "blocked.json").resolve(),
            report,
            forbidden_values=(report.approval_id,),
        )
    assert exc_info.value.code == "QMT_PROBE_REDACTION_FAILED"
