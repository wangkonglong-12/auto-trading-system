"""Export/check the public V2 process config schema without resolving credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from auto_trading_system.config.node_v2 import NodeProcessConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "config/v2/node-config.schema.json"
    data = json.dumps(
        NodeProcessConfig.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    if args.check:
        if path.read_text(encoding="utf-8") != data:
            raise SystemExit("V2 node config schema drift")
    else:
        path.write_text(data, encoding="utf-8")


if __name__ == "__main__":
    main()
