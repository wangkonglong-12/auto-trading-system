# ruff: noqa: RUF001
"""Best-effort display history. No broker, control, or critical-journal access.

One console worker samples existing cached status and the latest page observation.
Queues, history, file size and wire response are bounded. Disk/network work never
runs on the engine owner, SDK callbacks, or while holding the display read lock.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.domain.trading_time import SHANGHAI_TIMEZONE
from auto_trading_system.protocol.tail_picks import TailPicks

MAX_EVENTS = 500
MAX_VISIBLE = 200
MAX_BYTES = 1024 * 1024
REFRESH_STABILITY_SECONDS = 5
_SYMBOL = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,95}$")
_PROGRESS = {
    "WAITING_MARKET": "等待合格行情和完整成交量窗口",
    "WAITING_WINDOW": "等待买入时间",
    "WAITING_CAPACITY": "正在核对资金、数量及盘口条件",
    "ORDER_ACTIVE": "已有买入委托，等待成交",
    "FINISHED": "本批买入目标执行结束",
    "POSITION_CAPACITY": "可用股票名额不足，未买入",
    "SKIPPED_MIN_ORDER": "金额不足最小申报数量，未买入",
    "MISSED_ENTRY_WINDOW": "已过买入时间，未买入",
    "WINDOW_CLOSED": "买入窗口已结束",
    "CHILD_LIMIT": "已达到委托次数上限，停止新增委托",
    "DUPLICATE_SIGNAL": "重复入选，沿用当天原买入目标",
    "MANUAL_BUY_MANAGED": "由手动买入接管，自动策略不再新增买入",
    "WAITING_RECOVERY": "等待账户及持仓核对",
}
_ORDER = {
    "SUBMITTING": "正在提交委托",
    "ACKED": "券商已接收委托，等待成交",
    "PARTIALLY_FILLED": "券商回报部分成交，等待成交核对",
    "FILLED": "券商回报全部成交，等待成交核对",
    "CANCEL_PENDING": "撤单申请处理中",
    "PARTIAL_FILL_CANCEL_PENDING": "部分成交，撤单申请处理中",
    "CANCELLED": "未成交委托已撤销",
    "PARTIALLY_FILLED_CANCELLED": "部分成交，剩余委托已撤销",
    "REJECTED": "券商已拒绝委托",
    "UNKNOWN": "委托结果待核对，勿重复提交",
    "ERROR_HOLD": "委托异常，等待核对",
}


def _dict(value: object) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _rows(value: object, maximum: int = 100) -> list[dict[str, JsonValue]]:
    return (
        [_dict(v) for v in value[:maximum] if isinstance(v, dict)]
        if isinstance(value, list)
        else []
    )


def _symbol(value: object) -> str | None:
    return value if isinstance(value, str) and _SYMBOL.fullmatch(value) else None


def _name(value: object) -> str | None:
    return (
        "".join(c for c in value if c.isprintable())[:64] or None
        if isinstance(value, str)
        else None
    )


def _code(value: object) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else "原因待核对"


def _text(value: object) -> str:
    return str(value)[:32] if isinstance(value, (str, int)) and not isinstance(value, bool) else "—"


class TradingProcessLog:
    def __init__(
        self,
        root: Path,
        *,
        node_id: str,
        account_id: str,
        status: Callable[[], dict[str, JsonValue]],
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self.path = root / "operator/trading_process.json"
        self.feed_path = root / "critical/tail-feed-state.json"
        self.closing_path = root / "critical/opening-reference.json"
        self.node_id, self.account_id = node_id, account_id
        self.status, self.clock, self.elapsed = status, clock, elapsed
        self._lock, self._stop = Lock(), Event()
        self._thread: Thread | None = None
        self._events: deque[dict[str, JsonValue]] = deque(maxlen=MAX_EVENTS)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._stocks: dict[str, str | None] = {}
        self._pending: tuple[dict[str, JsonValue], dict[str, JsonValue]] | None = None
        self._day = self.clock().astimezone(SHANGHAI_TIMEZONE).date().isoformat()
        self._stream, self._seq = uuid4().hex, 0
        self._dirty = False
        self._warning: str | None = None
        self._refresh_pending: dict[str, tuple[str, float]] = {}
        self._refresh_notices: dict[str, str] = {}
        self._file_stamps: dict[Path, tuple[int, int]] = {}
        self._file_errors: set[Path] = set()
        self._load()

    @staticmethod
    def _read(path: Path, maximum: int) -> dict[str, JsonValue]:
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("display file exceeds bound")
        return _dict(json.loads(raw))

    def _load(self) -> None:
        try:
            saved = self._read(self.path, MAX_BYTES)
            if (saved.get("node_id"), saved.get("account_id"), saved.get("day")) != (
                self.node_id,
                self.account_id,
                self._day,
            ):
                return
            events = _rows(saved.get("events"), MAX_EVENTS)
            legacy_progress: set[tuple[str, str]] = set()
            for row in events:
                if (
                    type(row.get("seq")) is not int
                    or int(str(row["seq"])) <= self._seq
                    or row.get("level") not in {"INFO", "WAIT", "ERROR", "SUCCESS"}
                    or not isinstance(row.get("message"), str)
                    or len(str(row["message"])) > 256
                    or datetime.fromisoformat(str(row.get("occurred_at")))
                    .astimezone(SHANGHAI_TIMEZONE)
                    .date()
                    .isoformat()
                    != self._day
                ):
                    raise ValueError("invalid display event")
                self._seq = int(str(row["seq"]))
                message = str(row["message"])
                if row.get("level") == "INFO" and message in {
                    "买入目标执行结束，等待实际成交核对",
                    "重复入选，沿用当天原买入目标",
                }:
                    identity = (str(row.get("symbol")), message)
                    if identity in legacy_progress:
                        self._dirty = True
                        continue
                    legacy_progress.add(identity)
                    if message == "买入目标执行结束，等待实际成交核对":
                        row = {**row, "message": _PROGRESS["FINISHED"]}
                        self._dirty = True
                self._events.append(
                    {k: row.get(k) for k in ("seq", "occurred_at", "level", "symbol", "message")}
                )
            self._stream = str(saved.get("stream_id"))[:64]
            for row in _rows(saved.get("stocks"), 200):
                symbol = _symbol(row.get("symbol"))
                if symbol:
                    self._stocks[symbol] = _name(row.get("name"))
            for key, state in list(_dict(saved.get("seen")).items())[-2048:]:
                if len(key) <= 160 and isinstance(state, str) and len(state) <= 512:
                    self._seen[key] = state
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self._events.clear()
            self._seq, self._stream = 0, uuid4().hex
            self._warning = "日志历史暂不可读取，当前显示本次采集记录。"

    def start(self) -> None:
        try:
            self._thread = Thread(target=self._run, name="console-process-log", daemon=True)
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._warning = "交易过程采集暂不可用，交易执行不受此窗口控制。"

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def offer(self, details: Mapping[str, JsonValue], buys: dict[str, JsonValue]) -> None:
        # Latest-only handoff: a slow writer can never build a queue of requests.
        snapshot: dict[str, JsonValue] = {
            k: details.get(k)
            for k in ("available", "coherent", "truncated", "observed_at", "trade_date")
        }
        for key in ("orders", "trades", "submissions", "sell_submissions", "sell_positions"):
            snapshot[key] = [dict(row) for row in _rows(details.get(key))]
        snapshot["names"] = dict(list(_dict(details.get("names")).items())[:200])
        buy_view: dict[str, JsonValue] = {
            k: buys.get(k) for k in ("available", "display_state", "selection_date")
        }
        buy_view["rows"] = [dict(row) for row in _rows(buys.get("rows"))]
        with self._lock:
            self._pending = snapshot, buy_view

    def _stock(self, symbol: object, name: object = None) -> str | None:
        code = _symbol(symbol)
        if code and (code in self._stocks or len(self._stocks) < 200):
            cleaned = _name(name) or self._stocks.get(code)
            if code not in self._stocks or self._stocks[code] != cleaned:
                self._stocks[code] = cleaned
                self._dirty = True
        return code

    def _emit(
        self,
        key: str,
        state: object,
        message: str,
        now: datetime,
        level: str = "INFO",
        symbol: str | None = None,
    ) -> None:
        fingerprint = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)[:512]
        previous = self._seen.get(key)
        self._seen[key] = fingerprint
        self._seen.move_to_end(key)
        while len(self._seen) > 2048:
            self._seen.popitem(last=False)
        if previous == fingerprint:
            return
        self._seq += 1
        self._events.append(
            {
                "seq": self._seq,
                "occurred_at": now.isoformat(),
                "level": level,
                "symbol": symbol,
                "message": message[:256],
            }
        )
        self._dirty = True

    def _changed_file(self, path: Path, maximum: int) -> dict[str, JsonValue] | None:
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if self._file_stamps.get(path) == stamp:
                return None
            value = self._read(path, maximum)
            self._file_stamps[path] = stamp
            self._file_errors.discard(path)
            return value
        except FileNotFoundError:
            self._file_errors.discard(path)
            return None
        except (OSError, ValueError):
            self._file_errors.add(path)
            return None

    def _selection(self, saved: dict[str, JsonValue] | None, now: datetime) -> None:
        if saved is None:
            return
        feed = TailPicks.model_validate_json(json.dumps(saved.get("feed"), ensure_ascii=False))
        if feed.trade_date.isoformat() != self._day:
            return
        for batch in feed.batches:
            phase = batch.phase[:2] + ":" + batch.phase[2:]
            if batch.status == "FAILED":
                self._emit(
                    "batch:" + phase,
                    [batch.status, batch.error_code],
                    f"{phase}批次选股失败：" + _code(batch.error_code),
                    now,
                    "ERROR",
                )
                continue
            if batch.status != "READY":
                self._emit("batch:" + phase, batch.status, f"等待{phase}批次选股结果", now, "WAIT")
                continue
            self._emit(
                "batch:" + phase,
                "READY",
                f"{phase}批次选股已收到，共{len(batch.stocks)}只股票",
                now,
            )
            for stock in batch.stocks:
                code = self._stock(stock.code, stock.name)
                self._emit(
                    f"selection:{phase}:{code}",
                    "READY",
                    f"{phase}批次入选，第{stock.rank}名",
                    now,
                    symbol=code,
                )
        if feed.all_ready:
            self._emit("selection-complete", "READY", "三批选股结果已收齐", now)
        if saved.get("suspended") is True:
            self._emit(
                "selection-suspended", True, "选股接入已暂停，需要核对异常原因", now, "ERROR"
            )

    def _refresh_health(
        self,
        key: str,
        available: bool,
        unavailable_message: str,
        current_message: str,
        now: datetime,
    ) -> None:
        # Debounce only display reads, never broker events or trading permission.
        state = "CURRENT" if available else "UNAVAILABLE"
        previous = self._seen.get(key)
        if available and previous != json.dumps("UNAVAILABLE"):
            self._refresh_pending.pop(key, None)
            self._refresh_notices.pop(key, None)
            self._emit(key, state, current_message, now)
            return
        self._refresh_notices[key] = (
            current_message + "，正在确认稳定性" if available else unavailable_message
        )
        if previous == json.dumps(state):
            self._refresh_pending.pop(key, None)
            return
        elapsed = self.elapsed()
        pending = self._refresh_pending.get(key)
        if pending is None or pending[0] != state:
            self._refresh_pending[key] = state, elapsed
            return
        if elapsed - pending[1] < REFRESH_STABILITY_SECONDS:
            return
        self._emit(
            key,
            state,
            current_message if available else unavailable_message,
            now,
            "INFO" if available else "ERROR",
        )
        self._refresh_pending.pop(key, None)
        if available:
            self._refresh_notices.pop(key, None)

    def _status(self, snapshot: dict[str, JsonValue], now: datetime) -> None:
        boxes = _dict(snapshot.get("components"))
        box = _dict(boxes.get("engine"))
        current = box.get("availability") == "CURRENT"
        self._refresh_health(
            "connection",
            current,
            "交易引擎状态已过期，暂不能确认最新交易进展"
            if box.get("availability") == "STALE"
            else "交易引擎状态进度异常，暂不能确认最新交易进展"
            if box.get("error") == "PROGRESS_REGRESSED"
            else "页面暂时无法读取交易引擎状态，等待刷新核对",
            "交易引擎状态读取正常",
            now,
        )
        if not current:
            return
        engine = _dict(box.get("data"))
        if engine.get("trade_date") is not None and engine.get("trade_date") != self._day:
            self._emit(
                "engine-day",
                "WAITING",
                "等待新交易日账户核验，旧状态不作为当天交易结果",
                now,
                "WAIT",
            )
            return
        boot = engine.get("boot_id")
        if boot:
            self._emit("boot", boot, "已连接交易程序，开始观察本次交易过程", now)
        recovery = engine.get("recovery")
        self._emit(
            "recovery",
            [recovery, engine.get("recovery_error")],
            "账户核验通过"
            if recovery == "CLEAR"
            else "账户核验等待完成：" + _code(engine.get("recovery_error") or recovery),
            now,
            "INFO" if recovery == "CLEAR" else "ERROR" if engine.get("recovery_error") else "WAIT",
        )
        automatic = _dict(engine.get("daily_auto"))
        reason = automatic.get("reason")
        if automatic:
            message = (
                "持续自动买入已开启，等待14:40执行窗口"
                if reason == "WAITING_WINDOW"
                else "自动买入正在运行"
                if automatic.get("state") == "RUNNING"
                else "等待正式选股结果"
                if reason == "WAITING_FORMAL_PICKS"
                else "持续自动买入已关闭"
                if automatic.get("enabled") is False
                else "今日自动买入已结束，等待下一交易日"
                if reason == "WAITING_NEXT_DAY"
                else "自动买入状态：" + _code(reason)
            )
            self._emit(
                "automatic",
                [automatic.get("enabled"), automatic.get("state"), reason],
                message,
                now,
                "ERROR" if automatic.get("state") == "FAULT" else "INFO",
            )
        market = _dict(engine.get("market_session"))
        if market.get("failure_code"):
            self._emit(
                "market-error",
                market["failure_code"],
                "行情处理异常：" + _code(market["failure_code"]),
                now,
                "ERROR",
            )
        for row in reversed(_rows(market.get("recent_activity"), 80)):
            state = str(row.get("state", ""))
            category = row.get("category")
            if category not in {"ENTRY_SIGNAL", "ORDER", "ERROR", "ENTRY_TARGET"}:
                continue
            try:
                observed = datetime.fromisoformat(str(row.get("occurred_at")))
            except ValueError:
                continue
            if (
                observed.tzinfo is None
                or observed.astimezone(SHANGHAI_TIMEZONE).date().isoformat() != self._day
            ):
                continue
            code = self._stock(row.get("symbol"), row.get("name"))
            if code is None and category != "ERROR":
                continue
            if category == "ENTRY_SIGNAL":
                message = _PROGRESS.get(state, "买入进度：" + _code(state))
            elif category == "ORDER":
                message = _ORDER.get(state.split(":")[0], "委托状态待核对")
                detail = row.get("detail")
                if isinstance(detail, str) and re.fullmatch(
                    r"(BUY|SELL):[0-9]{1,10}/[0-9]{1,10}", detail
                ):
                    message += " · 累计成交/委托 " + detail.split(":")[1] + " 股"
            elif category == "ENTRY_TARGET":
                message = (
                    "买入目标已确定"
                    if state.startswith("BUY_TARGET_READY")
                    else _PROGRESS.get(str(row.get("detail")), "本股未生成买入目标")
                )
            else:
                message = "交易异常：" + _code(state)
            if row.get("phase") in {"1440", "1445", "1450"}:
                phase = str(row["phase"])
                message = phase[:2] + ":" + phase[2:] + "批：" + message
            # Informational signal observations are idempotent even when an old
            # producer refreshes their timestamps. Orders/errors retain their
            # original event identity so real transitions are never swallowed.
            identity_value = (
                {k: row.get(k) for k in ("category", "symbol", "context", "state", "detail")}
                if category in {"ENTRY_SIGNAL", "ENTRY_TARGET"}
                else row
            )
            identity = sha256(
                json.dumps(identity_value, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            self._emit(
                "activity:" + identity,
                True,
                message,
                observed,
                "ERROR"
                if category == "ERROR" or state.startswith(("REJECTED", "UNKNOWN", "ERROR_HOLD"))
                else "INFO",
                code,
            )
        agent_box = _dict(boxes.get("agent"))
        if agent_box.get("availability") == "CURRENT":
            agent = _dict(agent_box.get("data"))
            if str(agent.get("invocation_id", "")).startswith("tail-feed-"):
                self._emit("selection-start", self._day, "已开始获取当天选股结果", now)
                result = _dict(agent.get("last_result"))
                result_state = result.get("status")
                if result_state in {"RETRY", "SUSPENDED", "PENDING"}:
                    self._emit(
                        "selection-result",
                        [result_state, result.get("reason")],
                        "选股获取异常：" + _code(result.get("reason")),
                        now,
                        "ERROR",
                    )
                elif result_state in {"WAITING_PHASES", "COMPLETE"}:
                    self._emit(
                        "selection-result",
                        result_state,
                        "选股接口访问正常，等待后续批次"
                        if result_state == "WAITING_PHASES"
                        else "选股获取完成",
                        now,
                    )

    def _account(
        self, details: dict[str, JsonValue], buys: dict[str, JsonValue], now: datetime
    ) -> None:
        if not details.get("available") or details.get("truncated"):
            self._refresh_health(
                "account",
                False,
                "账户数据暂不可用或不完整，等待刷新核对",
                "账户交易数据已更新",
                now,
            )
            return
        try:
            observed = datetime.fromisoformat(str(details.get("observed_at")))
        except ValueError:
            self._emit(
                "account", "INVALID_TIME", "账户数据时间待核对，暂不确认交易结果", now, "ERROR"
            )
            return
        if (
            observed.tzinfo is None
            or not 0 <= (now - observed).total_seconds() < 15
            or observed.astimezone(SHANGHAI_TIMEZONE).date().isoformat() != self._day
        ):
            return
        self._refresh_health("account", True, "", "账户交易数据已更新", now)
        names = _dict(details.get("names"))
        for row in [*_rows(details.get("submissions")), *_rows(details.get("sell_submissions"))]:
            code = self._stock(
                row.get("symbol"), row.get("name") or names.get(str(row.get("symbol")))
            )
            state = row.get("status")
            prefix = "卖出：" if row.get("operation_id") else "买入："
            message = prefix + {
                "PERSISTING": "正在准备委托",
                "DISPATCHING": "正在发送委托",
                "SDK_ACCEPTED": "委托已提交，等待券商回报",
                "RECOVERED": "已恢复委托记录，继续核对",
            }.get(str(state), "委托结果待核对，勿重复提交")
            self._emit(
                "submission:" + str(row.get("client_order_id")),
                state,
                message,
                now,
                "INFO"
                if state in {"PERSISTING", "DISPATCHING", "SDK_ACCEPTED", "RECOVERED"}
                else "ERROR",
                code,
            )
        for row in _rows(details.get("orders")):
            if row.get("owned_by_system") is not True:
                continue
            code = self._stock(row.get("symbol"), names.get(str(row.get("symbol"))))
            state = str(row.get("status"))
            self._emit(
                "order:" + str(row.get("client_order_id")),
                [state, row.get("filled")],
                _ORDER.get(state, "委托状态待核对")
                + f" · 累计成交{_text(row.get('filled'))}/{_text(row.get('quantity'))}股"
                + f"，委托价{_text(row.get('limit_price'))}元",
                now,
                "ERROR" if state in {"REJECTED", "UNKNOWN", "ERROR_HOLD"} else "INFO",
                code,
            )
            cancel = row.get("cancel_state")
            if cancel in {"PENDING", "UNKNOWN", "FAILED", "CONFIRMED"}:
                message = {
                    "PENDING": "撤单申请处理中",
                    "UNKNOWN": "撤单结果待核对，勿重复提交",
                    "FAILED": "撤单失败：" + _code(row.get("cancel_reason")),
                    "CONFIRMED": "券商已确认撤单结果",
                }[str(cancel)]
                self._emit(
                    "cancel:" + str(row.get("client_order_id")),
                    [cancel, row.get("cancel_reason")],
                    message,
                    now,
                    "ERROR" if cancel in {"UNKNOWN", "FAILED"} else "INFO",
                    code,
                )
        for row in _rows(details.get("trades")):
            if row.get("owned_by_system") is not True:
                continue
            code = self._stock(row.get("symbol"), names.get(str(row.get("symbol"))))
            self._emit(
                "trade:" + str(row.get("trade_id")),
                True,
                f"实际{'卖出' if row.get('side') == 'SELL' else '买入'}成交"
                + f"{_text(row.get('quantity'))}股，成交价{_text(row.get('price'))}元",
                now,
                symbol=code,
            )
        for row in _rows(buys.get("rows")):
            if (
                details.get("coherent") is not True
                or row.get("is_snapshot") is True
                or (buys.get("display_state") == "SNAPSHOT" and row.get("is_snapshot") is not False)
                or not buys.get("available")
                or (row.get("trade_date") or buys.get("selection_date")) != self._day
            ):
                continue
            code = self._stock(row.get("symbol"), row.get("name"))
            if code is None:
                continue
            if (
                row.get("state") == "FILLED"
                and type(row.get("quantity")) is int
                and int(str(row["quantity"])) > 0
            ):
                self._emit(
                    "buy-result:" + str(code),
                    [row.get("quantity"), row.get("amount")],
                    f"买入成功：实际成交{_text(row.get('quantity'))}股，均价{_text(row.get('average_price'))}元，成交金额{_text(row.get('amount'))}元",
                    now,
                    "SUCCESS",
                    code,
                )
            elif row.get("state") in {"CANCELLED", "REJECTED", "PARTIALLY_FILLED_CANCELLED"}:
                self._emit(
                    "buy-terminal:" + code,
                    [row.get("state"), row.get("quantity")],
                    "买入结果：委托已拒绝，未买入"
                    if row.get("state") == "REJECTED"
                    else "买入结果：委托已撤销，未买入"
                    if row.get("state") == "CANCELLED"
                    else f"买入结果：实际成交{_text(row.get('quantity'))}股，剩余委托已撤销",
                    now,
                    "ERROR" if row.get("state") == "REJECTED" else "INFO",
                    code,
                )
            elif row.get("state") == "NOT_BOUGHT" and row.get("signal_state") in {
                "WINDOW_CLOSED",
                "SKIPPED_MIN_ORDER",
                "POSITION_CAPACITY",
                "MISSED_ENTRY_WINDOW",
            }:
                self._emit(
                    "buy-terminal:" + code,
                    row.get("signal_state"),
                    "买入结果：未买入，" + _PROGRESS[str(row["signal_state"])],
                    now,
                    symbol=code,
                )
        for row in _rows(details.get("sell_positions")):
            code = self._stock(row.get("symbol"), row.get("name"))
            if (
                code is not None
                and details.get("coherent") is True
                and row.get("state") in {"SOLD", "PARTIAL_SOLD"}
            ):
                self._emit(
                    "sell-result:" + code,
                    [row.get("sold_today"), row.get("remaining")],
                    f"卖出成交已核对：今日已卖{_text(row.get('sold_today'))}股，剩余{_text(row.get('remaining'))}股",
                    now,
                    symbol=code,
                )

    def collect(self) -> None:
        """One bounded pass; used by the console worker and focused offline tests."""
        now = self.clock().astimezone(SHANGHAI_TIMEZONE)
        status = self.status()  # Existing cache only; never an RPC or broker call.
        feed = self._changed_file(self.feed_path, 131072)
        debug = self._changed_file(self.feed_path.with_name("debug-pick.json"), 131072)
        closing = self._changed_file(self.closing_path, 65536)
        with self._lock:
            day = now.date().isoformat()
            if day != self._day:
                self._day, self._stream, self._seq = day, uuid4().hex, 0
                self._events.clear()
                self._seen.clear()
                self._stocks.clear()
                self._file_stamps.clear()
                self._refresh_pending.clear()
                self._refresh_notices.clear()
                self._dirty = True
            self._status(status, now)
            try:
                if debug:
                    payload = _dict(_dict(debug.get("plan")).get("payload"))
                    if payload.get("trade_date") == self._day:
                        for stock in _rows(payload.get("allocations")):
                            code = self._stock(stock.get("symbol"), debug.get("name"))
                            if code is None:
                                continue
                            self._emit(
                                "debug-selection:" + code,
                                "READY",
                                "调试选股已接收，共1只；使用真实账户执行自动买入",
                                now,
                                symbol=code,
                            )
                self._selection(feed, now)
            except (ValueError, TypeError):
                self._emit("selection-file", "INVALID", "选股记录格式异常，等待核对", now, "ERROR")
            pending, self._pending = self._pending, None
            if pending is not None:
                self._account(*pending, now)
            if (
                closing
                and closing.get("trade_date") == day
                and re.fullmatch(r"sha256:[0-9a-f]{64}", str(closing.get("payload_hash")))
            ):
                self._emit(
                    "closing",
                    closing.get("payload_hash"),
                    "当日持仓封存记录已保存，后续继续按实际持仓核对",
                    now,
                )
        self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            value = {
                "node_id": self.node_id,
                "account_id": self.account_id,
                "day": self._day,
                "stream_id": self._stream,
                "events": list(self._events),
                "stocks": [{"symbol": s, "name": n} for s, n in self._stocks.items()],
                "seen": dict(self._seen),
            }
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        try:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            if len(body) > MAX_BYTES:
                raise ValueError("display history exceeds byte bound")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            with self._lock:
                self._dirty, self._warning = False, None
        except (OSError, ValueError):
            with self._lock:
                self._warning = "过程日志暂不能保存，当前记录仍可查看；交易执行不受此窗口控制。"
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict[str, JsonValue]:
        with self._lock:
            warnings = [
                self._warning,
                "部分过程文件暂不可读取，继续展示可用的交易状态。" if self._file_errors else None,
                *self._refresh_notices.values(),
            ]
            return {
                "day": self._day,
                "stream_id": self._stream,
                "warning": " ".join(message for message in warnings if message) or None,
                "retained": len(self._events),
                "maximum": MAX_EVENTS,
                "stocks": [
                    {"symbol": s, "name": n or "名称待读取"} for s, n in self._stocks.items()
                ],
                "events": [
                    {**e, "name": self._stocks.get(str(e.get("symbol"))) or "名称待读取"}
                    for e in list(self._events)[-MAX_VISIBLE:]
                ],
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect()
            except Exception:
                with self._lock:
                    self._warning = "交易过程采集暂不完整，交易执行不受此窗口控制。"
            self._stop.wait(2)
