"""Execution lifecycle, durable retry and authenticated upload regression checks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_tail_feed import node_config

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.ipc_protocol import RPCClient
from auto_trading_system.protocol.executions.models import (
    Acknowledgment,
    Batch,
    Candidate,
    Draft,
    Event,
    Identity,
    OrderFact,
    Progress,
    Receipt,
    SubmissionFact,
    TradeFact,
    body_hash,
)
from auto_trading_system.protocol.executions.projection import State, apply_event, summary
from auto_trading_system.protocol.v2.canonical import digest
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.execution_store import ExecutionStore
from auto_trading_system.sync.execution_upload import (
    ExecutionHttp,
    ExecutionUploader,
    ExecutionUploadError,
)

NOW = datetime.fromisoformat("2026-10-23T14:45:00+08:00")
IDENTITY = Identity(
    account_id="local-alias",
    strategy_id="TAIL_CLOSE",
    selection_date=NOW.date(),
    symbol="000001.SZ",
    environment="TEST",
)


def event(
    fact: dict[str, Any], seq: int, *, now: datetime = NOW, identity: Identity = IDENTITY
) -> Event:
    value = {
        "identity": identity.model_dump(mode="json"),
        "source_key": digest((str(seq) + json.dumps(fact, sort_keys=True)).encode()),
        "occurred_at": now.isoformat(),
        "fact": fact,
        "seq": seq,
    }
    return Event.model_validate_json(json.dumps(value))


def candidate(phase: str = "1440") -> dict[str, Any]:
    return {
        "kind": "CANDIDATE",
        "phase": phase,
        "source_run_id": "run-" + phase,
        "name": "平安银行",
        "target_amount": "2000.00",
    }


def intent(
    key: str = "buy-1",
    side: str = "BUY",
    status: str = "SDK_ACCEPTED",
    quantity: int = 100,
    **changes: object,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "SUBMISSION",
        "client_order_id": key,
        "side": side,
        "action": "NEW",
        "original_client_order_id": None,
        "quantity": quantity,
        "price": "11.00" if side == "BUY" else "12.00",
        "status": status,
        "lot_id": "lot-1" if side == "BUY" else None,
    }
    value.update(changes)
    return value


def order(
    key: str = "buy-1",
    side: str = "BUY",
    status: str = "FILLED",
    filled: int = 100,
    **changes: object,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "ORDER",
        "client_order_id": key,
        "broker_order_id": "broker-" + key,
        "side": side,
        "quantity": 100,
        "filled_quantity": filled,
        "price": "11.00" if side == "BUY" else "12.00",
        "status": status,
        "reason_code": "BROKER_REJECTED" if status == "REJECTED" else "CONFIRMED",
    }
    value.update(changes)
    return value


def trade(
    key: str = "buy-1",
    side: str = "BUY",
    quantity: int = 100,
    price: str = "10.00",
    trade_id: str = "fill-1",
) -> dict[str, Any]:
    from decimal import Decimal

    return {
        "kind": "TRADE",
        "client_order_id": key,
        "broker_order_id": "broker-" + key,
        "broker_trade_id": trade_id,
        "side": side,
        "quantity": quantity,
        "price": price,
        "amount": f"{Decimal(price) * quantity:.2f}",
    }


def fold(facts: list[dict[str, Any]]) -> State:
    state = State(identity=IDENTITY)
    for seq, fact in enumerate(facts, 1):
        state = apply_event(state, event(fact, seq))
    return state


def receipt(batch: Batch) -> Receipt:
    return Receipt(
        schema_version=1,
        acknowledgments=tuple(
            Acknowledgment(event_id=e.event_id, event_hash=body_hash(e)) for e in batch.events
        ),
    )


def test_one_master_partial_buy_retry_then_next_day_sell_and_failure_history() -> None:
    state = fold(
        [
            candidate(),
            candidate("1445"),
            {
                "kind": "PROGRESS",
                "plan_hash": "sha256:" + "a" * 64,
                "status": "ORDER_ACTIVE",
                "target_quantity": 200,
            },
            intent(),
            order(),
            trade(),
            intent("buy-2", status="UNCERTAIN"),
            order("buy-2", status="REJECTED", filled=0),
        ]
    )
    assert summary(state).bought_quantity == 100 and summary(state).buy_status == "PARTIAL"
    for fact in (intent("buy-3"), order("buy-3"), trade("buy-3", price="11.00", trade_id="fill-3")):
        state = apply_event(state, event(fact, state.last_seq + 1))
    assert summary(state).buy_average_price == "10.50000000"
    next_day = NOW + timedelta(days=3)
    for fact in (
        intent("sell-1", "SELL", quantity=200),
        order("sell-1", "SELL", "REJECTED", 0, quantity=200),
    ):
        state = apply_event(state, event(fact, state.last_seq + 1, now=next_day))
    main = summary(state)
    assert (
        main.buy_status == "FILLED"
        and main.sell_status == "FAILED"
        and main.remaining_quantity == 200
    )
    for fact in (
        intent("sell-2", "SELL", quantity=200),
        order("sell-2", "SELL", quantity=200, filled_quantity=200),
        trade("sell-2", "SELL", 200, "12.50", "sell-fill"),
    ):
        state = apply_event(state, event(fact, state.last_seq + 1, now=next_day))
    main = summary(state)
    assert main.execution_id == IDENTITY.execution_id and main.stage == "CLOSED"
    assert (
        main.sold_quantity == 200 and main.sell_amount == "2500.00" and main.remaining_quantity == 0
    )
    assert state.orders["sell-1"].status == "REJECTED"
    assert main.available_quantity is None  # No broker availability was queried for this execution.


def test_order_confirmation_is_not_a_fill_and_late_updates_do_not_regress() -> None:
    state = fold([candidate(), intent(), order()])
    assert summary(state).stage == "REVIEW" and summary(state).bought_quantity == 0
    state = apply_event(state, event(trade(), 4))
    state = apply_event(state, event(order(status="ACTIVE", filled=0), 5))
    assert summary(state).bought_quantity == 100 and state.orders["buy-1"].status == "FILLED"
    state = apply_event(state, event(trade(), 6))
    assert summary(state).bought_quantity == 100  # A duplicate broker fill cannot double holdings.


@pytest.mark.parametrize(
    "facts",
    [
        [order()],
        [trade()],
        [intent(), order(quantity=200)],
        [intent(), trade(quantity=101)],
        [intent(), trade(price="12.00")],
        [intent(side="SELL"), trade(side="SELL", price="12.00")],
        [intent(), order(), trade(), trade(price="11.00")],
        [intent(), order(), order(broker_order_id="changed")],
        [intent(), intent(quantity=200)],
        [
            {
                "kind": "CANCEL_ERROR",
                "client_order_id": "missing",
                "broker_order_id": "x",
                "side": "BUY",
                "reason_code": "REJECTED",
            }
        ],
    ],
)
def test_conflicting_or_unowned_facts_are_rejected(facts: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        fold(facts)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("UNCERTAIN", "UNKNOWN"),
        ("PERSISTENCE_UNCERTAIN", "UNKNOWN"),
        ("RECOVERED", "UNKNOWN"),
        ("RECOVERY_REQUIRED", "UNKNOWN"),
        ("BEGIN_FAILED", "FAILED"),
        ("NOT_DISPATCHED", "FAILED"),
        ("PERSISTENCE_FAILED", "FAILED"),
        ("EVIDENCE_MISMATCH", "FAILED"),
        ("PERSISTING", "ORDERING"),
        ("DISPATCHING", "ORDERING"),
        ("SDK_ACCEPTED", "ORDERING"),
    ],
)
def test_submission_statuses_preserve_uncertainty(status: str, expected: str) -> None:
    assert summary(fold([candidate(), intent(status=status)])).buy_status == expected


@pytest.mark.parametrize(
    "status",
    [
        "MISSED_ENTRY_WINDOW",
        "BOARD_NOT_ALLOWED",
        "SKIPPED_MIN_ORDER",
        "POSITION_CAPACITY",
        "WINDOW_CLOSED",
        "CHILD_LIMIT",
    ],
)
def test_not_bought_reasons_are_recorded_without_failed_orders(status: str) -> None:
    state = fold(
        [
            candidate(),
            {
                "kind": "PROGRESS",
                "plan_hash": "sha256:" + "b" * 64,
                "status": status,
                "target_quantity": 0,
            },
        ]
    )
    assert summary(state).buy_status == "SKIPPED" and summary(state).stage == "NOT_BOUGHT"


def test_partial_cancel_and_cancel_failure_then_authoritative_resolution() -> None:
    state = fold(
        [
            candidate(),
            intent(),
            order(status="PARTIAL", filled=50),
            trade(quantity=50),
            {
                "kind": "CANCEL_ERROR",
                "client_order_id": "buy-1",
                "broker_order_id": "broker-buy-1",
                "side": "BUY",
                "reason_code": "CANCEL_FAILED",
            },
        ]
    )
    assert summary(state).stage == "REVIEW"
    state = apply_event(state, event(order(status="CANCELLED", filled=50), 6))
    assert summary(state).stage == "HOLDING" and summary(state).bought_quantity == 50
    empty = fold([intent(), order(status="CANCELLED", filled=0)])
    assert summary(empty).buy_status == "CANCELLED"


def make_store(path: Path) -> ExecutionStore:
    return ExecutionStore(path, scope="test-scope", node_id="test-node")


def test_outbox_restart_lost_ack_duplicate_and_exact_receipt(tmp_path: Path) -> None:
    path = tmp_path / "reports.json"
    store = make_store(path)
    first = event(candidate(), 1)
    draft = Draft.model_validate(first.model_dump(exclude={"seq"}))
    store.append(draft)
    store.bind(order_id="order-1", identity=IDENTITY, lot_id="lot-1")
    store = make_store(path)
    batch = store.batch()
    assert batch is not None and batch.events == (first,)
    assert store.state.lots["lot-1"] == IDENTITY
    store.append(draft.model_copy(update={"occurred_at": NOW + timedelta(seconds=1)}))
    assert store.batch() == batch
    bad = Receipt(
        schema_version=1,
        acknowledgments=(Acknowledgment(event_id=first.event_id, event_hash="sha256:" + "0" * 64),),
    )
    with pytest.raises(ValueError):
        store.acknowledge(batch, bad)
    assert make_store(path).batch() == batch
    store.acknowledge(batch, receipt(batch))
    assert make_store(path).batch() is None
    store.append(draft)
    assert store.batch() is None
    store.append(Draft.model_validate(event(intent(), 2).model_dump(exclude={"seq"})))
    assert store.batch().events[0].seq == 2  # type: ignore[union-attr]


def test_outbox_corruption_conflict_scope_and_atomic_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "reports.json"
    store = make_store(path)
    draft = Draft.model_validate(event(candidate(), 1).model_dump(exclude={"seq"}))
    store.append(draft)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        ExecutionStore(path, scope="another", node_id="test-node")
    changed = draft.model_copy(
        update={"fact": Candidate.model_validate({**candidate(), "name": "changed"})}
    )
    with pytest.raises(ValueError):
        store.append(changed)
    monkeypatch.setattr(
        "auto_trading_system.sync.execution_store.os.replace",
        lambda *a: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        store.append(Draft.model_validate(event(intent(), 2).model_dump(exclude={"seq"})))
    assert path.read_bytes() == original and len(store.state.pending) == 1
    path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        make_store(path)


def upload_config(root: Path) -> NodeProcessConfig:
    value = node_config(root).model_dump(mode="json")
    value["execution_reporting"] = {
        "token": {"source": "ENVIRONMENT", "variable": "ATS_SECRET_TEST_REPORT_TOKEN"}
    }
    value["quant_secret"] = {"source": "ENVIRONMENT", "variable": "ATS_SECRET_TEST_REPORT_QUANT"}
    value["ipc_secrets"] = {
        role: {"source": "ENVIRONMENT", "variable": "ATS_SECRET_TEST_REPORT_" + role.upper()}
        for role in ("agent", "operator", "jobrunner")
    }
    return NodeProcessConfig.model_validate_json(json.dumps(value))


class ReportPeer:
    def __init__(self, config: NodeProcessConfig, store: ExecutionStore) -> None:
        self.config, self.store = config, store
        self.files = ArtifactFiles(config.data_root / "artifacts")
        self.lose_ack = False

    def invoke(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if action == "GET_EXECUTION_EVENTS":
            batch = self.store.batch()
            return (
                {
                    "status": "READY",
                    "artifact_hash": self.files.put(batch.model_dump_json().encode()),
                }
                if batch
                else {"status": "EMPTY"}
            )
        assert action == "ACK_EXECUTION_EVENTS" and payload is not None
        batch = Batch.model_validate_json(self.files.read(payload["batch_hash"]))
        answer = Receipt.model_validate_json(self.files.read(payload["receipt_hash"]))
        self.store.acknowledge(batch, answer)
        if self.lose_ack:
            self.lose_ack = False
            from auto_trading_system.control.ipc_protocol import IPCError

            raise IPCError("lost local ack")
        return {"status": "ACKNOWLEDGED"}


def test_uploader_does_not_poll_selection_and_survives_uncertain_ack(tmp_path: Path) -> None:
    config = upload_config(tmp_path)
    store = make_store(tmp_path / "outbox.json")
    identity = IDENTITY.model_copy(update={"account_id": config.account_id, "environment": "REAL"})
    store.append(
        Draft.model_validate(event(candidate(), 1, identity=identity).model_dump(exclude={"seq"}))
    )
    # The store's node id must match the configured upload identity.
    store.node_id = config.node_id
    peer = ReportPeer(config, store)
    peer.lose_ack = True
    sent = []

    def send(body: bytes) -> bytes:
        batch = Batch.model_validate_json(body)
        sent.append(batch)
        return receipt(batch).model_dump_json().encode()

    uploader = ExecutionUploader(config, cast(RPCClient, peer), send=send)
    from auto_trading_system.control.ipc_protocol import IPCError

    with pytest.raises(IPCError):
        uploader.run()
    again = ExecutionUploader(config, cast(RPCClient, peer), send=send)
    assert again.run()["status"] == "SYNCED" and len(sent) == 1


@pytest.mark.parametrize(
    "http_status,retryable",
    [(401, False), (403, False), (409, False), (429, True), (503, True), (500, True), (302, False)],
)
def test_upload_transport_never_accepts_failed_http(http_status: int, retryable: bool) -> None:
    batch = Batch(
        schema_version=1,
        node_id="test-node",
        account_id=IDENTITY.account_id,
        strategy_id=IDENTITY.strategy_id,
        environment="TEST",
        events=(event(candidate(), 1),),
    )
    client = ExecutionHttp(
        "https://quant.test",
        b"x" * 43,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(http_status, json={"error": {"code": "BAD"}})
        ),
    )
    try:
        with pytest.raises(ExecutionUploadError) as caught:
            client.send(batch.model_dump_json().encode())
        assert caught.value.retryable is retryable
    finally:
        client.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_upload_transport_validates_exact_receipt_and_https(scheme: str) -> None:
    batch = Batch(
        schema_version=1,
        node_id="test-node",
        account_id=IDENTITY.account_id,
        strategy_id=IDENTITY.strategy_id,
        environment="TEST",
        events=(event(candidate(), 1),),
    )

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path.endswith("execution-events")
        return httpx.Response(
            200, content=receipt(Batch.model_validate_json(request.content)).model_dump_json()
        )

    client = ExecutionHttp(
        f"{scheme}://quant.test:8001", b"x" * 43, transport=httpx.MockTransport(answer)
    )
    try:
        assert Receipt.model_validate_json(
            client.send(batch.model_dump_json().encode())
        ) == receipt(batch)
    finally:
        client.close()
    with pytest.raises(ValueError):
        ExecutionHttp("ftp://quant.test", b"x" * 43)


def reporter_config(root: Path, account: str = "test-account") -> NodeProcessConfig:
    value = json.loads(Path("config/v2/node.mock.example.json").read_bytes())
    value.update(data_root=str(root), account_id=account, strategy_id="TAIL_CLOSE")
    (root / "jobs").mkdir(parents=True, exist_ok=True)
    return NodeProcessConfig.model_validate_json(json.dumps(value))


def drain_reporter(reporter: Any) -> list[Event]:
    output: list[Event] = []
    for _ in range(100):
        batch = reporter.batch()
        if batch is None:
            return output
        output.extend(batch.events)
        reporter.acknowledge(batch, receipt(batch))
    raise AssertionError("reporter did not drain")


def test_real_engine_broker_fill_and_restart_reporting_does_not_reorder_or_resubmit(
    tmp_path: Path,
) -> None:
    from auto_trading_system.apps.trade_runtime.engine_simulation import (
        open_offline_engine,
        wait_for,
    )
    from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario, settle
    from auto_trading_system.runtime.execution_reporting import ExecutionReporter

    report_root = tmp_path / "report-node"
    with open_offline_engine(tmp_path / "engine") as runtime:
        engine = runtime.kernel
        config = reporter_config(report_root, engine.account_id)
        reporter = ExecutionReporter(config, engine.emergency_halt)
        engine.execution_reporter = reporter
        try:
            engine.start_recovery()
            wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
            runtime.confirmed_arm()
            original = runtime.buy()
            runtime.broker.queue_submit(fill_scenario(100, full=True))
            settle(runtime, original)
            events = drain_reporter(reporter)
            assert any(e.fact.kind == "SUBMISSION" for e in events)
            assert len([e for e in events if e.fact.kind == "TRADE"]) == 1
            state = State(identity=events[0].identity)
            for item in events:
                state = apply_event(state, item)
            assert summary(state).bought_quantity == 100 and summary(state).buy_amount == "1000.00"
            reporter.query_facts(
                engine._order_state.orders(), engine._order_state.trades(), engine._known
            )
            assert drain_reporter(reporter) == []
        finally:
            engine.execution_reporter = None
            reporter.close()
    restored = ExecutionReporter(config, lambda _: None)
    try:
        assert restored.batch() is None
        assert original.client_order_id in restored.store.state.orders
    finally:
        restored.close()


def test_board_filtered_candidates_keep_names_and_report_terminal_reason(tmp_path: Path) -> None:
    from test_tail_feed import NOW as FEED_NOW
    from test_tail_feed import local_feed, wire

    from auto_trading_system.runtime.execution_reporting import ExecutionReporter

    feed = local_feed(tmp_path / "feed")
    feed.settings = feed.settings.model_copy(
        update={"buy": feed.settings.buy.model_copy(update={"allowed_boards": ("MAIN",)})}
    )
    value = wire()
    value["batches"][0]["stocks"] = [
        {"code": "301413.SZ", "name": "安培龙", "rank": 1},
        {"code": "000001.SZ", "name": "平安银行", "rank": 2},
    ]
    feed.accept(json.dumps(value).encode(), FEED_NOW)
    assert feed.snapshot is not None
    reporter = ExecutionReporter(reporter_config(tmp_path, feed.config.account_id), lambda _: None)
    try:
        for _ in range(2):
            reporter.tail_candidates(
                feed.snapshot, "1000.00", NOW, filtered_plans=feed.plans.plans.accepted()
            )
        events = drain_reporter(reporter)
        excluded = [e for e in events if e.identity.symbol == "301413.SZ"]
        assert [e.fact.kind for e in excluded] == ["CANDIDATE", "PROGRESS"]
        state = State(identity=excluded[0].identity)
        for e in excluded:
            state = apply_event(state, e)
        result = summary(state)
        assert (result.buy_status, result.stage, result.reason_code) == (
            "SKIPPED",
            "NOT_BOUGHT",
            "BOARD_NOT_ALLOWED",
        )
        assert result.name == "安培龙" and result.bought_quantity == 0
        assert [e.fact.kind for e in events if e.identity.symbol == "000001.SZ"] == ["CANDIDATE"]
        # An existing strategy order must not acquire a later exclusion event.
        for stream in reporter.store.state.streams.values():
            if stream.identity.symbol == "301413.SZ":
                reporter.request(
                    lambda identity=stream.identity: reporter.store.bind(
                        order_id="existing", identity=identity
                    )
                ).result()
                break
        reporter._seen.clear()
        reporter.tail_candidates(
            feed.snapshot, "1000.00", NOW, filtered_plans=feed.plans.plans.accepted()
        )
        assert drain_reporter(reporter) == []
    finally:
        reporter.close()


def test_reporter_retains_candidate_names_progress_cancel_error_and_cross_day_sell_binding(
    tmp_path: Path, submission: Any
) -> None:
    from test_tail_feed import wire

    from auto_trading_system.domain import ShanghaiTimestamp
    from auto_trading_system.gateways.broker.models import BrokerCancelError
    from auto_trading_system.journal.critical_models import SellAllocation, SellBinding
    from auto_trading_system.protocol.tail_picks import TailPicks
    from auto_trading_system.reconcile.position_lots import lot_id
    from auto_trading_system.runtime.execution_reporting import ExecutionReporter

    config = reporter_config(tmp_path, submission.account_id)
    reporter = ExecutionReporter(config, lambda _: None)
    value = wire()
    value["batches"][0]["stocks"] = [{"code": submission.symbol, "name": "平安银行", "rank": 1}]
    feed = TailPicks.model_validate_json(json.dumps(value))
    # Use same-day fixture identity; no real broker is created by this test.
    item = submission.model_copy(update={"trade_date": NOW.date()})
    try:
        reporter.tail_candidates(feed, "1000.00", NOW)
        reporter.tail_candidates(feed, "1000.00", NOW)
        reporter.submission(item, "SDK_ACCEPTED", NOW)
        reporter.broker(
            BrokerCancelError(
                item.client_order_id,
                "broker-1",
                "CANCEL_FAILED",
                "private-sdk-text",
                ShanghaiTimestamp(NOW),
            ),
            item,
        )
        events = drain_reporter(reporter)
        assert len([e for e in events if e.fact.kind == "CANDIDATE"]) == 1
        assert any(e.fact.kind == "CANCEL_ERROR" for e in events)
        assert all("private-sdk-text" not in e.model_dump_json() for e in events)
        reporter.close()
        reporter = ExecutionReporter(config, lambda _: None)
        sale = item.model_copy(
            update={
                "client_order_id": "ord_sale000001",
                "trade_date": NOW.date() + timedelta(days=3),
                "side": "SELL",
                "entry_binding": None,
                "sell_binding": SellBinding(
                    strategy_id=config.strategy_id,
                    opening_hash="sha256:" + "a" * 64,
                    allocations=(SellAllocation(lot_id=lot_id(item), quantity=item.quantity),),
                ),
            }
        )
        reporter.submission(sale, "SDK_ACCEPTED", NOW + timedelta(days=3))
        sold_events = drain_reporter(reporter)
        assert len(sold_events) == 1 and sold_events[0].identity.selection_date == NOW.date()
        assert sold_events[0].identity.execution_id == events[0].identity.execution_id
        assert sold_events[0].occurred_at.date() != sold_events[0].identity.selection_date
    finally:
        reporter.close()


def test_reporter_storage_failure_blocks_new_orders_and_never_acknowledges_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading_system.runtime.execution_reporting import ExecutionReporter

    halts: list[str] = []
    config = reporter_config(tmp_path)
    reporter = ExecutionReporter(config, halts.append)
    try:

        def fail(*args: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(reporter.store, "append", fail)
        draft = Draft.model_validate(event(candidate(), 1).model_dump(exclude={"seq"}))
        future = reporter.request(lambda: reporter.store.append(draft))
        with pytest.raises(OSError):
            future.result(timeout=3)
        assert reporter.failed and halts
        with pytest.raises(ValueError):
            reporter.batch()
    finally:
        reporter.close()


@pytest.mark.parametrize(
    "case", ["temporary", "conflict", "bad_receipt", "wrong_scope", "engine_bad_status"]
)
def test_uploader_keeps_pending_on_errors(tmp_path: Path, case: str) -> None:
    config = upload_config(tmp_path)
    store = ExecutionStore(tmp_path / "outbox.json", scope="test", node_id=config.node_id)
    identity = IDENTITY.model_copy(update={"account_id": config.account_id, "environment": "REAL"})
    store.append(
        Draft.model_validate(event(candidate(), 1, identity=identity).model_dump(exclude={"seq"}))
    )
    peer = ReportPeer(config, store)

    def send(body: bytes) -> bytes:
        if case in ("temporary", "conflict"):
            raise ExecutionUploadError(retryable=case == "temporary", delay=5)
        return b"{}"

    if case == "wrong_scope":
        store.node_id = "wrong"
    if case == "engine_bad_status":
        peer.invoke = lambda *a, **k: {"status": "UNKNOWN"}  # type: ignore[method-assign]
    seconds = [0.0]
    upload = ExecutionUploader(config, cast(RPCClient, peer), send=send, seconds=lambda: seconds[0])
    assert upload.run()["status"] == ("PENDING" if case == "temporary" else "NEEDS_ATTENTION")
    assert store.state.pending
    assert upload.run()["status"] == "SKIPPED"


def test_reporter_signed_candidates_progress_and_duplicate_phase_does_not_replace_owner(
    tmp_path: Path,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_tail_phase_plans import NOW as PLAN_NOW
    from test_tail_phase_plans import phase_plan

    from auto_trading_system.runtime.execution_reporting import ExecutionReporter

    plan = phase_plan("1440", Ed25519PrivateKey.generate())
    config = reporter_config(tmp_path, plan.payload.account_id)
    reporter = ExecutionReporter(config, lambda _: None)
    try:
        reporter.candidates((plan,), {"000001.SZ": "平安银行"}, PLAN_NOW)
        reporter.candidates((plan,), {}, PLAN_NOW)
        reporter.progress(plan, (), (("000001.SZ", "WAITING_MARKET"),), PLAN_NOW)
        reporter.progress(plan, (), (("000001.SZ", "WAITING_MARKET"),), PLAN_NOW)
        events = drain_reporter(reporter)
        assert len(events) == 2 and events[0].fact.kind == "CANDIDATE"
        state = State(identity=events[0].identity)
        for e in events:
            state = apply_event(state, e)
        assert summary(state).name == "平安银行"
        good = Progress(
            kind="PROGRESS",
            plan_hash=plan.envelope.payload_hash,
            status="ORDER_ACTIVE",
            target_quantity=100,
        )
        state = apply_event(
            state, event(good.model_dump(), 3, now=PLAN_NOW, identity=state.identity)
        )
        ignored = Progress(
            kind="PROGRESS",
            plan_hash="sha256:" + "c" * 64,
            status="POSITION_CAPACITY",
            target_quantity=0,
        )
        state = apply_event(
            state, event(ignored.model_dump(), 4, now=PLAN_NOW, identity=state.identity)
        )
        assert summary(state).target_quantity == 100
    finally:
        reporter.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "CANCEL"},
        {"price": "0"},
        {"quantity": 0},
    ],
)
def test_malformed_submission_facts_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SubmissionFact.model_validate({**intent(), **bad})


def test_model_date_amount_scope_and_duplicate_batch_checks() -> None:
    with pytest.raises(ValueError):
        Receipt.model_validate_json('{"schema_version":true,"acknowledgments":[]}')
    with pytest.raises(ValueError):
        OrderFact.model_validate(order(status="FILLED", filled=50))
    with pytest.raises(ValueError):
        TradeFact.model_validate({**trade(), "amount": "999.00"})
    with pytest.raises(ValueError):
        event(candidate(), 1, now=NOW - timedelta(days=1))
    with pytest.raises(ValueError):
        event(candidate(), 1, now=NOW + timedelta(days=1))
    with pytest.raises(ValueError):
        Batch(
            schema_version=1,
            node_id="test-node",
            account_id=IDENTITY.account_id,
            strategy_id=IDENTITY.strategy_id,
            environment="TEST",
            events=(event(candidate(), 1), event(candidate(), 1)),
        )
    with pytest.raises(ValueError):
        Batch(
            schema_version=1,
            node_id="test-node",
            account_id="another",
            strategy_id=IDENTITY.strategy_id,
            environment="TEST",
            events=(event(candidate(), 1),),
        )


def test_reporting_pump_continues_after_selection_has_stopped(tmp_path: Path) -> None:
    from threading import Event as Signal

    from auto_trading_system.sync.node_agent import AgentEndpoint, AgentWorker

    config = upload_config(tmp_path)
    store = ExecutionStore(tmp_path / "outbox.json", scope="test", node_id=config.node_id)
    identity = IDENTITY.model_copy(update={"account_id": config.account_id, "environment": "REAL"})
    store.append(
        Draft.model_validate(event(candidate(), 1, identity=identity).model_dump(exclude={"seq"}))
    )
    peer = ReportPeer(config, store)
    worker = AgentWorker(config, cast(RPCClient, peer), clock=lambda: NOW)
    assert worker.tail_feed is not None and worker.execution_upload is not None
    worker.tail_feed.stopped_day = NOW.date().isoformat()
    started, release = Signal(), Signal()
    sends = []

    def send(body: bytes) -> bytes:
        sends.append(body)
        started.set()
        assert release.wait(3)
        return receipt(Batch.model_validate_json(body)).model_dump_json().encode()

    worker.execution_upload.send = send
    endpoint = AgentEndpoint(worker, Signal(), "test-boot")
    try:
        endpoint.pump_tail_feed()
        assert endpoint._active is None
        endpoint.pump_execution_reports()
        assert started.wait(3)
        for _ in range(10):
            endpoint.pump_execution_reports()
        assert len(sends) == 1
        release.set()
        assert endpoint._report_active is not None
        assert endpoint._report_active.result(timeout=3)["status"] == "UPLOADED"
        endpoint.pump_execution_reports()
        assert endpoint._report_last["status"] == "UPLOADED"
    finally:
        release.set()
        endpoint.close()


def test_real_credential_resolution_upload_and_forbidden_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading_system.config.models import EnvironmentSecretReference
    from auto_trading_system.config.node_v2 import resolve_secret
    from auto_trading_system.sync import execution_upload as module

    config = upload_config(tmp_path)
    store = ExecutionStore(tmp_path / "outbox.json", scope="test", node_id=config.node_id)
    identity = IDENTITY.model_copy(update={"account_id": config.account_id, "environment": "REAL"})
    store.append(
        Draft.model_validate(event(candidate(), 1, identity=identity).model_dump(exclude={"seq"}))
    )
    peer = ReportPeer(config, store)
    closed = []

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def send(self, body: bytes) -> bytes:
            return receipt(Batch.model_validate_json(body)).model_dump_json().encode()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(module, "ExecutionHttp", Client)
    monkeypatch.setenv("ATS_SECRET_TEST_REPORT_TOKEN", "r" * 43)
    assert config.tail_picks is not None
    for ref in (
        config.quant_secret,
        config.ipc_secrets.agent,
        config.ipc_secrets.jobrunner,
        config.ipc_secrets.operator,
        config.tail_picks.token,
    ):
        if isinstance(ref, EnvironmentSecretReference):
            monkeypatch.setenv(ref.variable, ref.variable + "x" * 43)
    assert ExecutionUploader(config, cast(RPCClient, peer)).run()["status"] == "UPLOADED"
    assert closed == [True]
    store.append(
        Draft.model_validate(event(intent(), 2, identity=identity).model_dump(exclude={"seq"}))
    )
    monkeypatch.setenv("ATS_SECRET_TEST_REPORT_TOKEN", resolve_secret(config.quant_secret).decode())
    assert ExecutionUploader(config, cast(RPCClient, peer)).run()["status"] == "NEEDS_ATTENTION"
    assert store.state.pending


@pytest.mark.parametrize(
    "case", ["bad_ack", "too_large", "timeout", "connection", "sequence_gap", "malformed_error"]
)
def test_upload_transport_response_failures(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_trading_system.sync import execution_upload as module

    batch = Batch(
        schema_version=1,
        node_id="test-node",
        account_id=IDENTITY.account_id,
        strategy_id=IDENTITY.strategy_id,
        environment="TEST",
        events=(event(candidate(), 1),),
    )

    def answer(request: httpx.Request) -> httpx.Response:
        if case == "connection":
            raise httpx.ConnectError("offline")
        if case == "sequence_gap":
            return httpx.Response(409, json={"error": {"code": "EVENT_SEQUENCE_GAP"}})
        if case == "malformed_error":
            return httpx.Response(409, content="bad")
        if case == "too_large":
            return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(
            200,
            content=receipt(batch)
            .model_copy(
                update={
                    "acknowledgments": (
                        Acknowledgment(
                            event_id=batch.events[0].event_id, event_hash="sha256:" + "0" * 64
                        ),
                    )
                }
            )
            .model_dump_json(),
        )

    if case == "timeout":
        stamps = iter([0.0, 9.0])
        monkeypatch.setattr(module, "monotonic", lambda: next(stamps))
    client = ExecutionHttp("https://quant.test", b"x" * 43, transport=httpx.MockTransport(answer))
    try:
        with pytest.raises(ExecutionUploadError) as caught:
            client.send(batch.model_dump_json().encode())
        assert caught.value.retryable == (case in ("connection", "sequence_gap", "timeout"))
    finally:
        client.close()


def test_request_rejection_and_retry_and_late_submission_do_not_regress_master() -> None:
    state = fold(
        [
            candidate(),
            intent(),
            {
                "kind": "REQUEST_ERROR",
                "client_order_id": "buy-1",
                "action": "NEW",
                "side": "BUY",
                "reason_code": "QMT_ORDER_ERROR_1001",
            },
        ]
    )
    assert summary(state).buy_status == "FAILED" and summary(state).stage == "NOT_BOUGHT"
    state = apply_event(state, event(intent(status="PERSISTING"), 4))
    assert state.submissions["buy-1"].status == "SDK_ACCEPTED"
    assert summary(state).buy_status == "FAILED"
    state = apply_event(state, event(order(status="REJECTED", filled=0), 5))
    assert summary(state).buy_status == "FAILED"
    with pytest.raises(ValueError):
        apply_event(
            state,
            event(
                {
                    "kind": "REQUEST_ERROR",
                    "client_order_id": "missing",
                    "action": "NEW",
                    "side": "BUY",
                    "reason_code": "REJECTED",
                },
                6,
            ),
        )
    state = fold(
        [
            intent(),
            order(status="ACTIVE", filled=0),
            intent("cancel-1", action="CANCEL", original_client_order_id="buy-1"),
            {
                "kind": "REQUEST_ERROR",
                "client_order_id": "cancel-1",
                "action": "CANCEL",
                "side": "BUY",
                "reason_code": "QMT_CANCEL_ERROR_1001",
            },
        ]
    )
    assert summary(state).stage == "REVIEW"
    state = apply_event(state, event(order(status="CANCELLED", filled=0), 5))
    assert summary(state).buy_status == "CANCELLED"


def test_request_error_reaches_engine_audit_and_reporting_without_raw_sdk_text(
    tmp_path: Path,
) -> None:
    from auto_trading_system.apps.trade_runtime.engine_simulation import (
        open_offline_engine,
        wait_for,
    )
    from auto_trading_system.domain import ShanghaiTimestamp
    from auto_trading_system.gateways.broker.models import BrokerRequestError
    from auto_trading_system.runtime.execution_reporting import ExecutionReporter

    with open_offline_engine(tmp_path / "engine") as runtime:
        engine = runtime.kernel
        config = reporter_config(tmp_path / "reports", engine.account_id)
        reporter = ExecutionReporter(config, engine.emergency_halt)
        engine.execution_reporter = reporter
        try:
            engine.start_recovery()
            wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
            item = runtime.buy()
            # A retained, known intent is required before associating an error with a stock.
            engine._known[item.client_order_id] = item
            reporter.submission(item, "SDK_ACCEPTED", runtime.clock())
            engine.broker_event(
                BrokerRequestError(
                    item.client_order_id,
                    "NEW",
                    "QMT_ORDER_ERROR_1001",
                    ShanghaiTimestamp(runtime.clock()),
                )
            )
            engine.poll()
            assert engine.snapshot().halted
            events = drain_reporter(reporter)
            assert any(e.fact.kind == "REQUEST_ERROR" for e in events)
            state = State(identity=events[0].identity)
            for e in events:
                state = apply_event(state, e)
            assert summary(state).buy_status == "FAILED"
        finally:
            engine.execution_reporter = None
            reporter.close()
