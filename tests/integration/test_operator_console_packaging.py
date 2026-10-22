from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from auto_trading_system.apps.local_console import build_local_console_app, console_static_root
from auto_trading_system.config.models import RuntimeConfig

_ASSET_REFERENCE = re.compile(r"""(?:src|href)=["'](?P<path>/assets/[^"']+)["']""")


def _runtime_settings() -> RuntimeConfig:
    config_path = (
        Path(__file__).resolve().parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    )
    return RuntimeConfig.model_validate(json.loads(config_path.read_text(encoding="utf-8")))


def test_packaged_console_entrypoint_and_all_referenced_assets_are_served() -> None:
    static_root = console_static_root()
    assert static_root.is_dir()
    assert not list(static_root.rglob("*.map"))

    client = TestClient(
        build_local_console_app(_runtime_settings()),
        base_url="http://127.0.0.1",
    )
    index = client.get("/")

    assert index.status_code == 200
    assert "<title>交易节点操作台</title>" in index.text
    assert index.headers["cache-control"] == "no-store"
    referenced_assets = sorted(
        match.group("path") for match in _ASSET_REFERENCE.finditer(index.text)
    )
    assert referenced_assets
    packaged_assets = sorted(
        f"/{path.relative_to(static_root).as_posix()}"
        for path in (static_root / "assets").iterdir()
        if path.is_file()
    )
    assert set(referenced_assets).issubset(packaged_assets)

    for asset_path in packaged_assets:
        asset = client.get(asset_path)
        assert asset.status_code == 200, asset_path
        assert asset.content
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert asset.headers["content-security-policy"].startswith("default-src 'self'")


def test_packaged_console_preserves_hash_routing_and_rejects_remote_hosts() -> None:
    client = TestClient(
        build_local_console_app(_runtime_settings()),
        base_url="http://127.0.0.1",
    )

    assert client.get("/#/orders").status_code == 200
    assert client.get("/", headers={"host": "192.0.2.10"}).status_code == 400
