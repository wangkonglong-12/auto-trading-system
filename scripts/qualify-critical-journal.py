"""Measure isolated developer-machine J1 round-trip latency; never a live-readiness approval."""

from __future__ import annotations

import argparse
import json
import platform
from datetime import date
from pathlib import Path
from statistics import median
from time import monotonic, perf_counter_ns
from uuid import uuid4

import apsw

from auto_trading_system.journal.critical_models import ActionTarget, Submission
from auto_trading_system.journal.critical_writer import open_critical_journal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--samples", type=int, default=200)
    args = parser.parse_args()
    if not 10 <= args.samples <= 2000:
        parser.error("samples must be 10..2000")
    directory = args.output_directory.resolve() / ("journal-qualification-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    owner = open_critical_journal(directory / "critical.db", "isolated-test-account")
    samples: list[float] = []
    try:
        for number in range(args.samples):
            item = Submission(
                client_order_id=f"ord_qualification{number:06}",
                account_id="isolated-test-account",
                trade_date=date(2026, 9, 7),
                action_type="NEW",
                plan_hash="sha256:" + "a" * 64,
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                limit_price="10",
                maximum_amount="1005",
                remark_token=f"Q{number:06}",
                engine_boot_id="isolated-test-boot",
                permission_epoch=1,
                target=ActionTarget(
                    action_id="test-action", target_remaining=100, rule_id="test-rule"
                ),
            )
            start = perf_counter_ns()
            result = owner.commit(item, deadline=monotonic() + 2).result(timeout=3)
            assert result.fresh
            samples.append((perf_counter_ns() - start) / 1_000_000)
    finally:
        assert owner.close()
    samples.sort()
    report = {
        "environment": "DEVELOPER_ISOLATION_ONLY",
        "python": platform.python_version(),
        "apsw": apsw.apswversion(),
        "sqlite": apsw.sqlitelibversion(),
        "samples": args.samples,
        "metric": "owner queue plus J1 plus receipt round-trip milliseconds",
        "p50_ms": round(median(samples), 3),
        "p95_ms": round(samples[int(0.95 * (len(samples) - 1))], 3),
        "p99_ms": round(samples[int(0.99 * (len(samples) - 1))], 3),
        "maximum_ms": round(max(samples), 3),
        "target_qmt_storage_qualified": False,
        "caveat": (
            "This test does not simulate physical power loss, "
            "disk firmware stalls or live QMT load."
        ),
    }
    with (directory / "report.json").open("x", encoding="utf-8") as target:
        json.dump(report, target, indent=2)
    print(json.dumps({"report_path": str(directory / "report.json"), **report}))


if __name__ == "__main__":
    main()
