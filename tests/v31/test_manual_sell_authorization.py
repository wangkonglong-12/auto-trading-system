"""Authenticated owner mailbox reaches real Mock/J1 sale execution."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep, time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from test_manual_sell import manager, pump

from auto_trading_system.apps.trade_runtime.engine_simulation import open_offline_engine
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
    next_day_plan,
)
from auto_trading_system.control.ipc_protocol import Request
from auto_trading_system.control.node_authorization import NodeAuthorization
from auto_trading_system.control.owner_mailbox import OwnerMailbox
from auto_trading_system.domain import ShanghaiTimestamp, SystemClock
from auto_trading_system.security.operator_password import OperatorPasswordHasher
from auto_trading_system.security.operator_session import LocalOperatorSessionManager


def test_authenticated_sale_preview_confirm_lookup_and_no_caller_overrides(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    with (
        open_offline_engine(
            tmp_path,
            clock=lambda: D1_NOW,
            plan_payload=next_day_plan(),
            opening_reference=opening,
            broker_opening_quantity=100,
        ) as rt,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        seller = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, seller, lambda s: s.state == "REVIEW_REQUIRED")
        sessions = LocalOperatorSessionManager(
            SecretStr(OperatorPasswordHasher().hash_password("manual-sell-test-password")),
            actor_id="test-operator",
            clock=SimpleNamespace(now=lambda: ShanghaiTimestamp(D1_NOW)),
            monotonic_clock=SystemClock(),
        )
        issued = sessions.login(SecretStr("manual-sell-test-password"))
        mailbox: OwnerMailbox[Any] = OwnerMailbox()
        auth = NodeAuthorization(
            rt.kernel, mailbox, sessions, clock=lambda: D1_NOW, manual_sell=seller
        )

        def invoke(action: str, body: dict[str, Any]) -> Any:
            request = Request(
                request_id=uuid4().hex,
                engine_boot_id=rt.kernel.boot_id,
                sender_role="operator",
                deadline_ms=int(time() * 1000) + 7500,
                action=action,
                payload={
                    "session_token": issued.session_token,
                    "csrf_token": issued.csrf_token,
                    **body,
                },
            )
            result = pool.submit(auth.handle, request)
            until = monotonic() + 5
            while not result.done() and monotonic() < until:
                rt.kernel.poll()
                mailbox.poll()
                seller.poll()
                sleep(0.001)
            return result.result(1)

        preview = invoke(
            "PREVIEW_MANUAL_SELL", {"symbol": "000001", "limit_price": "10", "quantity": 100}
        )
        assert rt.broker.query_orders() == ()
        with pytest.raises(ValidationError):
            invoke(
                "CONFIRM_MANUAL_SELL", {"operation_id": preview["operation_id"], "quantity": 200}
            )
        rt.broker.queue_submit(fill_scenario(100, full=True))
        receipt = invoke("CONFIRM_MANUAL_SELL", {"operation_id": preview["operation_id"]})
        assert receipt["operation_id"] == preview["operation_id"]
        pump(rt, seller, lambda s: bool(s.lots) and s.lots[0].remaining == 0)
        actual = invoke("MANUAL_SELL_RESULT", {"operation_id": preview["operation_id"]})
        assert actual["children"][0]["status"] == "FILLED"
        invoke("CONFIRM_MANUAL_SELL", {"operation_id": preview["operation_id"]})
        assert len(rt.broker.query_orders()) == 1 and rt.kernel.snapshot().grant_id is None
