from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "diagnose-environment.ps1"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _walk_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _run_diagnostic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            str(SCRIPT),
            *arguments,
        ),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell is required")
def test_environment_diagnostic_generates_bounded_sanitized_inventory(tmp_path: Path) -> None:
    qmt_root = tmp_path / "MiniQMT"
    qmt_binary = qmt_root / "bin" / "MiniQMT.exe"
    xtquant_module = qmt_root / "python" / "site-packages" / "xtquant" / "__init__.py"
    function_queue = qmt_root / "userdata_mini" / "up_queue_xtquant"
    qmt_binary.parent.mkdir(parents=True)
    xtquant_module.parent.mkdir(parents=True)
    function_queue.mkdir(parents=True)
    qmt_binary.write_bytes(b"diagnostic fixture only")
    xtquant_module.write_text("__version__ = 'fixture'\n", encoding="utf-8")
    output_path = tmp_path / "inventory.json"
    output_path.write_text("stale inventory", encoding="utf-8")

    result = _run_diagnostic(
        "-Python",
        sys.executable,
        "-QmtRoot",
        str(qmt_root),
        "-UserdataMini",
        str(qmt_root / "userdata_mini"),
        "-OutputPath",
        str(output_path),
    )

    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)
    assert json.loads(output_path.read_text(encoding="utf-8")) == inventory
    assert inventory["schema"] == "qmt-environment-inventory/v1"
    assert inventory["script_revision"] == "1.0.0"
    assert inventory["collection_mode"] == "READ_ONLY"
    assert re.fullmatch(r"[0-9a-f]{16}", inventory["host_fingerprint"])

    executable_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    python_fact = next(item for item in inventory["python"] if item["source"] == "explicit")
    assert python_fact["available"] is True
    assert python_fact["version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert python_fact["architecture_bits"] == 64
    assert python_fact["sha256"] == executable_hash

    qmt_fact = next(item for item in inventory["qmt_installations"] if item["source"] == "explicit")
    assert qmt_fact["exists"] is True
    assert qmt_fact["scan_truncated"] is False
    assert qmt_fact["visited_directory_count"] <= 300
    assert qmt_fact["artifacts"][0]["sha256"] == hashlib.sha256(qmt_binary.read_bytes()).hexdigest()
    assert qmt_fact["xtquant_directories"][0]["sampled_file_count"] == 1

    assert inventory["checks"]["qmt_installation_found"] is True
    assert inventory["checks"]["xtquant_found"] is True
    assert inventory["checks"]["userdata_mini_found"] is True
    assert inventory["checks"]["function_trading_queue_found"] is True
    assert inventory["technical_ready"] == all(inventory["checks"].values())
    expected_blockers = {
        "PYTHON_3_11_X64_NOT_FOUND": "python_3_11_x64_found",
        "QMT_INSTALLATION_NOT_FOUND": "qmt_installation_found",
        "XTQUANT_NOT_FOUND": "xtquant_found",
        "USERDATA_MINI_NOT_FOUND": "userdata_mini_found",
        "FUNCTION_TRADING_QUEUE_NOT_FOUND": "function_trading_queue_found",
    }
    assert set(inventory["blockers"]) == {
        blocker for blocker, check in expected_blockers.items() if not inventory["checks"][check]
    }

    assert inventory["privacy"] == {
        "raw_computer_name_omitted": True,
        "broker_account_not_read": True,
        "userdata_contents_not_enumerated": True,
        "user_profile_paths_tokenized": True,
    }
    assert "computer_name" not in set(_walk_keys(inventory))
    assert "account_id" not in set(_walk_keys(inventory))
    computer_name = os.environ.get("COMPUTERNAME", "")
    if computer_name:
        assert computer_name.casefold() not in result.stdout.casefold()
    username = os.environ.get("USERNAME", "")
    if username and len(username) >= 3:
        assert username.casefold() not in result.stdout.casefold()


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell is required")
def test_environment_diagnostic_rejects_relative_inventory_paths() -> None:
    result = _run_diagnostic("-QmtRoot", ".\\relative-qmt")

    assert result.returncode != 0
    assert "QmtRoot must be an absolute path" in result.stderr


def test_environment_diagnostic_has_no_qmt_session_or_recursive_drive_scan() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "XtQuantTrader" not in source
    assert "subscribe_quote" not in source
    assert "order_stock" not in source
    assert "-Recurse" not in source
    assert "$maximumDirectoriesPerRoot = 300" in source
