"""Operator-authenticated proxy to the configured quant history API only."""

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.config.quant_access import read_access_token

PATH = "/api/v2/node/history"
MAX_BODY = 16 * 1024 * 1024


def register_history(
    app: FastAPI,
    config: NodeProcessConfig,
    authorize: Callable[[Request, bool], Awaitable[None]],
) -> None:
    async def remote(
        token: bytes,
        params: dict[str, str],
    ) -> httpx.Response | None:
        origin = urlsplit(config.quant_origin)
        if origin.scheme not in {"http", "https"} or origin.username or origin.password:
            return None
        try:
            async with (
                asyncio.timeout(14),
                httpx.AsyncClient(
                    verify=ssl.create_default_context(
                        cafile=str(config.quant_ca_file) if config.quant_ca_file else None
                    ),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(12, connect=3),
                ) as client,
                client.stream(
                    "GET",
                    config.quant_origin.rstrip("/") + "/api/auto-trading/execution-history",
                    headers={"Authorization": "Bearer " + token.decode("ascii")},
                    params=params,
                ) as reply,
            ):
                output = bytearray()
                async for chunk in reply.aiter_bytes():
                    if len(output) + len(chunk) > MAX_BODY:
                        return None
                    output.extend(chunk)
                return httpx.Response(reply.status_code, content=bytes(output))
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            return None

    def failure(code: str, status: int = 503) -> JSONResponse:
        return JSONResponse({"code": code}, status_code=status)

    @app.get(PATH)
    async def listing(request: Request) -> Response:
        await authorize(request, True)
        allowed = {
            "symbol",
            "buy_from",
            "buy_to",
            "sell_from",
            "sell_to",
            "offset",
            "limit",
            "export",
        }
        if any(k not in allowed for k in request.query_params):
            return failure("INVALID_ARGUMENTS", 400)
        params = dict(request.query_params)
        params.update(account_id=config.account_id, strategy_id=config.strategy_id)
        try:
            token = await run_in_threadpool(read_access_token, config.data_root)
        except SecretLoadError:
            return failure("HISTORY_ACCESS_REJECTED")
        if token is None:
            return failure("QUANT_ACCESS_REQUIRED")
        reply = await remote(token, params)
        if reply is None:
            return failure("HISTORY_UNAVAILABLE")
        if reply.status_code in {401, 403}:
            return failure("HISTORY_ACCESS_REJECTED")
        if reply.status_code != 200:
            try:
                code = reply.json().get("error", {}).get("code")
            except (ValueError, AttributeError):
                code = None
            return failure(
                code
                if code in {"EXPORT_TOO_LARGE", "INVALID_DATE_RANGE"}
                else "HISTORY_UNAVAILABLE",
                400,
            )
        exporting = params.get("export") == "true"
        return Response(
            reply.content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if exporting
            else "application/json",
            headers={"Content-Disposition": 'attachment; filename="trade-history.xlsx"'}
            if exporting
            else {},
        )
