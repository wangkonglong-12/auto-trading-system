"""Separate configuration persistence and authenticated board-engine controls."""

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.strategies.board_sell_configuration import (
    BoardSellConfiguration,
    BoardSellConfigurationError,
    BoardSellConfigurationStore,
    BoardSellDraft,
)


class SaveBoardSellConfiguration(WireModel):
    expected_version: int = Field(ge=0)
    settings: BoardSellConfiguration


def register_board_sell_configuration(
    app: FastAPI,
    store: BoardSellConfigurationStore,
    authorize: Callable[[Request, bool], Awaitable[None]],
    invoke: Callable[[str, str, dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]
    | None = None,
    session_proof: Callable[[Request], dict[str, JsonValue]] | None = None,
) -> None:
    def response(draft: BoardSellDraft | None) -> JSONResponse:
        return JSONResponse(
            {
                "draft": draft.model_dump(mode="json") if draft else None,
                "defaults": BoardSellConfiguration().model_dump(mode="json"),
                "save_enables_execution": False,
                "execution_status_path": "/api/v2/node/sell-engines",
                "execution_supported": True,
            }
        )

    @app.exception_handler(BoardSellConfigurationError)
    async def failure(_: Request, error: BoardSellConfigurationError) -> JSONResponse:
        code = str(error)
        return JSONResponse(
            {"code": code}, status_code=409 if code.endswith("VERSION_CONFLICT") else 503
        )

    @app.get("/api/v2/node/sell-configuration")
    async def read(request: Request) -> JSONResponse:
        await authorize(request, True)
        return response(await run_in_threadpool(store.read))

    @app.post("/api/v2/node/sell-configuration")
    async def save(request: Request) -> JSONResponse:
        await authorize(request, False)
        body = await request.body()
        if len(body) > 8192:
            return JSONResponse({"code": "INVALID_ARGUMENTS"}, status_code=400)
        value = SaveBoardSellConfiguration.model_validate_json(body)
        return response(await run_in_threadpool(store.save, value.settings, value.expected_version))

    if invoke is not None and session_proof is not None:

        class BoardInput(WireModel):
            board: Literal["MAIN", "CHINEXT"]

        class ConfirmInput(WireModel):
            preview_id: str = Field(pattern=r"^board-sell-[a-f0-9]{32}$")
            phrase: str = Field(min_length=1, max_length=128)

        @app.get("/api/v2/node/sell-engines")
        async def engines(request: Request) -> dict[str, JsonValue]:
            await authorize(request, True)
            return await invoke("authorization", "BOARD_SELL_STATUS", session_proof(request))

        @app.post("/api/v2/node/sell-engines/{action}", response_model=None)
        async def control(action: str, request: Request) -> dict[str, JsonValue] | JSONResponse:
            actions = {
                "start": "START_BOARD_SELL",
                "prepare": "PREPARE_BOARD_SELL",
                "preview": "PREVIEW_BOARD_SELL",
                "confirm": "CONFIRM_BOARD_SELL",
                "pause": "PAUSE_BOARD_SELL",
            }
            await authorize(request, False)
            body = await request.body()
            if action not in actions or len(body) > 8192:
                return JSONResponse({"code": "INVALID_ARGUMENTS"}, status_code=400)
            value = (
                ConfirmInput.model_validate_json(body)
                if action == "confirm"
                else BoardInput.model_validate_json(body)
            )
            return await invoke(
                "authorization",
                actions[action],
                {**session_proof(request), **value.model_dump(mode="json")},
            )
