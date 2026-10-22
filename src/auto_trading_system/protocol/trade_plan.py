"""Semantic checks that complement the TradePlan JSON Schema."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from auto_trading_system.protocol.canonical_json import verify_plan_hash
from auto_trading_system.protocol.compat import ProtocolVersionError, require_supported_major

SHANGHAI_OFFSET = timedelta(hours=8)
TIMESTAMP_FIELDS = (
    "generated_at",
    "valid_from",
    "entry_start",
    "stop_new_orders_at",
    "expire_at",
)


class TradePlanSemanticError(ValueError):
    """Raised when a schema-valid TradePlan violates a business invariant."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{code} at {field}: {message}")
        self.code = code
        self.field = field


def _fail(code: str, field: str, message: str) -> NoReturn:
    raise TradePlanSemanticError(code, field, message)


def _parse_datetime(plan: Mapping[str, Any], field: str) -> datetime:
    raw = plan.get(field)
    if not isinstance(raw, str):
        _fail("INVALID_TIMESTAMP", field, "must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TradePlanSemanticError("INVALID_TIMESTAMP", field, str(exc)) from exc
    if parsed.utcoffset() != SHANGHAI_OFFSET:
        _fail("INVALID_TIMEZONE_OFFSET", field, "must use +08:00")
    return parsed


def _parse_ratio(container: Mapping[str, Any], field: str) -> Decimal:
    raw = container.get(field)
    if not isinstance(raw, str):
        _fail("INVALID_DECIMAL", field, "must be a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise TradePlanSemanticError("INVALID_DECIMAL", field, str(exc)) from exc
    if value < 0 or value > 1:
        _fail("RATIO_OUT_OF_RANGE", field, "must be between 0 and 1")
    return value


def validate_trade_plan_semantics(plan: Mapping[str, Any]) -> None:
    """Validate ordering, eligibility, exposure and digest invariants."""
    version = plan.get("schema_version")
    if not isinstance(version, str):
        _fail("INVALID_SCHEMA_VERSION", "schema_version", "must be a string")
    try:
        require_supported_major(version)
    except ProtocolVersionError as exc:
        raise TradePlanSemanticError(
            "PLAN_VERSION_UNSUPPORTED",
            "schema_version",
            str(exc),
        ) from exc

    if plan.get("timezone") != "Asia/Shanghai":
        _fail("INVALID_TIMEZONE", "timezone", "must be Asia/Shanghai")

    raw_trade_date = plan.get("trade_date")
    if not isinstance(raw_trade_date, str):
        _fail("INVALID_TRADE_DATE", "trade_date", "must be an ISO date string")
    try:
        trade_date = date.fromisoformat(raw_trade_date)
    except ValueError as exc:
        raise TradePlanSemanticError("INVALID_TRADE_DATE", "trade_date", str(exc)) from exc

    timestamps = {field: _parse_datetime(plan, field) for field in TIMESTAMP_FIELDS}
    timeline = [timestamps[field] for field in TIMESTAMP_FIELDS]
    entry_window_is_empty = timestamps["entry_start"] == timestamps["stop_new_orders_at"]
    if timeline != sorted(timeline) or entry_window_is_empty:
        _fail(
            "INVALID_PLAN_TIMELINE",
            "generated_at..expire_at",
            "timestamps must be ordered and the entry window must be non-empty",
        )
    if any(timestamp.date() != trade_date for timestamp in timeline):
        _fail("TRADE_DATE_MISMATCH", "trade_date", "all timestamps must use the trade date")

    items = plan.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        _fail("INVALID_ITEMS", "items", "must be an array")
    status = plan.get("status")
    if status == "READY" and not items:
        _fail("READY_PLAN_EMPTY", "items", "READY plans require at least one item")
    if status == "NO_TRADE" and items:
        _fail("NO_TRADE_HAS_ITEMS", "items", "NO_TRADE plans require an empty item array")

    source = plan.get("source")
    if not isinstance(source, Mapping):
        _fail("INVALID_SOURCE", "source", "must be an object")
    live_eligible = plan.get("live_eligible") is True
    if source.get("data_completeness") != "full" and live_eligible:
        _fail(
            "INCOMPLETE_PLAN_LIVE_ELIGIBLE",
            "live_eligible",
            "partial or unknown source data cannot be live eligible",
        )
    if live_eligible and source.get("run_status") != "succeeded":
        _fail("SOURCE_NOT_SUCCEEDED", "source.run_status", "live plans require succeeded source")

    portfolio = plan.get("portfolio")
    if not isinstance(portfolio, Mapping):
        _fail("INVALID_PORTFOLIO", "portfolio", "must be an object")
    target = _parse_ratio(portfolio, "target_exposure")
    hard_max = _parse_ratio(portfolio, "hard_max_exposure")
    cash_reserve = _parse_ratio(portfolio, "cash_reserve")
    if target > hard_max:
        _fail("EXPOSURE_EXCEEDS_HARD_MAX", "portfolio.target_exposure", "exceeds hard maximum")
    if target + cash_reserve > 1:
        _fail("CASH_RESERVE_VIOLATION", "portfolio.cash_reserve", "target plus reserve exceeds 1")

    seen_intent_ids: set[str] = set()
    seen_symbols: set[str] = set()
    seen_ranks: set[int] = set()
    total_target_weight = Decimal(0)
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            _fail("INVALID_ITEM", f"items[{index}]", "must be an object")

        intent_id = item.get("intent_id")
        symbol = item.get("symbol")
        rank = item.get("rank")
        if not isinstance(intent_id, str):
            _fail("INVALID_ITEM_IDENTITY", f"items[{index}].intent_id", "must be a string")
        if not isinstance(symbol, str):
            _fail("INVALID_ITEM_IDENTITY", f"items[{index}].symbol", "must be a string")
        if not isinstance(rank, int):
            _fail("INVALID_ITEM_IDENTITY", f"items[{index}].rank", "must be an integer")
        if intent_id in seen_intent_ids:
            _fail("DUPLICATE_INTENT_ID", f"items[{index}].intent_id", intent_id)
        if symbol in seen_symbols:
            _fail("DUPLICATE_SYMBOL", f"items[{index}].symbol", symbol)
        if rank in seen_ranks:
            _fail("DUPLICATE_RANK", f"items[{index}].rank", str(rank))

        seen_intent_ids.add(intent_id)
        seen_symbols.add(symbol)
        seen_ranks.add(rank)
        total_target_weight += _parse_ratio(item, "target_weight")

    if total_target_weight > target:
        _fail(
            "ITEM_WEIGHT_EXCEEDS_TARGET",
            "items[*].target_weight",
            "sum of item weights exceeds portfolio target exposure",
        )

    if not verify_plan_hash(plan):
        _fail("PLAN_HASH_MISMATCH", "plan_hash", "digest does not match canonical plan content")
