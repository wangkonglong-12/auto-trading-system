from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_single_entry_runs_offline_without_qmt_or_production_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "auto_trading_system.apps.trade_runtime",
            "--simulate",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    report = json.loads(result.stdout)
    assert report["mode"] == "SIMULATION"
    assert report["submission_records"] == 2
    assert report["filled_quantity"] == 50
    assert report["daily_upload_status"] == "SYNCED"
    assert report["restart_paused"] and not report["real_trading_enabled"]
    assert Path(report["output_directory"]).is_relative_to(tmp_path)
