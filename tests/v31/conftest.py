from __future__ import annotations

from datetime import date

import pytest

from auto_trading_system.journal.critical_models import ActionTarget, Submission


@pytest.fixture
def submission() -> Submission:
    return Submission(
        client_order_id="ord_r310000001",
        account_id="simulation-account",
        trade_date=date(2026, 9, 7),
        action_type="NEW",
        plan_hash="sha256:" + "a" * 64,
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        limit_price="10.00",
        maximum_amount="1005.00",
        remark_token="R310000001",
        engine_boot_id="boot-1",
        permission_epoch=2,
        target=ActionTarget(action_id="action-1", target_remaining=100, rule_id="entry-v1"),
    )
