"""Loopback V2 console, composed separately from the legacy V1 application.

Browser sessions are issued and checked by the engine. Two HttpOnly, SameSite
cookies let a refreshed page recover its session; a CSRF proof must additionally
be supplied as an explicit header on every mutation. Cancellation is limited to
active orders already owned by this engine.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.domain import Price
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.runtime.single_instance import (
    AccountInstanceGuard,
    WindowsNamedMutexBackend,
)
from auto_trading_system.strategies.exit_v3 import (
    ExitLot,
    ExitObservation,
    ExitState,
    evaluate_exit,
)
from auto_trading_system.strategies.sell_rule_drafts import (
    SellRuleDraft,
    SellRuleDraftError,
    SellRuleDraftStore,
)
from auto_trading_system.strategies.sell_rules import SellRules
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.strategies.trading_settings_drafts import (
    TradingSettingsDraft,
    TradingSettingsDraftError,
    TradingSettingsDraftStore,
)

from .app import _CONTENT_SECURITY_POLICY, LocalConsoleAssetError, console_static_root
from .node_bridge import NodeConsoleBridge

PREFIX = "/api/v2/node"
SESSION_COOKIE = "ats_node_session"
CSRF_COOKIE = "ats_node_csrf"


class LoginInput(WireModel):
    password: str = Field(
        min_length=1, max_length=128, description="本机操作员口令，仅本次请求使用"
    )


class ActionInput(WireModel):
    action: Literal["HALT", "RECOVER"] = Field(description="急停或启动只读恢复核验")


class PreviewInput(WireModel):
    action: Literal["CONFIRM_RECOVERY", "ARM", "RESUME"] = Field(description="待预览确认的模拟动作")


class ConfirmInput(WireModel):
    preview_id: str = Field(pattern=r"^preview-[a-f0-9]{32}$", description="引擎生成的本次预览编号")
    phrase: str = Field(min_length=1, max_length=128, description="人工逐字输入的确认短语")


class ResultInput(WireModel):
    request_id: str = Field(pattern=r"^control-[a-f0-9]{32}$", description="待查询的原控制请求编号")


class CancelOrderInput(WireModel):
    client_order_id: str = Field(pattern=r"^ord_[A-Za-z0-9_-]{8,64}$")


class LiquidationInput(WireModel):
    phrase: Literal["确认清仓策略持仓"]


class SaveSellRulesInput(WireModel):
    expected_version: int = Field(ge=0)
    rules: SellRules


class SaveTradingSettingsInput(WireModel):
    expected_version: int = Field(ge=0)
    settings: TradingSettings


class TestSellRulesInput(WireModel):
    rules: SellRules
    buy_cost: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    current_price: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    observed_at: datetime


def _grant_for(
    config: NodeProcessConfig,
    settings: TradingSettingsDraft | None = None,
    snapshot: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Derive a grant from trusted local state, never browser supplied limits."""
    paper = config.paper_session
    limits = paper.limits if paper is not None else None
    if config.broker == "QMT_TRADING" and settings is not None:
        value = settings.settings
        components = snapshot.get("components") if isinstance(snapshot, dict) else None
        engine_box = components.get("engine") if isinstance(components, dict) else None
        engine = engine_box.get("data") if isinstance(engine_box, dict) else None
        directions: list[JsonValue] = []
        if isinstance(engine, dict) and engine.get("plan_loaded"):
            directions.append("BUY")
        if isinstance(engine, dict) and engine.get("exit_context_loaded"):
            directions.append("SELL")
        if not directions:
            directions = ["BUY"]
        return {
            "directions": directions,
            "max_buy_value": value.buy.max_daily_buy_value if "BUY" in directions else "0",
            "max_order_value": (value.buy.max_order_value if value.buy.enabled else "1000000.00"),
            "max_positions": value.safety.max_positions,
            "duration_seconds": 900,
        }
    return {
        "directions": ["SELL" if paper and paper.phase == "EXIT" else "BUY"],
        "max_buy_value": limits.max_buy_value if limits else "0.00",
        "max_order_value": limits.max_order_value if limits else "0.00",
        "max_positions": limits.max_positions if limits else 0,
        "duration_seconds": 60,
    }


def _rule_response(
    draft: SellRuleDraft | None, snapshot: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    components = snapshot.get("components")
    engine_box = components.get("engine") if isinstance(components, dict) else None
    engine = engine_box.get("data") if isinstance(engine_box, dict) else None
    paper = engine.get("paper_session") if isinstance(engine, dict) else None
    state = engine.get("state") if isinstance(engine, dict) else None
    current_hash = digest(canonical_bytes(draft.rules.model_dump(mode="json"))) if draft else None
    matches = (
        isinstance(paper, dict)
        and paper.get("phase") == "EXIT"
        and paper.get("rule_id") == "COST_EXIT_V1"
        and paper.get("sell_rules_hash") == current_hash
    )
    return {
        "draft": draft.model_dump(mode="json") if draft else None,
        "simulation_ready": bool(matches),
        "active": bool(matches and state == "ARMED"),
        "active_version": draft.version
        if draft is not None and matches and state == "ARMED"
        else None,
    }


def _trading_settings_response(
    draft: TradingSettingsDraft | None, snapshot: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    components = snapshot.get("components")
    engine_box = components.get("engine") if isinstance(components, dict) else None
    engine = engine_box.get("data") if isinstance(engine_box, dict) else None
    paper = engine.get("paper_session") if isinstance(engine, dict) else None
    broker = engine.get("broker") if isinstance(engine, dict) else None
    installed = engine.get("installed_limits") if isinstance(engine, dict) else None
    state = engine.get("state") if isinstance(engine, dict) else None
    current_hash = (
        digest(canonical_bytes(draft.settings.sell.model_dump(mode="json"))) if draft else None
    )
    matches = (
        isinstance(paper, dict)
        and paper.get("phase") == "EXIT"
        and paper.get("rule_id") == "COST_EXIT_V1"
        and paper.get("sell_rules_hash") == current_hash
    )
    live_matches = bool(
        draft
        and broker == "QMT_TRADING"
        and isinstance(installed, dict)
        and installed.get("mode") == "LIVE"
        and installed.get("max_buy_value") == draft.settings.buy.max_daily_buy_value
        and installed.get("max_order_value") == draft.settings.buy.max_order_value
        and installed.get("max_positions") == draft.settings.safety.max_positions
    )
    return {
        "draft": draft.model_dump(mode="json") if draft else None,
        "buy_configured": bool(draft and draft.settings.buy.enabled),
        "simulation_ready": bool(matches),
        "live_ready": live_matches,
        "restart_required": bool(draft and broker == "QMT_TRADING" and not live_matches),
        "active": bool((matches or live_matches) and state == "ARMED"),
        "active_version": draft.version
        if draft is not None and (matches or live_matches) and state == "ARMED"
        else None,
    }


async def _body(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise IPCError("JSON_REQUIRED")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 8192:
            raise IPCError("REQUEST_TOO_LARGE")
        raw.extend(chunk)
    parse_bytes(bytes(raw), max_bytes=8192)  # Also rejects duplicate keys/non-finite JSON.
    return bytes(raw)


def _session(request: Request, *, restore: bool = False) -> dict[str, JsonValue]:
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf = (
        request.cookies.get(CSRF_COOKIE, "") if restore else request.headers.get("x-csrf-token", "")
    )
    if not token or not csrf or len(token) > 128 or len(csrf) > 128:
        raise IPCError("OPERATOR_SESSION_REQUIRED")
    return {"session_token": token, "csrf_token": csrf}


def _error(code: str) -> JSONResponse:
    # Accept protocol codes only; validation text may contain the submitted password.
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) is None:
        code = "CONSOLE_REQUEST_FAILED"
    status = 409
    if code in {
        "OPERATOR_SESSION_REQUIRED",
        "OPERATOR_SESSION_EXPIRED",
        "OPERATOR_LOGIN_FAILED",
        "OPERATOR_SESSION_INVALID",
    }:
        status = 401
    elif "CSRF" in code or code in {"ORIGIN_DENIED", "HOST_DENIED", "FETCH_SITE_DENIED"}:
        status = 403
    elif code in {"CONSOLE_BUSY", "OPERATOR_LOGIN_RATE_LIMITED", "OPERATOR_SESSION_CAPACITY"}:
        status = 429
    elif (
        code.startswith("PIPE_")
        or code == "CONSOLE_REQUEST_FAILED"
        or code
        in {
            "RULE_STORE_INVALID",
            "RULE_SAVE_FAILED",
            "TRADING_SETTINGS_STORE_INVALID",
            "TRADING_SETTINGS_SAVE_FAILED",
        }
    ):
        status = 503
    elif code in {"INVALID_ARGUMENTS", "JSON_REQUIRED", "REQUEST_TOO_LARGE"}:
        status = 400
    return JSONResponse({"code": code}, status_code=status)


def _settings_validation_error(exc: ValidationError) -> JSONResponse:
    """Only fixed field descriptions cross HTTP; never echo submitted values."""
    descriptions = {
        "buy.max_daily_buy_value": "当日最多买入: 请填写非负金额，最多两位小数。",
        "buy.max_symbol_buy_value": "单只股票最多买入: 请填写非负金额，最多两位小数。",
        "buy.max_order_value": "单笔委托最多: 请填写非负金额，最多两位小数。",
        "buy.cancel_after_seconds": "未成交等待: 请填写1至300的整数秒数。",
        "buy.max_retries": "最多重新下单次数: 请填写0至3的整数。",
        "sell.take_profit_pct": "止盈涨幅: 请填写大于0、不超过100的百分比，最多两位小数。",
        "sell.stop_loss_pct": "止损跌幅: 请填写大于0、小于100的百分比，最多两位小数。",
        "sell.sell_by": "最晚卖出时间: 请选择09:30-11:25或13:00-14:50，精确到分钟。",
        "safety.max_daily_loss": "当日最大亏损: 请填写非负金额，最多两位小数。",
        "safety.max_positions": "最多持有股票数量: 请填写0至15的整数。",
        "advanced.max_order_quantity": "单笔最大股数: 请填写100至1000000的整数。",
        "advanced.max_chase_bps": "最大追价: 请填写0至1000的整数基点。",
        "advanced.max_spread_bps": "最大买卖价差: 请填写0至1000的整数基点。",
        "advanced.quote_max_age_ms": "行情有效期: 请填写100至5000的整数毫秒。",
    }
    custom = {
        "buy_limits_incomplete": (
            "buy",
            "三个买入金额必须全部大于0；关闭自动买入时，请将三个金额全部设为0。",
        ),
        "buy_limits_order": (
            "buy",
            "买入金额须满足: 单笔委托最多 ≤ 单只股票最多买入 ≤ 当日最多买入。",
        ),
        "buy_safety_required": (
            "safety",
            "启用自动买入时，当日最大亏损和最多持有股票数量都必须大于0。",
        ),
    }
    issues = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        path = list(error["loc"])
        if path and path[0] == "settings":
            path.pop(0)
        if path and path[0] == "rules":
            path[0] = "sell"
        field = ".".join(str(item) for item in path)
        if error["type"] in custom:
            field, message = custom[error["type"]]
        elif field == "sell":
            field, message = "sell.sell_by", descriptions["sell.sell_by"]
        else:
            message = descriptions.get(field, "请检查交易设置中的必填项和数值范围。")
        issue = {
            "field": field if field in descriptions or field in {"buy", "safety"} else "",
            "message": message,
        }
        if issue not in issues:
            issues.append(issue)
    return JSONResponse({"code": "INVALID_ARGUMENTS", "issues": issues[:20]}, status_code=400)


def _desktop_setup_url() -> str | None:
    """Return only the launcher-owned loopback setup endpoint."""
    value = os.environ.get("ATS_DESKTOP_SETUP_URL", "")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and parsed.path == "/"
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
        ):
            return value
    except ValueError:
        pass
    return None


def build_node_console_app(
    config: NodeProcessConfig,
    *,
    port: int = 8780,
    bridge: NodeConsoleBridge | None = None,
    asset_root: Path | None = None,
) -> FastAPI:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("console port must be between 1024 and 65535")
    assets = console_static_root() if asset_root is None else asset_root
    if not (assets / "node.html").is_file() or not (assets / "assets").is_dir():
        raise LocalConsoleAssetError(
            "CONSOLE_ASSETS_MISSING", "V2 console assets are not installed"
        )
    bridge = bridge or NodeConsoleBridge(config)
    drafts = SellRuleDraftStore(config.data_root)
    trading_drafts = TradingSettingsDraftStore(config.data_root)
    origin = f"http://127.0.0.1:{port}"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        bridge.status.start()
        try:
            yield
        finally:
            await run_in_threadpool(bridge.status.close)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.router.redirect_slashes = False

    @app.middleware("http")
    async def boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            if request.headers.get("host") != f"127.0.0.1:{port}":
                raise IPCError("HOST_DENIED")
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("origin") != origin:
                    raise IPCError("ORIGIN_DENIED")
                if request.headers.get("sec-fetch-site") != "same-origin":
                    raise IPCError("FETCH_SITE_DENIED")
            response = await call_next(request)
        except IPCError as exc:
            response = _error(str(exc))
        response.headers.update(
            {
                "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    @app.exception_handler(IPCError)
    async def ipc_error(_: Request, exc: IPCError) -> Response:
        return _error(str(exc))

    @app.exception_handler(ValueError)
    async def validation_error(_: Request, exc: ValueError) -> Response:
        return _error("INVALID_ARGUMENTS")

    @app.exception_handler(ValidationError)
    async def settings_validation_error(request: Request, exc: ValidationError) -> Response:
        if request.url.path in {
            PREFIX + "/trading-settings",
            PREFIX + "/sell-rules",
            PREFIX + "/sell-rules/test",
        }:
            return _settings_validation_error(exc)
        return _error("INVALID_ARGUMENTS")

    @app.exception_handler(SellRuleDraftError)
    async def rule_store_error(_: Request, exc: SellRuleDraftError) -> Response:
        return _error(exc.code)

    @app.exception_handler(TradingSettingsDraftError)
    async def trading_settings_store_error(_: Request, exc: TradingSettingsDraftError) -> Response:
        return _error(exc.code)

    async def invoke(
        channel: str, action: str, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        try:
            return await run_in_threadpool(bridge.invoke, channel, action, payload)
        except IPCError:
            raise
        except (OSError, RuntimeError):
            raise IPCError("CONSOLE_REQUEST_FAILED") from None

    @app.get(PREFIX + "/status")
    async def status() -> dict[str, JsonValue]:
        return bridge.status.snapshot()

    @app.get("/connect-qmt")
    async def connect_qmt() -> Response:
        target = _desktop_setup_url()
        if target is None:
            return JSONResponse({"code": "QMT_SETUP_UNAVAILABLE"}, status_code=503)
        return RedirectResponse(target, status_code=307)

    @app.post(PREFIX + "/session")
    async def login(request: Request) -> Response:
        value = LoginInput.model_validate_json(await _body(request))
        result = await invoke("authorization", "LOGIN", {"password": value.password})
        session, csrf = result.pop("session_token"), result["csrf_token"]
        if not isinstance(session, str) or not isinstance(csrf, str):
            raise IPCError("CONSOLE_REQUEST_FAILED")
        response = JSONResponse(result)
        for key, token in ((SESSION_COOKIE, session), (CSRF_COOKIE, csrf)):
            response.set_cookie(key, token, httponly=True, samesite="strict", path="/")
        return response

    @app.get(PREFIX + "/session")
    async def session(request: Request) -> dict[str, JsonValue]:
        proof = _session(request, restore=True)
        result = await invoke("authorization", "SESSION", proof)
        return {**result, "csrf_token": proof["csrf_token"]}

    @app.delete(PREFIX + "/session")
    async def logout(request: Request) -> Response:
        await invoke("authorization", "LOGOUT", _session(request))
        response = JSONResponse({"logged_out": True})
        for key in (SESSION_COOKIE, CSRF_COOKIE):
            response.delete_cookie(key, httponly=True, samesite="strict", path="/")
        return response

    @app.post(PREFIX + "/control")
    async def control(request: Request) -> dict[str, JsonValue]:
        value = ActionInput.model_validate_json(await _body(request))
        return await invoke("control", "OPERATOR_" + value.action, _session(request))

    @app.get(PREFIX + "/trades")
    async def trade_details(request: Request) -> dict[str, JsonValue]:
        return await invoke("query", "OPERATOR_DETAILS", _session(request, restore=True))

    @app.post(PREFIX + "/orders/cancel")
    async def cancel_order(request: Request) -> dict[str, JsonValue]:
        value = CancelOrderInput.model_validate_json(await _body(request))
        return await invoke(
            "control",
            "OPERATOR_CANCEL",
            {**_session(request), "client_order_id": value.client_order_id},
        )

    @app.get(PREFIX + "/emergency-liquidation")
    async def liquidation_preview(request: Request) -> dict[str, JsonValue]:
        return await invoke(
            "control", "OPERATOR_LIQUIDATION_PREVIEW", _session(request, restore=True)
        )

    @app.post(PREFIX + "/emergency-liquidation")
    async def emergency_liquidation(request: Request) -> dict[str, JsonValue]:
        value = LiquidationInput.model_validate_json(await _body(request))
        return await invoke(
            "control",
            "OPERATOR_LIQUIDATE",
            {**_session(request), **value.model_dump(mode="json")},
        )

    @app.post(PREFIX + "/preview")
    async def preview(request: Request) -> dict[str, JsonValue]:
        value = PreviewInput.model_validate_json(await _body(request))
        proof = _session(request)
        await invoke("authorization", "SESSION", proof)
        if value.action in {"ARM", "RESUME"}:
            settings = await run_in_threadpool(trading_drafts.read)
            if settings is None:
                raise IPCError("TRADING_SETTINGS_REQUIRED")
        proposal: dict[str, JsonValue] = {"action": value.action}
        if value.action != "CONFIRM_RECOVERY":
            # The browser cannot supply trading limits.  For an installed paper
            # session the trusted local config determines phase and ceilings.
            proposal["grant"] = _grant_for(config, settings, bridge.status.snapshot())
        return await invoke("authorization", "PREVIEW_CONTROL", {**proof, **proposal})

    @app.post(PREFIX + "/confirm")
    async def confirm(request: Request) -> dict[str, JsonValue]:
        value = ConfirmInput.model_validate_json(await _body(request))
        return await invoke(
            "authorization",
            "CONFIRM_CONTROL",
            {**_session(request), **value.model_dump(mode="json")},
        )

    @app.post(PREFIX + "/result")
    async def result(request: Request) -> dict[str, JsonValue]:
        value = ResultInput.model_validate_json(await _body(request))
        return await invoke(
            "authorization",
            "CONTROL_RESULT",
            {**_session(request), **value.model_dump(mode="json")},
        )

    @app.get(PREFIX + "/sell-rules")
    async def sell_rules(request: Request) -> dict[str, JsonValue]:
        await invoke("authorization", "SESSION", _session(request, restore=True))
        current = await run_in_threadpool(drafts.read)
        return _rule_response(current, bridge.status.snapshot())

    @app.post(PREFIX + "/sell-rules")
    async def save_sell_rules(request: Request) -> dict[str, JsonValue]:
        proof = _session(request)
        await invoke("authorization", "SESSION", proof)
        value = SaveSellRulesInput.model_validate_json(await _body(request))
        current = await run_in_threadpool(
            drafts.save, value.rules, expected_version=value.expected_version
        )
        return _rule_response(current, bridge.status.snapshot())

    @app.post(PREFIX + "/sell-rules/test")
    async def test_sell_rules(request: Request) -> dict[str, JsonValue]:
        await invoke("authorization", "SESSION", _session(request))
        value = TestSellRulesInput.model_validate_json(await _body(request))
        if value.observed_at.utcoffset() != timedelta(hours=8):
            raise ValueError("test time requires China market offset")
        today = value.observed_at.date()
        lot = ExitLot(
            "preview-lot",
            100,
            date.min,
            today,
            Price.parse(str(value.buy_cost)),
            Price.parse(str(value.buy_cost)),
            Price.parse(str(value.buy_cost)),
        )
        result = evaluate_exit(
            lot,
            ExitState(100),
            ExitObservation(
                value.observed_at, 1, Price.parse(str(value.current_price)), True, True
            ),
            value.rules,
        )
        return {
            "reasons": list(result.reasons),
            "should_sell": result.state.target_remaining == 0,
            "change_pct": str((value.current_price / value.buy_cost - 1) * 100),
            "actual_order_sent": False,
        }

    @app.get(PREFIX + "/trading-settings")
    async def trading_settings(request: Request) -> dict[str, JsonValue]:
        await invoke("authorization", "SESSION", _session(request, restore=True))
        current = await run_in_threadpool(trading_drafts.read)
        return _trading_settings_response(current, bridge.status.snapshot())

    @app.post(PREFIX + "/trading-settings")
    async def save_trading_settings(request: Request) -> dict[str, JsonValue]:
        await invoke("authorization", "SESSION", _session(request))
        value = SaveTradingSettingsInput.model_validate_json(await _body(request))
        current = await run_in_threadpool(
            trading_drafts.save,
            value.settings,
            expected_version=value.expected_version,
        )
        return _trading_settings_response(current, bridge.status.snapshot())

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(assets / "node.html")

    app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")
    return app


def run_node_console(config: NodeProcessConfig, *, port: int) -> None:
    """The console owns only its own mutex; exit never stops engine or agent."""
    identity = hashlib.sha256(("console:" + config.account_id.upper()).encode()).hexdigest()
    with AccountInstanceGuard(identity, backend=WindowsNamedMutexBackend()):
        app = build_node_console_app(config, port=port)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            server_header=False,
            proxy_headers=False,
            limit_concurrency=32,
        )
