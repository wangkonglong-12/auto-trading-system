from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Thread
from typing import cast

import pytest

from auto_trading_system.runtime.decision_loop import (
    BoundedEventIngress,
    DecisionEventLoop,
    DecisionLoopDispatchError,
    DecisionLoopError,
    DecisionLoopStatus,
    DispatchKind,
    EventPriority,
    PublishOutcome,
    QueueOverflowReport,
    RuntimeDispatch,
    RuntimeEventEnvelope,
    RuntimeEventSource,
)


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


def make_ingress(
    *, normal_capacity: int = 4, critical_capacity: int = 2
) -> tuple[BoundedEventIngress[str], IncrementingClock]:
    clock = IncrementingClock()
    return (
        BoundedEventIngress(
            normal_capacity=normal_capacity,
            critical_capacity=critical_capacity,
            monotonic_clock=clock,
        ),
        clock,
    )


@pytest.mark.parametrize(
    ("normal_capacity", "critical_capacity", "message"),
    [
        (0, 1, "normal_capacity"),
        (True, 1, "normal_capacity"),
        (1, 0, "critical_capacity"),
        (1, False, "critical_capacity"),
    ],
)
def test_ingress_requires_positive_integer_capacities(
    normal_capacity: int, critical_capacity: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        BoundedEventIngress[str](
            normal_capacity=normal_capacity,
            critical_capacity=critical_capacity,
            monotonic_clock=IncrementingClock(),
        )


def test_publish_rejects_untyped_source_and_priority() -> None:
    ingress, _ = make_ingress()

    with pytest.raises(ValueError, match="RuntimeEventSource"):
        ingress.publish("event", source=cast(RuntimeEventSource, "BROKER"))
    with pytest.raises(ValueError, match="EventPriority"):
        ingress.publish(
            "event",
            source=RuntimeEventSource.BROKER,
            priority=cast(EventPriority, "CRITICAL"),
        )


def test_critical_lane_is_reserved_and_prioritized_with_fifo_per_lane() -> None:
    ingress, _ = make_ingress(normal_capacity=2, critical_capacity=2)

    assert ingress.publish("normal-1", source=RuntimeEventSource.MARKET) is PublishOutcome.ACCEPTED
    assert ingress.publish("normal-2", source=RuntimeEventSource.SYNC) is PublishOutcome.ACCEPTED
    assert (
        ingress.publish(
            "critical-1",
            source=RuntimeEventSource.BROKER,
            priority=EventPriority.CRITICAL,
        )
        is PublishOutcome.ACCEPTED
    )
    assert (
        ingress.publish(
            "critical-2",
            source=RuntimeEventSource.WATCHDOG,
            priority=EventPriority.CRITICAL,
        )
        is PublishOutcome.ACCEPTED
    )
    depth = ingress.depth()
    assert (depth.normal, depth.critical, depth.total, depth.closed) == (2, 2, 4, False)

    ingress.close()
    drained = [ingress.take_blocking() for _ in range(4)]

    assert [item.payload for item in drained if item is not None] == [
        "critical-1",
        "critical-2",
        "normal-1",
        "normal-2",
    ]
    assert ingress.take_blocking() is None


def test_overflow_latch_summarizes_drops_and_resets_atomically() -> None:
    ingress, _ = make_ingress(normal_capacity=1, critical_capacity=1)
    assert ingress.publish("normal", source=RuntimeEventSource.MARKET) is PublishOutcome.ACCEPTED
    assert ingress.publish("drop-1", source=RuntimeEventSource.MARKET) is PublishOutcome.OVERFLOW
    assert ingress.publish("drop-2", source=RuntimeEventSource.BROKER) is PublishOutcome.OVERFLOW
    assert (
        ingress.publish(
            "critical",
            source=RuntimeEventSource.WATCHDOG,
            priority=EventPriority.CRITICAL,
        )
        is PublishOutcome.ACCEPTED
    )
    assert (
        ingress.publish(
            "drop-critical",
            source=RuntimeEventSource.WATCHDOG,
            priority=EventPriority.CRITICAL,
        )
        is PublishOutcome.OVERFLOW
    )

    report = ingress.consume_overflow()

    assert report == QueueOverflowReport(
        dropped_total=3,
        dropped_normal=2,
        dropped_critical=1,
        sources=(
            RuntimeEventSource.BROKER,
            RuntimeEventSource.MARKET,
            RuntimeEventSource.WATCHDOG,
        ),
        first_monotonic_ns=2,
        last_monotonic_ns=5,
    )
    assert ingress.consume_overflow() is None


def test_closed_ingress_rejects_publish_without_reading_clock() -> None:
    ingress, clock = make_ingress()
    assert ingress.close() == 0

    assert ingress.publish("late", source=RuntimeEventSource.INTERNAL) is PublishOutcome.CLOSED
    assert clock.value == 0
    assert ingress.depth().closed is True


def test_close_can_drain_or_explicitly_discard_pending_events() -> None:
    drain_ingress, _ = make_ingress()
    drain_ingress.publish("keep", source=RuntimeEventSource.INTERNAL)
    assert drain_ingress.close() == 0
    kept = drain_ingress.take_blocking()
    assert kept is not None and kept.payload == "keep"
    assert drain_ingress.take_blocking() is None

    discard_ingress, _ = make_ingress()
    discard_ingress.publish("drop", source=RuntimeEventSource.INTERNAL)
    assert discard_ingress.close(discard_pending=True) == 1
    assert discard_ingress.take_blocking() is None


def test_blocking_consumer_is_woken_by_publish() -> None:
    ingress, _ = make_ingress()
    consumed: list[RuntimeEventEnvelope[str] | None] = []
    consumer = Thread(target=lambda: consumed.append(ingress.take_blocking()), daemon=True)
    consumer.start()

    assert ingress.publish("wake", source=RuntimeEventSource.SCHEDULER) is PublishOutcome.ACCEPTED
    consumer.join(timeout=2)

    assert not consumer.is_alive()
    assert consumed[0] is not None and consumed[0].payload == "wake"


def test_parallel_publishers_receive_unique_ordered_sequences() -> None:
    ingress, _ = make_ingress(normal_capacity=200, critical_capacity=1)

    def publish(index: int) -> PublishOutcome:
        return ingress.publish(f"event-{index}", source=RuntimeEventSource.MARKET)

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(publish, range(200)))
    ingress.close()
    drained = [ingress.take_blocking() for _ in range(200)]
    envelopes = [item for item in drained if item is not None]

    assert set(outcomes) == {PublishOutcome.ACCEPTED}
    assert [item.ingress_sequence for item in envelopes] == list(range(1, 201))
    assert [item.published_monotonic_ns for item in envelopes] == list(range(1, 201))


def test_runtime_dispatch_enforces_discriminated_payload() -> None:
    envelope = RuntimeEventEnvelope(
        ingress_sequence=1,
        source=RuntimeEventSource.INTERNAL,
        priority=EventPriority.NORMAL,
        published_monotonic_ns=1,
        payload="event",
    )
    overflow = QueueOverflowReport(1, 1, 0, (RuntimeEventSource.INTERNAL,), 1, 1)

    with pytest.raises(ValueError, match="positive"):
        RuntimeDispatch(dispatch_sequence=0, kind=DispatchKind.EVENT, envelope=envelope)
    with pytest.raises(ValueError, match="only an envelope"):
        RuntimeDispatch(dispatch_sequence=1, kind=DispatchKind.EVENT, overflow=overflow)
    with pytest.raises(ValueError, match="only an overflow"):
        RuntimeDispatch(dispatch_sequence=1, kind=DispatchKind.QUEUE_OVERFLOW, envelope=envelope)


def test_decision_loop_serializes_events_and_enforces_task_ownership() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress()
        handled: list[str] = []
        child_rejected: list[bool] = []
        active_handlers = 0
        maximum_active_handlers = 0
        loop: DecisionEventLoop[str]

        async def handler(dispatch: RuntimeDispatch[str]) -> None:
            nonlocal active_handlers, maximum_active_handlers
            loop.assert_owner()
            active_handlers += 1
            maximum_active_handlers = max(maximum_active_handlers, active_handlers)
            await asyncio.sleep(0)
            assert dispatch.envelope is not None
            handled.append(dispatch.envelope.payload)

            async def child_mutation() -> None:
                with pytest.raises(DecisionLoopError, match="DECISION_STATE_OWNER_REQUIRED"):
                    loop.assert_owner()
                child_rejected.append(True)

            await asyncio.create_task(child_mutation())
            active_handlers -= 1

        loop = DecisionEventLoop(ingress=ingress, handler=handler)
        ingress.publish("normal-1", source=RuntimeEventSource.MARKET)
        ingress.publish(
            "critical",
            source=RuntimeEventSource.BROKER,
            priority=EventPriority.CRITICAL,
        )
        ingress.publish("normal-2", source=RuntimeEventSource.SYNC)
        ingress.close()

        await loop.run()

        assert handled == ["critical", "normal-1", "normal-2"]
        assert child_rejected == [True, True, True]
        assert maximum_active_handlers == 1
        assert loop.dispatch_count == 3
        assert loop.status is DecisionLoopStatus.STOPPED
        with pytest.raises(DecisionLoopError, match="DECISION_STATE_OWNER_REQUIRED"):
            loop.assert_owner()

    asyncio.run(scenario())


def test_overflow_is_dispatched_before_business_event_for_safe_degradation() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress(normal_capacity=1, critical_capacity=1)
        safe_mode = False
        observations: list[tuple[DispatchKind, bool]] = []

        async def handler(dispatch: RuntimeDispatch[str]) -> None:
            nonlocal safe_mode
            if dispatch.kind is DispatchKind.QUEUE_OVERFLOW:
                safe_mode = True
                assert dispatch.overflow is not None
                assert dispatch.overflow.dropped_total == 2
            observations.append((dispatch.kind, safe_mode))

        loop = DecisionEventLoop(ingress=ingress, handler=handler)
        ingress.publish("queued", source=RuntimeEventSource.MARKET)
        ingress.publish("dropped", source=RuntimeEventSource.MARKET)
        ingress.publish(
            "critical-queued",
            source=RuntimeEventSource.BROKER,
            priority=EventPriority.CRITICAL,
        )
        ingress.publish(
            "critical-dropped",
            source=RuntimeEventSource.WATCHDOG,
            priority=EventPriority.CRITICAL,
        )
        ingress.close()

        await loop.run()

        assert observations == [
            (DispatchKind.QUEUE_OVERFLOW, True),
            (DispatchKind.EVENT, True),
            (DispatchKind.EVENT, True),
        ]

    asyncio.run(scenario())


def test_graceful_stop_drains_and_owner_remains_valid_while_stopping() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        handled: list[str] = []
        loop: DecisionEventLoop[str]

        async def handler(dispatch: RuntimeDispatch[str]) -> None:
            assert dispatch.envelope is not None
            if dispatch.envelope.payload == "first":
                first_started.set()
                await release_first.wait()
            loop.assert_owner()
            handled.append(dispatch.envelope.payload)

        loop = DecisionEventLoop(ingress=ingress, handler=handler)
        ingress.publish("first", source=RuntimeEventSource.INTERNAL)
        ingress.publish("second", source=RuntimeEventSource.INTERNAL)
        task = asyncio.create_task(loop.run())
        await first_started.wait()

        assert loop.request_stop() == 0
        assert loop.status is DecisionLoopStatus.STOPPING
        release_first.set()
        await task

        assert handled == ["first", "second"]
        assert loop.status.value == DecisionLoopStatus.STOPPED.value
        assert ingress.publish("late", source=RuntimeEventSource.INTERNAL) is PublishOutcome.CLOSED

    asyncio.run(scenario())


def test_emergency_stop_discards_pending_work() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        handled: list[str] = []

        async def handler(dispatch: RuntimeDispatch[str]) -> None:
            assert dispatch.envelope is not None
            first_started.set()
            await release_first.wait()
            handled.append(dispatch.envelope.payload)

        loop = DecisionEventLoop(ingress=ingress, handler=handler)
        ingress.publish("in-flight", source=RuntimeEventSource.INTERNAL)
        ingress.publish("pending", source=RuntimeEventSource.INTERNAL)
        task = asyncio.create_task(loop.run())
        await first_started.wait()

        assert loop.request_stop(discard_pending=True) == 1
        release_first.set()
        await task

        assert handled == ["in-flight"]
        assert loop.status is DecisionLoopStatus.STOPPED

    asyncio.run(scenario())


def test_stop_before_start_closes_loop_and_run_is_rejected() -> None:
    ingress, _ = make_ingress()
    ingress.publish("pending", source=RuntimeEventSource.INTERNAL)
    loop = DecisionEventLoop(ingress=ingress, handler=lambda _: asyncio.sleep(0))

    assert loop.request_stop(discard_pending=True) == 1
    assert loop.status is DecisionLoopStatus.STOPPED
    assert loop.request_stop() == 0
    with pytest.raises(DecisionLoopError, match="DECISION_LOOP_NOT_STARTABLE"):
        asyncio.run(loop.run())


def test_loop_cannot_be_started_twice() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress()
        loop = DecisionEventLoop(ingress=ingress, handler=lambda _: asyncio.sleep(0))
        task = asyncio.create_task(loop.run())
        while loop.status is DecisionLoopStatus.CREATED:
            await asyncio.sleep(0)

        with pytest.raises(DecisionLoopError, match="DECISION_LOOP_NOT_STARTABLE"):
            await loop.run()
        loop.request_stop()
        await task

        with pytest.raises(DecisionLoopError, match="DECISION_LOOP_NOT_STARTABLE"):
            await loop.run()

    asyncio.run(scenario())


def test_handler_failure_fails_closed_and_preserves_cause() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress()

        async def failing_handler(_: RuntimeDispatch[str]) -> None:
            raise ValueError("sensitive downstream detail")

        loop = DecisionEventLoop(ingress=ingress, handler=failing_handler)
        ingress.publish("first", source=RuntimeEventSource.INTERNAL)
        ingress.publish("discarded", source=RuntimeEventSource.INTERNAL)
        ingress.close()

        with pytest.raises(DecisionLoopDispatchError) as exc_info:
            await loop.run()

        assert exc_info.value.code == "DECISION_HANDLER_FAILED"
        assert exc_info.value.dispatch_sequence == 1
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert loop.status is DecisionLoopStatus.FAILED
        assert ingress.depth().total == 0
        assert loop.request_stop() == 0

    asyncio.run(scenario())


def test_cancellation_closes_ingress_and_discards_pending_work() -> None:
    async def scenario() -> None:
        ingress, _ = make_ingress()
        loop = DecisionEventLoop(ingress=ingress, handler=lambda _: asyncio.sleep(0))
        task = asyncio.create_task(loop.run())
        while loop.status is DecisionLoopStatus.CREATED:
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert loop.status is DecisionLoopStatus.STOPPED
        assert ingress.depth().closed is True
        assert ingress.publish("late", source=RuntimeEventSource.INTERNAL) is PublishOutcome.CLOSED

    asyncio.run(scenario())
