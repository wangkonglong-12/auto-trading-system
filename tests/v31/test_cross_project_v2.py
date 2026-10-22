from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "base.py",
        "canonical.py",
        "daily_sync.py",
        "authentication.py",
        "records.py",
        "plans.py",
        "api.py",
        "openapi.py",
    ],
)
def test_v2_transport_sources_do_not_drift_between_projects(name: str) -> None:
    root = Path(__file__).resolve().parents[2]
    quant = root.parent / "ai-stock-assistant/backend/app/protocol/auto_trading/v2" / name
    if not quant.exists():
        pytest.skip("peer quant repository is not present in this checkout")
    windows = root / "src/auto_trading_system/protocol/v2" / name
    normalized = quant.read_text(encoding="utf-8").replace(
        "app.protocol.auto_trading.v2",
        "auto_trading_system.protocol.v2",
    )
    assert ast.dump(ast.parse(normalized)) == ast.dump(
        ast.parse(windows.read_text(encoding="utf-8"))
    )
