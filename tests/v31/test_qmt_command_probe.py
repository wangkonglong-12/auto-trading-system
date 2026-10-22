"""The controlled QMT diagnostic is tested without any real SDK or order."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.qmt_command_transport import (
    QmtCallbackFact,
    QmtCommandError,
    QmtCommandTransport,
    QmtOrderFact,
)
from auto_trading_system.monitoring.qmt_command_probe import (
    QmtCommandProbeResult,
    execute_qmt_command_probe,
)
from auto_trading_system.monitoring.qmt_order_preflight import (
    QmtOrderPocApproval,
    QmtOrderSide,
    evaluate_qmt_order_preflight,
)

NOW = ShanghaiTimestamp.parse("2026-09-04T14:50:00+08:00")


class FakeClock:
    def now(self) -> ShanghaiTimestamp:
        return NOW


def approved() -> QmtOrderPocApproval:
    return QmtOrderPocApproval(
        approval_id="QMT004-20260904-0001",
        revision=1,
        single_use_nonce="0123456789abcdef0123456789abcdef",
        target_host_fingerprint="0123456789abcdef",
        target_account_fingerprint=hashlib.sha256(b"A123456789").hexdigest()[:16],
        qmt001_report_sha256="1" * 64,
        qmt002_report_sha256="2" * 64,
        qmt003_report_sha256="3" * 64,
        symbol=SecurityCode.parse("600000.SH"),
        side=QmtOrderSide.BUY,
        quantity=Quantity(100),
        limit_price=Price.parse("10.00"),
        maximum_notional=Money.parse("1000.00"),
        cancel_after_seconds=30,
        valid_from=ShanghaiTimestamp.parse("2026-09-04T14:45:00+08:00"),
        valid_until=ShanghaiTimestamp.parse("2026-09-04T15:00:00+08:00"),
        operator_id="operator.one",
        reviewer_id="reviewer.two",
        change_ticket="CHANGE/QMT004/20260904-0001",
        order_remark="Q4_20260904_0001",
        manual_supervision_confirmed=True,
        qmt_emergency_control_confirmed=True,
        single_order_only_confirmed=True,
    )


class FakeTransport:
    def __init__(
        self, *, terminal: bool = True, response: bool = True, duplicate_remark: bool = False
    ) -> None:
        self.terminal, self.response = terminal, response
        self.duplicate_remark = duplicate_remark
        self.started = False
        self.stopped = False
        self.submissions = 0
        self.cancels = 0
        self.queried = 0

    def start(self) -> None:
        self.started = True

    def preflight(self, _: object) -> None:
        assert self.started

    def submit(self, _: object) -> int:
        self.submissions += 1
        return 71

    def drain(self) -> tuple[QmtCallbackFact, ...]:
        if self.response:
            self.response = False
            return (
                QmtCallbackFact(
                    "ORDER_RESPONSE",
                    NOW.value,
                    sequence=71,
                    order_id=81234,
                ),
            )
        return ()

    def query_order(self, _: int, remark: str) -> QmtOrderFact:
        self.queried += 1
        return QmtOrderFact(
            81234,
            56 if self.terminal else 54 if self.cancels else 50,
            100,
            100 if self.terminal else 0,
            "600000.SH",
            23,
            10.0,
            remark,
        )

    def query_orders(self) -> tuple[QmtOrderFact, ...]:
        fact = self.query_order(81234, approved().order_remark)
        if self.duplicate_remark:
            return (fact, replace(fact, order_id=81235))
        return (fact,)

    def cancel(self, _: object) -> int:
        self.cancels += 1
        return 72

    def stop(self) -> None:
        self.stopped = True


def run(tmp_path: Path, fake: FakeTransport) -> QmtCommandProbeResult:
    approval = approved()
    report = evaluate_qmt_order_preflight(
        approval,
        approval_sha256="a" * 64,
        config_hash="sha256:" + "b" * 64,
        broker_account_id="A123456789",
        host_fingerprint="0123456789abcdef",
        runtime_maximum_notional=Money.parse("5000.00"),
        clock=FakeClock(),
    )
    elapsed = [0.0]

    def pause(seconds: float) -> None:
        elapsed[0] += seconds

    return execute_qmt_command_probe(
        cast(QmtCommandTransport, fake),
        approval,
        report,
        tmp_path / "nonce",
        buy_type=23,
        sell_type=24,
        clock=FakeClock().now,
        elapsed=lambda: elapsed[0],
        pause=pause,
    )


def test_one_submit_and_terminal_broker_order(tmp_path: Path) -> None:
    fake = FakeTransport()
    result = run(tmp_path, fake)
    assert result.outcome == "ORDER_TERMINAL"
    assert (result.submit_sequence, result.broker_order_id, result.broker_status) == (71, 81234, 56)
    assert fake.submissions == 1 and fake.cancels == 0 and fake.stopped
    with pytest.raises(QmtCommandError, match="QMT_APPROVAL_ALREADY_USED"):
        run(tmp_path, FakeTransport())


def test_active_order_is_cancelled_once_and_confirmed(tmp_path: Path) -> None:
    fake = FakeTransport(terminal=False)
    result = run(tmp_path, fake)
    assert result.outcome == "ORDER_TERMINAL"
    assert result.cancel_sequence == 72 and result.broker_status == 54
    assert fake.submissions == 1 and fake.cancels == 1 and fake.stopped


def test_missing_async_response_recovers_by_unique_remark_without_retry(tmp_path: Path) -> None:
    fake = FakeTransport(response=False)
    result = run(tmp_path, fake)
    assert result.outcome == "ORDER_TERMINAL"
    assert result.broker_order_id == 81234
    assert fake.submissions == 1 and fake.cancels == 0 and fake.stopped


def test_missing_response_with_ambiguous_remark_stays_unknown(tmp_path: Path) -> None:
    fake = FakeTransport(response=False, duplicate_remark=True)
    result = run(tmp_path, fake)
    assert result.outcome == "UNKNOWN"
    assert result.reason == "QMT_ORDER_REMARK_AMBIGUOUS"
    assert fake.submissions == 1 and fake.cancels == 0 and fake.stopped
