"""Build a per-file reuse map from the preserved pre-change hash inventories."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".codex_tmp/r31-20260907"


def disposition(name: str, quant: bool) -> str:
    if quant:
        return "保留并分包复验" if "auto_trading" in name else "原量化文件保护范围"
    if name.startswith("tests/") or name.startswith("contracts/v1/"):
        return "保留旧版回归证据"
    if name.startswith("src/auto_trading_system/journal/") or "/sync/outbox" in name:
        return "保留旧调用链 待新版端到端接线完成后逐文件替换"
    if "/gateways/" in name or "/security/" in name or "/domain/" in name:
        return "复用并执行回归 现场能力另行验收"
    return "保留 按R31工作包独立核对"


def main() -> None:
    result: dict[str, object] = {"baseline": "V3.1", "date": "2026-09-07"}
    for name, quant in (("windows", False), ("quant", True)):
        baseline = json.loads((BASELINE / f"{name}-before.json").read_text(encoding="utf-8"))
        result[name] = {
            "root": baseline["root"],
            "files": [
                {"path": path, "sha256": value, "disposition": disposition(path, quant)}
                for path, value in baseline["files"].items()
            ],
        }
    migration = (
        ROOT.parent
        / "ai-stock-assistant/backend/scripts/auto_trading/migrations"
        / "20260904_auto_trade_protocol_v1.sql"
    )
    result["existing_quant_tables"] = re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+)",
        migration.read_text(encoding="utf-8"),
    )
    result["retained_feature_defaults"] = {
        "AUTO_TRADE_MODULE_ENABLED": False,
        "AUTO_TRADE_PLAN_PUBLISHER_ENABLED": False,
        "AUTO_TRADE_EVENT_PROJECTOR_ENABLED": False,
        "AUTO_TRADE_MONITOR_ENABLED": False,
        "VITE_AUTO_TRADE_MODULE_ENABLED": False,
    }
    path = ROOT / "evidence/r31/20260907/reuse-map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Per-file baseline and reuse map: {path}")


if __name__ == "__main__":
    main()
