from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI

from auto_trading_system.apps.local_console import LocalConsoleServer, LocalConsoleServerStatus


def assert_server_status(server: LocalConsoleServer, expected: LocalConsoleServerStatus) -> None:
    assert server.status is expected


async def exercise_real_uvicorn_lifecycle() -> None:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        log_level="critical",
    )
    server = LocalConsoleServer(config, startup_timeout_seconds=2, shutdown_timeout_seconds=2)

    await server.start()
    assert_server_status(server, LocalConsoleServerStatus.RUNNING)
    await server.stop()
    assert_server_status(server, LocalConsoleServerStatus.STOPPED)


def test_real_uvicorn_server_starts_and_stops_on_ipv4_loopback() -> None:
    asyncio.run(exercise_real_uvicorn_lifecycle())
