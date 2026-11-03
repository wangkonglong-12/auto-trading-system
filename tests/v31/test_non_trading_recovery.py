"""Non-session account recovery, with real local journal and Mock broker only."""

from datetime import date, datetime
from time import monotonic

import pytest

from auto_trading_system.apps.trade_runtime.engine_simulation import open_offline_engine, wait_for
from auto_trading_system.journal.checkpoint_blobs import write_checkpoint
from auto_trading_system.protocol.v2.canonical import digest
from auto_trading_system.reconcile.journal_scan import PrefixDigest
from auto_trading_system.reconcile.portfolio_checkpoint import PortfolioCheckpoint, load_opening
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    LotIntent,
    PositionLot,
    project_lots,
)
from auto_trading_system.runtime.engine_v2 import CN

FRIDAY = date(2026, 9, 11)
CALENDAR = ExchangeCalendar(
    version="offline-calendar-v1",
    sessions=(date(2026, 9, 10), FRIDAY, date(2026, 9, 15), date(2026, 9, 16)),
)


def closing(account, quantity):
    lots = (
        ()
        if not quantity
        else (
            PositionLot(
                lot_id="lot-weekend",
                strategy_id="tail-entry",
                symbol="000001.SZ",
                trade_date_acquired=FRIDAY,
                eligible_sell_date=date(2026, 9, 15),
                calendar_version=CALENDAR.version,
                bought_quantity=quantity,
            ),
        )
    )
    return PortfolioCheckpoint(
        account_id=account,
        trade_date=FRIDAY,
        calendar_version=CALENDAR.version,
        recovery_report_hash=digest(b"offline-clear"),
        submission_high_watermark=0,
        submission_prefix_hash=PrefixDigest().payload_hash,
        lots=lots,
    )


@pytest.mark.parametrize(
    "day", [date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 14), date(2026, 9, 15)]
)
@pytest.mark.parametrize("quantity", [0, 100])
def test_weekend_holiday_and_next_session_recover_without_grant(tmp_path, day, quantity):
    now = datetime.combine(day, datetime.min.time(), CN).replace(hour=10)
    with open_offline_engine(
        tmp_path, clock=lambda: now, preload_plan=False, broker_opening_quantity=quantity
    ) as rt:
        rt.kernel._calendar = CALENDAR
        rt.kernel.trade_date = now.date()
        checkpoint = closing(rt.kernel.account_id, quantity)
        reference = checkpoint.reference()
        rt.journal.request(
            lambda store: write_checkpoint(store, reference.key, checkpoint.body()),
            deadline=monotonic() + 2,
        ).result(2)
        rt.kernel._opening_reference = reference
        rt.kernel.start_recovery()
        state = wait_for(rt.kernel, lambda s: s.state in {"REVIEW_REQUIRED", "RECOVERY_BLOCKED"})
        assert rt.kernel.recovery_error() is None
        assert state.recovery.status == "CLEAR" and state.state == "REVIEW_REQUIRED"
        assert sum(lot.remaining for lot in state.lots) == quantity
        assert state.halted and state.grant_id is None and rt.journal.healthy
        assert rt.kernel._opening_hash == reference.payload_hash
        assert rt.broker.query_orders() == ()
        if day not in CALENDAR.sessions:
            with pytest.raises(ValueError, match="portfolio date"):
                project_lots(
                    account_id=rt.kernel.account_id,
                    as_of=day,
                    calendar=CALENDAR,
                    intents=(LotIntent(rt.buy(), "tail-entry"),),
                    trades=(),
                )


def test_non_session_does_not_hide_real_account_mismatch(tmp_path):
    now = datetime(2026, 9, 12, 10, tzinfo=CN)
    with open_offline_engine(tmp_path, clock=lambda: now, preload_plan=False) as rt:
        rt.kernel._calendar = CALENDAR
        rt.kernel.trade_date = now.date()
        checkpoint = closing(rt.kernel.account_id, 100)
        reference = checkpoint.reference()
        rt.journal.request(
            lambda store: write_checkpoint(store, reference.key, checkpoint.body()),
            deadline=monotonic() + 2,
        ).result(2)
        rt.kernel._opening_reference = reference
        rt.kernel.start_recovery()
        state = wait_for(rt.kernel, lambda s: s.state == "RECOVERY_BLOCKED")
        assert "POSITION_OWNERSHIP_UNRESOLVED" in state.recovery.issue_codes
        assert state.halted and state.grant_id is None and rt.journal.healthy


@pytest.mark.parametrize(
    "case", ["account", "hash", "calendar", "same_day", "future", "outside_coverage"]
)
def test_non_session_opening_still_rejects_invalid_evidence(case):
    checkpoint = closing("simulation-account", 0)
    reference = checkpoint.reference()
    day = date(2026, 9, 12)
    account = checkpoint.account_id
    body = checkpoint.body()
    calendar = CALENDAR
    if case == "account":
        account = "other-account"
    elif case == "hash":
        body += b" "
    elif case == "calendar":
        calendar = CALENDAR.model_copy(update={"version": "other-calendar"})
    elif case == "same_day":
        day = FRIDAY
    elif case == "future":
        day = date(2026, 9, 10)
    else:
        day = date(2026, 9, 17)
    with pytest.raises(ValueError):
        load_opening(
            body,
            reference,
            account_id=account,
            trade_date=day,
            calendar=calendar,
            allow_historical=True,
        )


@pytest.mark.parametrize("filled", [100, 40])
def test_unsealed_trade_day_before_holiday_replays_actual_sale(tmp_path, filled):
    from test_historical_opening import TODAY, unsealed_sale

    reference, _, original_calendar = unsealed_sale(tmp_path, filled)
    calendar = original_calendar.model_copy(
        update={
            "sessions": (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 10), date(2026, 9, 11))
        }
    )
    with open_offline_engine(
        tmp_path, clock=lambda: TODAY, preload_plan=False, broker_opening_quantity=100 - filled
    ) as rt:
        rt.kernel._calendar = calendar
        rt.kernel.trade_date = TODAY.date()
        rt.kernel._opening_reference = reference
        rt.kernel.start_recovery()
        state = wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        assert state.recovery.status == "CLEAR"
        assert sum(lot.remaining for lot in state.lots) == 100 - filled
        assert rt.kernel._opening_proof and rt.journal.healthy
        assert state.halted and state.grant_id is None and rt.broker.query_orders() == ()
