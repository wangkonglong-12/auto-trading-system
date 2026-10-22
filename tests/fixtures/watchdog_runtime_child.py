"""Controlled subprocess used by watchdog recovery integration tests."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_trading_system.domain.trading_time import ShanghaiTimestamp
from auto_trading_system.runtime import RuntimeHeartbeat, RuntimeHeartbeatStore, RuntimeState


def main() -> int:
    storage_root = Path(sys.argv[1])
    launch_log = Path(sys.argv[2])
    existing = launch_log.read_text(encoding="utf-8").splitlines() if launch_log.exists() else []
    launch_number = len(existing) + 1
    with launch_log.open("a", encoding="utf-8") as handle:
        handle.write(f"{os.environ.get('ATS_WATCHDOG_FORCE_HALT', '0')}\n")
        handle.flush()

    store = RuntimeHeartbeatStore.from_storage_root(
        storage_root,
        node_id="node-dev-01",
        account_id="acct_demo_001",
    )
    boot_id = f"boot_{launch_number:032x}"
    sequence = 0
    while True:
        sequence += 1
        store.write(
            RuntimeHeartbeat(
                node_id="node-dev-01",
                account_id="acct_demo_001",
                boot_id=boot_id,
                process_id=os.getpid(),
                sequence=sequence,
                runtime_state=RuntimeState.HALT
                if os.environ.get("ATS_WATCHDOG_FORCE_HALT") == "1"
                else RuntimeState.READY_PAUSED,
                written_at=ShanghaiTimestamp(datetime.now(tz=ZoneInfo("Asia/Shanghai"))),
            )
        )
        if launch_number == 1:
            return 17
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
