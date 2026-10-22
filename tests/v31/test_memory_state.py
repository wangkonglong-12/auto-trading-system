from __future__ import annotations

import pytest

from auto_trading_system.domain import Money, Price
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.runtime.memory_state import (
    AccountObservation,
    MarketWindow,
    QuotePoint,
    ReservationBook,
)


def test_market_window_is_bounded_and_gap_is_not_silently_repaired() -> None:
    window = MarketWindow(capacity=3, horizon_ns=60)
    for seq in range(1, 5):
        window.add(QuotePoint(seq, seq, Price.parse("10")))
    snapshot = window.snapshot()
    assert len(snapshot.points) == 3 and not snapshot.path_complete
    window.add(QuotePoint(3, 6, Price.parse("9")))
    assert window.snapshot().points == snapshot.points
    window.add(QuotePoint(8, 100, Price.parse("11")))
    assert len(window.snapshot().points) == 1
    assert not window.snapshot().path_complete


def test_reservations_deduct_only_unreflected_cash(submission: Submission) -> None:
    book = ReservationBook()
    before = AccountObservation("before", Money.parse("3000"), frozenset(), (), True)
    book.reserve(submission, before)
    assert book.available_cash(before) == Money.parse("1995")
    reflected = AccountObservation(
        "after",
        Money.parse("1995"),
        frozenset({submission.client_order_id}),
        (),
        True,
    )
    assert book.available_cash(reflected) == Money.parse("1995")
    with pytest.raises(ValueError, match="already exists"):
        book.reserve(submission, before)
    with pytest.raises(ValueError, match="active"):
        book.reserve(submission.model_copy(update={"client_order_id": "ord_r310000002"}), before)
    with pytest.raises(ValueError, match="reconciliation"):
        book.resolve_terminal(submission.client_order_id, reconciled=False)
    assert len(book.snapshot()) == 1
    book.resolve_terminal(submission.client_order_id, reconciled=True)
    assert book.snapshot() == ()


def test_unknown_account_and_insufficient_cash_fail_closed(submission: Submission) -> None:
    book = ReservationBook()
    with pytest.raises(ValueError, match="coherent"):
        book.reserve(
            submission, AccountObservation("x", Money.parse("3000"), frozenset(), (), False)
        )
    with pytest.raises(ValueError, match="insufficient"):
        book.reserve(
            submission, AccountObservation("x", Money.parse("1000"), frozenset(), (), True)
        )
    sell = submission.model_copy(update={"side": "SELL"})
    with pytest.raises(ValueError, match="sellable"):
        book.reserve(sell, AccountObservation("x", Money.parse("1000"), frozenset(), (), True))
    book.reserve(
        sell, AccountObservation("x", Money.parse("1000"), frozenset(), ((sell.symbol, 100),), True)
    )
