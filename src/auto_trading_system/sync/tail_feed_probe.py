"""Read-only feed diagnostics. No engine, account, journal or order connection."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from auto_trading_system.protocol.tail_picks import CN, TailPicksPreview
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.sync.tail_feed import TailFeedHttp


def inspect_preview(body: bytes, now: datetime) -> dict[str, object]:
    preview = TailPicksPreview.model_validate_json(
        canonical_bytes(parse_bytes(body, max_bytes=65536))
    )
    preview.check_current(now)
    counts = Counter(s.code for b in preview.batches for s in b.stocks)
    return {
        "mode": "TEST",
        "executable": False,
        "status": preview.status,
        "requested_date": preview.requested_date.isoformat(),
        "trade_date": preview.trade_date.isoformat() if preview.trade_date else None,
        "lookback_days": preview.lookback_days,
        "unique_stock_count": len(counts),
        "repeated_across_phases": sorted(code for code, count in counts.items() if count > 1),
        "batches": [
            {
                "phase": b.phase,
                "status": b.status,
                "error_code": b.error_code,
                "stock_count": len(b.stocks),
                "stocks": [s.model_dump() for s in b.stocks],
            }
            for b in preview.batches
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查尾盘名单，历史数据不进入交易引擎")
    parser.add_argument("--origin", help="量化后台HTTPS源地址；不填接口路径")
    parser.add_argument("--ca-file")
    parser.add_argument("--token-env", default="TAIL_PICKS_TEST_TOKEN")
    parser.add_argument("--input", type=Path, help="离线校验已取得的测试响应")
    parser.add_argument("--output", type=Path, required=True, help="新建诊断报告，不覆盖已有文件")
    args = parser.parse_args()
    if bool(args.origin) == bool(args.input):
        parser.error("--origin和--input必须二选一")
    now = datetime.now(CN)
    client = None
    try:
        if args.input:
            with args.input.open("rb") as source:
                body = source.read(65537)
        else:
            token = os.environ.get(args.token_env, "").encode("ascii")
            client = TailFeedHttp(args.origin, token, ca_file=args.ca_file)
            body = client.get_preview(now)
        report = inspect_preview(body, datetime.now(CN))
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered)
        print(rendered, end="")
        return 0
    except (ValueError, OSError):
        # Do not print transport exceptions or configuration values containing credentials.
        print("名单检查失败: 请核对接口状态、专用令牌、响应格式及报告路径。")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
