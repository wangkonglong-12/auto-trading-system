from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_trading_system.apps.local_console import (
    LocalConsoleAssetError,
    build_local_console_app,
    build_local_console_uvicorn_config,
)
from auto_trading_system.config.models import RuntimeConfig


def runtime_settings() -> RuntimeConfig:
    path = Path(__file__).resolve().parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    return RuntimeConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def asset_root(tmp_path: Path) -> Path:
    root = tmp_path / "console-assets"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><body><div id="app"></div></body></html>',
        encoding="utf-8",
    )
    (root / "assets" / "app-abc123.js").write_text("export {};", encoding="utf-8")
    return root.resolve()


def test_console_serves_index_and_hashed_assets_with_security_headers(tmp_path: Path) -> None:
    app = build_local_console_app(runtime_settings(), asset_root=asset_root(tmp_path))
    client = TestClient(app, base_url="http://127.0.0.1")

    index = client.get("/")
    asset = client.get("/assets/app-abc123.js")

    assert index.status_code == 200
    assert '<html lang="zh-CN">' in index.text
    assert index.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in index.headers["content-security-policy"]
    assert index.headers["x-frame-options"] == "DENY"
    assert index.headers["x-content-type-options"] == "nosniff"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_console_rejects_untrusted_host_and_unknown_api(tmp_path: Path) -> None:
    app = build_local_console_app(runtime_settings(), asset_root=asset_root(tmp_path))
    client = TestClient(app, base_url="http://127.0.0.1")

    assert client.get("/", headers={"host": "remote.example"}).status_code == 400
    assert client.get("/api/v1/local/runtime").status_code == 404


def test_console_requires_a_physical_index_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(LocalConsoleAssetError, match="CONSOLE_ASSETS_MISSING"):
        build_local_console_app(runtime_settings(), asset_root=missing.resolve())
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(LocalConsoleAssetError, match="CONSOLE_ASSETS_MISSING"):
        build_local_console_app(runtime_settings(), asset_root=root.resolve())


def test_uvicorn_configuration_is_loopback_only_and_proxy_headers_are_disabled(
    tmp_path: Path,
) -> None:
    settings = runtime_settings()
    app = build_local_console_app(settings, asset_root=asset_root(tmp_path))

    config = build_local_console_uvicorn_config(settings, app)

    assert config.host == "127.0.0.1"
    assert config.port == settings.local_api.port
    assert config.access_log is False
    assert config.proxy_headers is False
    assert config.server_header is False
