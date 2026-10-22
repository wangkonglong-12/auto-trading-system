"""长期 QMT 只读节点适配器；报单与撤单在适配器边界永久拒绝。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Lock

from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.cicc_qmt_facts import CiccQmtFactsClient
from auto_trading_system.gateways.broker.models import (
    BrokerOrder,
    BrokerTrade,
    CancellationReceipt,
    CancelOrder,
    SubmissionReceipt,
    SubmitOrder,
)
from auto_trading_system.gateways.broker.port import BrokerEventSink
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeConfig,
    QmtReadOnlyAdapterError,
    QmtSignal,
    QmtSignalKind,
)
from auto_trading_system.runtime.recovery_v2 import AccountFact


class CiccQmtReadOnlyGateway:
    """One SDK session owned by SDKDispatcher; broker activity remains untrusted.

    Order and trade conversion is deliberately unavailable until the broker ID,
    client ID and external/manual ownership contract is verified. A non-empty
    order/trade query blocks recovery rather than losing real broker activity.
    """

    def __init__(
        self,
        config: QmtProbeConfig,
        account_id: str,
        account_alias: str,
        clock: Callable[[], datetime],
        *,
        bindings: XtquantBindings | None = None,
        unavailable_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._client = CiccQmtFactsClient(config, account_id, bindings=bindings)
        self._account_alias = account_alias
        self._clock = clock
        self._unavailable_sink = unavailable_sink
        self._signal_lock = Lock()
        self._invalid_signal = False
        self._started = False
        self._subscribed = False
        self._last_account: AccountFact | None = None
        self._last_trade_count = 0

    def _accept_signal(self, signal: QmtSignal) -> None:
        if signal.kind is QmtSignalKind.ACCOUNT_STATUS and signal.status == 0:
            return
        reason = {
            QmtSignalKind.DISCONNECTED: "QMT_SDK_DISCONNECTED",
            QmtSignalKind.INVALID_ACCOUNT_STATUS: "QMT_ACCOUNT_STATUS_INVALID",
            QmtSignalKind.ACCOUNT_STATUS: "QMT_ACCOUNT_STATUS_NOT_READY",
        }[signal.kind]
        with self._signal_lock:
            first_failure = not self._invalid_signal
            self._invalid_signal = True
        if first_failure and self._unavailable_sink is not None:
            self._unavailable_sink(reason)

    def _healthy(self) -> None:
        with self._signal_lock:
            invalid = self._invalid_signal
        if not self._started or invalid:
            raise QmtReadOnlyAdapterError("QMT_GATEWAY_UNAVAILABLE", "QMT session unavailable")

    def start(self, event_sink: BrokerEventSink) -> None:
        del event_sink  # Real order callbacks are not mapped in this read-only profile.
        if self._started:
            raise QmtReadOnlyAdapterError("QMT_GATEWAY_ALREADY_STARTED", "session already started")
        try:
            self._client.register_signal_sink(self._accept_signal)
            self._client.start()
            if self._client.connect() != 0:
                raise QmtReadOnlyAdapterError("QMT_GATEWAY_CONNECT_FAILED", "connection failed")
            if self._client.subscribe() != 0:
                raise QmtReadOnlyAdapterError("QMT_GATEWAY_SUBSCRIBE_FAILED", "subscription failed")
            self._subscribed = True
            self._started = True
            self._require_active_stock_account()
        except BaseException:
            try:
                if self._subscribed:
                    self._client.unsubscribe()
            finally:
                self._subscribed = False
                self._client.stop()
                self._started = False
            raise

    def _require_active_stock_account(self) -> None:
        self._healthy()
        statuses = self._client.query_account_statuses().target_statuses
        if len(statuses) != 1 or statuses[0].account_type != 2 or statuses[0].status != 0:
            raise QmtReadOnlyAdapterError("QMT_GATEWAY_ACCOUNT_NOT_OK", "account unavailable")
        self._healthy()

    def stop(self) -> None:
        if not self._started:
            self._client.stop()
            return
        try:
            if self._subscribed and self._client.unsubscribe() != 0:
                raise QmtReadOnlyAdapterError("QMT_GATEWAY_UNSUBSCRIBE_FAILED", "cleanup failed")
        finally:
            self._subscribed = False
            self._started = False
            self._client.stop()

    def query_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_active_stock_account()
        facts = self._client.read_facts(self._account_alias, self._clock())
        self._healthy()
        self._last_account = facts.account
        self._last_trade_count = facts.trade_count
        if facts.order_count or facts.trade_count:
            raise QmtReadOnlyAdapterError(
                "QMT_GATEWAY_ACTIVITY_UNMAPPED", "broker activity needs mapping"
            )
        return ()

    def query_trades(self) -> tuple[BrokerTrade, ...]:
        self._healthy()
        if self._last_account is None or self._last_trade_count:
            raise QmtReadOnlyAdapterError(
                "QMT_GATEWAY_ACTIVITY_UNMAPPED", "broker activity needs mapping"
            )
        return ()

    def read_account(self) -> AccountFact:
        self._healthy()
        if self._last_account is None:
            raise QmtReadOnlyAdapterError("QMT_GATEWAY_SNAPSHOT_MISSING", "account unavailable")
        return self._last_account

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        del client_order_id
        self._healthy()
        if self._last_account is None:
            raise QmtReadOnlyAdapterError("QMT_GATEWAY_SNAPSHOT_MISSING", "account unavailable")
        return None

    def submit_order(self, command: SubmitOrder) -> SubmissionReceipt:
        del command
        raise QmtReadOnlyAdapterError("QMT_GATEWAY_READ_ONLY", "order submission disabled")

    def cancel_order(self, command: CancelOrder) -> CancellationReceipt:
        del command
        raise QmtReadOnlyAdapterError("QMT_GATEWAY_READ_ONLY", "order cancellation disabled")
