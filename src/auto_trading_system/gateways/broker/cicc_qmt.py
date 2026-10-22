"""CICC MiniQMT adapter restricted to the QMT-001 read-only field probe."""

from __future__ import annotations

import hmac
import importlib
import importlib.metadata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from auto_trading_system.monitoring.qmt_readonly import (
    QmtAccountStatus,
    QmtProbeConfig,
    QmtReadOnlyAdapterError,
    QmtSignal,
    QmtSignalKind,
    QmtSignalSink,
    QmtStatusQuery,
)


@dataclass(frozen=True, slots=True)
class XtquantBindings:
    """Runtime-loaded SDK types; injectable so tests never require a broker SDK."""

    trader_type: Callable[..., Any]
    callback_type: type[Any]
    account_type: Callable[..., Any]
    sdk_version: str | None


@dataclass(frozen=True, slots=True)
class DiscoveredQmtAccount:
    """账号只在进程内传递；默认 repr 不泄漏完整账号。"""

    account_id: str = field(repr=False)
    account_type: int
    source: str = "ACCOUNT_INFO"


def discover_qmt_accounts(
    config: QmtProbeConfig, *, bindings: XtquantBindings | None = None
) -> tuple[DiscoveredQmtAccount, ...]:
    """只查询终端公开的账号列表，不读取 userdata 文件或登录凭据。

    调用方必须持有安装目录级探测锁。取得账号后关闭发现连接，再取得
    账户级锁执行正式只读探针；不能把这个连接升级成交易会话。
    """
    sdk = load_xtquant_bindings() if bindings is None else bindings
    trader = sdk.trader_type(str(config.userdata_mini), config.session_id)
    try:
        trader.register_callback(sdk.callback_type())
        trader.start()
        result = trader.connect()
        if type(result) is not int or result != 0:
            raise QmtReadOnlyAdapterError("QMT_DISCOVERY_CONNECT_FAILED", "connection failed")
        accounts = trader.query_account_infos()
        source = "ACCOUNT_INFO"
        if isinstance(accounts, list) and not accounts:
            # 部分券商终端未发布账号信息列表，继续以独立只读状态查询交叉核验。
            # None仍是查询异常，不能变成空账户；最终账户健康由QMT-001主动确认。
            accounts = trader.query_account_status()
            source = "ACCOUNT_STATUS"
        if not isinstance(accounts, list) or len(accounts) > 32:
            raise QmtReadOnlyAdapterError("QMT_DISCOVERY_RESULT_INVALID", "account list invalid")
        found: list[DiscoveredQmtAccount] = []
        seen: set[tuple[str, int]] = set()
        for account in accounts:
            account_id, account_type = account.account_id, account.account_type
            if (
                not isinstance(account_id, str)
                or not 4 <= len(account_id) <= 64
                or not account_id.isascii()
                or not account_id.isalnum()
                or type(account_type) is not int
                or (account_id, account_type) in seen
            ):
                raise QmtReadOnlyAdapterError(
                    "QMT_DISCOVERY_ACCOUNT_INVALID", "account identity invalid"
                )
            seen.add((account_id, account_type))
            found.append(DiscoveredQmtAccount(account_id, account_type, source))
        return tuple(found)
    except QmtReadOnlyAdapterError:
        raise
    except Exception as exc:
        raise QmtReadOnlyAdapterError("QMT_DISCOVERY_FAILED", "account discovery failed") from exc
    finally:
        try:
            trader.stop()
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_DISCOVERY_CLEANUP_FAILED", "discovery cleanup failed"
            ) from exc


def load_xtquant_bindings() -> XtquantBindings:
    """Load only the three SDK types used by the read-only probe."""
    try:
        package_module = importlib.import_module("xtquant")
        trader_module = importlib.import_module("xtquant.xttrader")
        account_module = importlib.import_module("xtquant.xttype")
        trader_type = trader_module.XtQuantTrader
        callback_type = trader_module.XtQuantTraderCallback
        account_type = account_module.StockAccount
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise QmtReadOnlyAdapterError(
            "XTQUANT_IMPORT_FAILED",
            "required XtQuant probe types are unavailable",
        ) from exc
    if (
        not callable(trader_type)
        or not isinstance(callback_type, type)
        or not callable(account_type)
    ):
        raise QmtReadOnlyAdapterError(
            "XTQUANT_API_INCOMPATIBLE",
            "required XtQuant probe types have an incompatible shape",
        )

    try:
        raw_version: object = importlib.metadata.version("xtquant")
    except importlib.metadata.PackageNotFoundError:
        raw_version = getattr(package_module, "__version__", None)
    sdk_version = (
        raw_version
        if isinstance(raw_version, str) and 0 < len(raw_version) <= 64 and raw_version.isprintable()
        else None
    )
    return XtquantBindings(
        trader_type=trader_type,
        callback_type=callback_type,
        account_type=account_type,
        sdk_version=sdk_version,
    )


class CiccQmtReadOnlyClient:
    """Expose no query, order or cancellation method beyond QMT-001 requirements."""

    def __init__(
        self,
        config: QmtProbeConfig,
        account_id: str,
        *,
        bindings: XtquantBindings | None = None,
    ) -> None:
        if (
            not isinstance(account_id, str)
            or not 4 <= len(account_id) <= 64
            or not account_id.isascii()
            or not account_id.isalnum()
        ):
            raise QmtReadOnlyAdapterError(
                "QMT_ACCOUNT_CREDENTIAL_INVALID",
                "broker account credential has an invalid shape",
            )
        self._bindings = load_xtquant_bindings() if bindings is None else bindings
        self._account_id = account_id
        self._signal_sink: QmtSignalSink | None = None
        self._callback: object | None = None
        self._started = False
        self._connected = False
        self._subscribed = False
        self._stopped = False
        try:
            self._account = self._bindings.account_type(account_id, config.account_type)
            self._trader = self._bindings.trader_type(
                str(Path(config.userdata_mini)),
                config.session_id,
            )
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_CLIENT_CREATE_FAILED",
                "XtQuant read-only client could not be created",
            ) from exc

    def __repr__(self) -> str:
        return "CiccQmtReadOnlyClient(<protected>)"

    @property
    def sdk_version(self) -> str | None:
        return self._bindings.sdk_version

    def register_signal_sink(self, sink: QmtSignalSink) -> None:
        if self._started or self._signal_sink is not None or not callable(sink):
            raise QmtReadOnlyAdapterError(
                "QMT_CALLBACK_REGISTRATION_INVALID",
                "callback registration order is invalid",
            )
        owner = self
        callback_base = self._bindings.callback_type

        class ReadOnlyCallback(callback_base):  # type: ignore[misc, valid-type]
            def on_disconnected(callback_self) -> None:
                del callback_self
                owner._emit(QmtSignal(kind=QmtSignalKind.DISCONNECTED))

            def on_account_status(callback_self, status: object) -> None:
                del callback_self
                owner._accept_account_status(status)

        self._signal_sink = sink
        try:
            callback = ReadOnlyCallback()
            self._trader.register_callback(callback)
        except Exception as exc:
            self._signal_sink = None
            raise QmtReadOnlyAdapterError(
                "QMT_CALLBACK_REGISTRATION_FAILED",
                "XtQuant callback could not be registered",
            ) from exc
        self._callback = callback

    def start(self) -> None:
        if self._signal_sink is None or self._started or self._stopped:
            raise QmtReadOnlyAdapterError(
                "QMT_START_ORDER_INVALID",
                "XtQuant start order is invalid",
            )
        try:
            self._trader.start()
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_START_FAILED",
                "XtQuant trading thread could not start",
            ) from exc
        self._started = True

    def connect(self) -> int:
        if not self._started or self._connected or self._stopped:
            raise QmtReadOnlyAdapterError(
                "QMT_CONNECT_ORDER_INVALID",
                "XtQuant connect order is invalid",
            )
        result = self._call_for_int("connect", "QMT_CONNECT_RESULT_INVALID")
        self._connected = result == 0
        return result

    def subscribe(self) -> int:
        if not self._connected or self._subscribed or self._stopped:
            raise QmtReadOnlyAdapterError(
                "QMT_SUBSCRIBE_ORDER_INVALID",
                "XtQuant subscribe order is invalid",
            )
        result = self._call_for_int(
            "subscribe",
            "QMT_SUBSCRIBE_RESULT_INVALID",
            self._account,
        )
        self._subscribed = result == 0
        return result

    def query_account_statuses(self) -> QmtStatusQuery:
        if not self._subscribed or self._stopped:
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_QUERY_ORDER_INVALID",
                "XtQuant account status query order is invalid",
            )
        try:
            result = self._trader.query_account_status()
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_QUERY_FAILED",
                "XtQuant account status query failed",
            ) from exc
        if result is None:
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_QUERY_NONE",
                "XtQuant account status query returned None",
            )
        if not isinstance(result, (list, tuple)):
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_QUERY_TYPE_INVALID",
                "XtQuant account status query returned an incompatible type",
            )

        target_statuses: list[QmtAccountStatus] = []
        for item in result:
            fields = self._copy_status_fields(item)
            # 一个资金号可同时出现在股票、沪港通和深港通账户中。
            if hmac.compare_digest(fields[0], self._account_id) and fields[1] == 2:
                target_statuses.append(QmtAccountStatus(account_type=fields[1], status=fields[2]))
        if len(target_statuses) > 1:
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_TARGET_DUPLICATE",
                "XtQuant returned duplicate target account statuses",
            )
        return QmtStatusQuery(
            total_account_count=len(result),
            target_statuses=tuple(target_statuses),
        )

    def unsubscribe(self) -> int:
        if not self._subscribed or self._stopped:
            raise QmtReadOnlyAdapterError(
                "QMT_UNSUBSCRIBE_ORDER_INVALID",
                "XtQuant unsubscribe order is invalid",
            )
        result = self._call_for_int(
            "unsubscribe",
            "QMT_UNSUBSCRIBE_RESULT_INVALID",
            self._account,
        )
        if result == 0:
            self._subscribed = False
        return result

    def stop(self) -> None:
        if self._stopped:
            return
        try:
            self._trader.stop()
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_STOP_FAILED",
                "XtQuant trading thread could not stop",
            ) from exc
        finally:
            self._stopped = True
            self._connected = False
            self._subscribed = False

    def _call_for_int(self, method_name: str, invalid_code: str, *arguments: object) -> int:
        try:
            method = getattr(self._trader, method_name)
            result = method(*arguments)
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                invalid_code.replace("RESULT_INVALID", "CALL_FAILED"),
                "XtQuant read-only call failed",
            ) from exc
        if type(result) is not int:
            raise QmtReadOnlyAdapterError(
                invalid_code,
                "XtQuant read-only call returned an incompatible result",
            )
        return result

    def _accept_account_status(self, item: object) -> None:
        try:
            account_id, account_type, status = self._copy_status_fields(item)
        except QmtReadOnlyAdapterError:
            self._emit(QmtSignal(kind=QmtSignalKind.INVALID_ACCOUNT_STATUS))
            return
        if hmac.compare_digest(account_id, self._account_id) and account_type == 2:
            self._emit(
                QmtSignal(
                    kind=QmtSignalKind.ACCOUNT_STATUS,
                    account_type=account_type,
                    status=status,
                )
            )

    @staticmethod
    def _copy_status_fields(item: object) -> tuple[str, int, int]:
        try:
            sdk_status = cast(Any, item)
            account_id = sdk_status.account_id
            account_type = sdk_status.account_type
            status = sdk_status.status
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_PAYLOAD_INVALID",
                "XtQuant account status is missing required fields",
            ) from exc
        if (
            not isinstance(account_id, str)
            or type(account_type) is not int
            or type(status) is not int
        ):
            raise QmtReadOnlyAdapterError(
                "QMT_STATUS_PAYLOAD_INVALID",
                "XtQuant account status fields have incompatible types",
            )
        return account_id, account_type, status

    def _emit(self, signal: QmtSignal) -> None:
        sink = self._signal_sink
        if sink is None:
            return
        try:
            sink(signal)
        except Exception:
            return
