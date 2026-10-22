"""FastAPI composition for packaged operator-console assets on loopback only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.control import LocalControlSubmissionService

from .control_api import build_local_control_router
from .security_api import LocalConsoleSecurityController, build_local_console_security_router

_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self'",
    )
)


class LocalConsoleAssetError(RuntimeError):
    """Stable startup error that omits installation paths."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def console_static_root() -> Path:
    """Return the wheel-packaged Vite output directory."""
    return (Path(__file__).resolve().parent / "static").resolve()


def _validated_asset_root(asset_root: Path) -> Path:
    if not isinstance(asset_root, Path) or not asset_root.is_absolute() or asset_root.is_symlink():
        raise LocalConsoleAssetError(
            "CONSOLE_ASSETS_INVALID",
            "operator console asset root is invalid",
        )
    try:
        resolved = asset_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalConsoleAssetError(
            "CONSOLE_ASSETS_MISSING",
            "operator console assets are not installed",
        ) from exc
    if not resolved.is_dir() or not (resolved / "index.html").is_file():
        raise LocalConsoleAssetError(
            "CONSOLE_ASSETS_MISSING",
            "operator console assets are not installed",
        )
    return resolved


def build_local_console_app(
    settings: RuntimeConfig,
    *,
    asset_root: Path | None = None,
    control_service: LocalControlSubmissionService | None = None,
    console_security: LocalConsoleSecurityController | None = None,
) -> FastAPI:
    """Build a same-origin static console with a strict loopback Host boundary."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    if settings.local_api.host != "127.0.0.1":
        raise ValueError("operator console must bind to IPv4 loopback")
    assets = _validated_asset_root(console_static_root() if asset_root is None else asset_root)
    app = FastAPI(
        title="Auto Trading Local Operator Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.router.redirect_slashes = False
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
    if (control_service is None) is not (console_security is None):
        raise ValueError("control_service and console_security must be configured together")
    if control_service is not None and console_security is not None:
        app.include_router(build_local_console_security_router(console_security))
        app.include_router(build_local_control_router(control_service, console_security))

    @app.middleware("http")
    async def add_console_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if request.url.path.startswith("/assets/")
            else "no-store"
        )
        return response

    app.mount("/", StaticFiles(directory=assets, html=True, check_dir=True), name="console")
    return app


def build_local_console_uvicorn_config(settings: RuntimeConfig, app: FastAPI) -> uvicorn.Config:
    """Freeze the server bind and proxy behavior for the local application."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    if settings.local_api.host != "127.0.0.1":
        raise ValueError("operator console must bind to IPv4 loopback")
    return uvicorn.Config(
        app,
        host="127.0.0.1",
        port=settings.local_api.port,
        loop="asyncio",
        lifespan="on",
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )
