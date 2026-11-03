"""Real journal and Mock trading across an unsealed sell day, never QMT."""

import json
from datetime import timedelta
from time import monotonic

import pytest
from test_manual_sell import manager, prepare, pump, session

from auto_trading_system.apps.trade_runtime.engine_simulation import open_offline_engine, wait_for
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
    next_day_plan,
    settle,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.checkpoint_blobs import read_checkpoint
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import digest
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.reconcile.historical_opening import load_recovery_opening

TODAY = (D1_NOW + timedelta(days=1)).replace(hour=14, minute=41)


def unsealed_sale(root, filled=100):
    opening = close_buy_day(root)
    plan = next_day_plan()
    with open_offline_engine(
        root,
        clock=lambda: D1_NOW,
        plan_payload=plan,
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        seller = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, seller, lambda s: s.state == "REVIEW_REQUIRED")
        preview = prepare(rt, seller)
        rt.broker.queue_submit(fill_scenario(filled, full=filled == 100))
        facts = seller.query_confirmation(session(), str(preview["operation_id"]))
        seller.confirm(session(), str(preview["operation_id"]), facts.result(2))
        pump(
            rt,
            seller,
            lambda s: bool(s.lots)
            and s.lots[0].remaining == 100 - filled
            and rt.kernel.critical_idle,
        )
        if filled < 100:
            child = next(s for s in rt.kernel._known.values() if s.action_type == "NEW")
            rt.kernel.operator_cancel_order(child.client_order_id)
            rt.kernel._next_observation = 0
            pump(
                rt,
                seller,
                lambda s: rt.kernel.critical_idle
                and all(v.phase == "TERMINAL_CONFIRMED" for v in s.orders),
            )
        account = rt.kernel.account_id
        calendar = rt.kernel._calendar
    return opening, account, calendar


def today_plan():
    return PlanPayload.model_validate_json(
        next_day_plan().model_dump_json().replace("2026-09-08", "2026-09-09")
    )


@pytest.mark.parametrize("filled", [100, 40])
def test_unsealed_sell_day_restores_current_quantity_and_restart_is_idempotent(tmp_path, filled):
    opening, _account, _calendar = unsealed_sale(tmp_path, filled)
    expected = 100 - filled
    proof_hash = None
    for _ in range(2):
        with open_offline_engine(
            tmp_path,
            clock=lambda: TODAY,
            plan_payload=today_plan(),
            opening_reference=opening,
            broker_opening_quantity=expected,
        ) as rt:
            rt.kernel.start_recovery()
            state = wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
            assert state.recovery.status == "CLEAR" and state.halted and state.grant_id is None
            assert sum(lot.remaining for lot in state.lots) == expected
            assert state.recovery.reserved_buy_value == "0.00"
            assert rt.kernel._records == [] and rt.broker.query_orders() == ()
            proof = rt.kernel._opening_proof
            assert proof
            if proof_hash is not None:
                assert digest(proof) == proof_hash
            proof_hash = digest(proof)
            saved = rt.journal.request(
                lambda store, identity=proof_hash: read_checkpoint(
                    store, "recovered-opening:" + identity[7:]
                ),
                deadline=monotonic() + 2,
            ).result(2)
            assert saved == proof
            assert rt.kernel._scan.prefix.high_watermark >= 2
    if filled == 100:
        # Yesterday's completed manual sale cannot occupy today's buy slot/budget.
        with open_offline_engine(
            tmp_path, clock=lambda: TODAY, plan_payload=today_plan(), opening_reference=opening
        ) as rt:
            rt.kernel.start_recovery()
            wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
            calendar = rt.kernel._calendar
            rt.kernel._calendar = calendar.model_copy(
                update={"sessions": (*calendar.sessions, (TODAY + timedelta(days=1)).date())}
            )
            rt.confirmed_arm()
            rt.broker.queue_submit(fill_scenario(100, full=True))
            settle(rt, rt.buy())
            assert len(rt.broker.query_orders()) == 1
            assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == 100


@pytest.mark.parametrize(
    "mode", ["missing_fill", "no_terminal", "duplicate_fill", "conflicting_fill", "wrong_account"]
)
def test_incomplete_or_conflicting_history_never_poison_writer_health(tmp_path, mode):
    opening, account, calendar = unsealed_sale(tmp_path)

    class FactsStore(APSWCriticalStore):
        def read_broker_facts(self, client_ids):
            bodies = super().read_broker_facts(client_ids)
            if mode == "missing_fill":
                return tuple(b for b in bodies if "broker_trade_id" not in json.loads(b)["data"])
            if mode == "no_terminal":
                return tuple(
                    b
                    for b in bodies
                    if json.loads(b)["data"].get("status")
                    not in {"FILLED", "CANCELLED", "REJECTED"}
                )
            trade = next(b for b in bodies if "broker_trade_id" in json.loads(b)["data"])
            if mode == "duplicate_fill":
                return (*bodies, trade)
            modified = json.loads(trade)
            if mode == "wrong_account":
                modified["data"]["account_id"] = "another-account"
            else:
                modified["data"]["quantity"] = 99
                modified["data"]["gross_amount"] = "990"
            return (*bodies, json.dumps(modified).encode())

    writer = CriticalJournalOwner(lambda: FactsStore(tmp_path / "critical/journal-v2.db", account))
    writer.start()
    try:

        def load(store):
            return load_recovery_opening(
                store, opening, account_id=account, today=TODAY.date(), calendar=calendar
            )

        result = writer.request(load, deadline=monotonic() + 3).result(3)
        assert writer.healthy
        if mode == "duplicate_fill":
            assert result.error is None and result.baseline.lots[0].remaining == 0
        else:
            assert result.error and result.baseline is None
        assert writer.request(lambda store: True, deadline=monotonic() + 2).result(2)
    finally:
        writer.close()


def test_current_account_mismatch_still_blocks_recovered_holdings(tmp_path):
    opening, _account, _calendar = unsealed_sale(tmp_path, 40)
    with open_offline_engine(
        tmp_path,
        clock=lambda: TODAY,
        plan_payload=today_plan(),
        opening_reference=opening,
        broker_opening_quantity=60,
    ) as rt:
        read = rt.kernel._account_reader
        rt.kernel._account_reader = lambda: read().model_copy(update={"positions": ()})
        rt.kernel.start_recovery()
        state = wait_for(rt.kernel, lambda s: s.state == "RECOVERY_BLOCKED")
        assert "POSITION_OWNERSHIP_UNRESOLVED" in state.recovery.issue_codes
        assert state.grant_id is None and state.halted and rt.journal.healthy


def test_manual_buy_without_any_seal_is_recovered_from_complete_facts(tmp_path):
    from test_manual_buy import (
        controller,
    )
    from test_manual_buy import (
        prepare as buy_preview,
    )
    from test_manual_buy import (
        session as buy_session,
    )
    from test_manual_buy import (
        settle as buy_settle,
    )

    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        buyer = controller(rt)
        preview = buy_preview(rt, buyer)
        rt.broker.queue_submit(fill_scenario(100, full=True))
        buyer.confirm(buy_session(), str(preview["preview_id"]))
        buy_settle(
            rt, lambda s: bool(s.lots) and s.lots[0].remaining == 100 and rt.kernel.critical_idle
        )
    with open_offline_engine(
        tmp_path, clock=lambda: D1_NOW, plan_payload=next_day_plan(), broker_opening_quantity=100
    ) as rt:
        rt.kernel.start_recovery()
        state = wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        assert state.recovery.status == "CLEAR" and state.recovery.reserved_buy_value == "0.00"
        assert sum(lot.remaining for lot in state.lots) == 100
        assert rt.kernel._opening_proof and not rt.broker.query_orders()


def test_second_unsealed_sell_day_binds_the_recovered_opening_hash(tmp_path):
    from auto_trading_system.runtime.manual_sell import ManualSellInput

    opening, account, calendar = unsealed_sale(tmp_path, 40)
    with open_offline_engine(
        tmp_path,
        clock=lambda: TODAY,
        plan_payload=today_plan(),
        opening_reference=opening,
        broker_opening_quantity=60,
    ) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        seller = manager(rt)
        value = ManualSellInput(symbol="000001", limit_price="10", quantity=60)
        preview = seller.preview(session(), value, seller.query(value).result(2))
        pump(rt, seller, lambda _: rt.kernel.critical_idle)
        rt.broker.queue_submit(fill_scenario(60, full=True))
        facts = seller.query_confirmation(session(), str(preview["operation_id"]))
        seller.confirm(session(), str(preview["operation_id"]), facts.result(2))
        pump(
            rt,
            seller,
            lambda s: bool(s.lots)
            and all(lot.remaining == 0 for lot in s.lots)
            and rt.kernel.critical_idle,
        )
    following = (TODAY + timedelta(days=1)).date()
    calendar = calendar.model_copy(update={"sessions": (*calendar.sessions, following)})
    writer = CriticalJournalOwner(
        lambda: APSWCriticalStore(tmp_path / "critical/journal-v2.db", account)
    )
    writer.start()
    try:
        loaded = writer.request(
            lambda store: load_recovery_opening(
                store, opening, account_id=account, today=following, calendar=calendar
            ),
            deadline=monotonic() + 3,
        ).result(3)
        assert loaded.error is None
        assert sum(lot.remaining for lot in loaded.baseline.lots) == 0
        assert writer.healthy
    finally:
        writer.close()


def test_new_automatic_buy_carries_ownership_for_unsealed_recovery(tmp_path):
    from test_entry_owner import armed

    from auto_trading_system.apps.trade_runtime.entry_simulation import publish_entry, pump_entry

    with armed(tmp_path) as (rt, owner, clock):
        rt.broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(owner, clock, 1)
        pump_entry(owner, lambda: bool(rt.kernel.snapshot().lots) and rt.kernel.critical_idle)
        item = next(s for s in rt.kernel._known.values() if s.action_type == "NEW")
        assert item.entry_binding.strategy_id == rt.plan.payload.strategy_id
    with open_offline_engine(
        tmp_path, clock=lambda: D1_NOW, plan_payload=next_day_plan(), broker_opening_quantity=100
    ) as rt:
        rt.kernel.start_recovery()
        state = wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        assert state.recovery.status == "CLEAR"
        assert sum(lot.remaining for lot in state.lots) == 100
        assert state.recovery.reserved_buy_value == "0.00" and state.grant_id is None
