from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_SCRIPTS = PROJECT_ROOT / "scripts" / "windows"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def test_windows_task_scripts_pass_their_real_smoke_verifier() -> None:
    result = subprocess.run(
        (
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            str(WINDOWS_SCRIPTS / "verify-scheduled-task-scripts.ps1"),
        ),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "Windows scheduled task scripts verified" in result.stdout


def test_task_scripts_avoid_shell_text_and_plaintext_secret_arguments() -> None:
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in WINDOWS_SCRIPTS.glob("*.ps1")
    }
    combined = "\n".join(sources.values()).casefold()

    assert "invoke-expression" not in combined
    assert "cmd.exe" not in combined
    assert "convertto-securestring -asplaintext" not in combined
    assert '"autotrading-runtime"' in sources["install-scheduled-tasks.ps1"].casefold()
    assert '"autotrading-watchdog"' in sources["install-scheduled-tasks.ps1"].casefold()
    assert '"pt30s"' in sources["install-scheduled-tasks.ps1"].casefold()
    assert (
        "runtime secrets must use configured credential references"
        in sources["TaskDeployment.Common.ps1"]
    )


def test_wheel_configuration_bundles_the_verified_task_scripts() -> None:
    configuration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    force_include = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include == {
        "scripts/diagnose-environment.ps1": (
            "auto_trading_system/deployment/diagnose-environment.ps1"
        ),
        "scripts/windows": "auto_trading_system/deployment/windows",
    }
