"""Loopback-only operator console application and static asset host."""

from auto_trading_system.apps.local_console.app import (
    LocalConsoleAssetError,
    build_local_console_app,
    build_local_console_uvicorn_config,
    console_static_root,
)
from auto_trading_system.apps.local_console.composition import (
    LocalControlApiRuntime,
    build_local_control_api_runtime,
)
from auto_trading_system.apps.local_console.control_api import (
    LocalControlCommandBody,
    LocalControlConfirmationBody,
    LocalControlReceiptResponse,
    build_local_control_router,
)
from auto_trading_system.apps.local_console.security_api import (
    CONTROL_AUTHORIZATION_HEADER_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    LocalConsoleSecurityController,
    build_local_console_security_router,
)
from auto_trading_system.apps.local_console.server import (
    LocalConsoleServer,
    LocalConsoleServerError,
    LocalConsoleServerStatus,
    UvicornServer,
)

__all__ = [
    "CONTROL_AUTHORIZATION_HEADER_NAME",
    "CSRF_HEADER_NAME",
    "SESSION_COOKIE_NAME",
    "LocalConsoleAssetError",
    "LocalConsoleSecurityController",
    "LocalConsoleServer",
    "LocalConsoleServerError",
    "LocalConsoleServerStatus",
    "LocalControlApiRuntime",
    "LocalControlCommandBody",
    "LocalControlConfirmationBody",
    "LocalControlReceiptResponse",
    "UvicornServer",
    "build_local_console_app",
    "build_local_console_security_router",
    "build_local_console_uvicorn_config",
    "build_local_control_api_runtime",
    "build_local_control_router",
    "console_static_root",
]
