from __future__ import annotations

import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_quality_gate_toolchain_versions_match_project_manifests() -> None:
    package = json.loads(
        (PROJECT_ROOT / "frontend" / "operator-console" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    check_script = (PROJECT_ROOT / "scripts" / "check.ps1").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert package["engines"]["node"] == ">=20.19.0"
    assert package["packageManager"] == "pnpm@11.19.0"
    assert project["project"]["requires-python"] == ">=3.11,<3.13"
    assert project["tool"]["coverage"]["report"]["precision"] == 2
    sdist_excludes = project["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "/evidence" in sdist_excludes
    assert "/.codex_tmp" in sdist_excludes
    assert "/dist" in sdist_excludes
    assert '[version]"20.19.0"' in check_script
    assert '[version]"3.11.0"' in check_script
    assert '[version]"3.13.0"' in check_script
    assert 'node-version: "22"' in workflow
    assert 'python-version: "3.11"' in workflow


def test_quality_gate_keeps_all_required_validation_layers() -> None:
    check_script = (PROJECT_ROOT / "scripts" / "check.ps1").read_text(encoding="utf-8")
    package = json.loads(
        (PROJECT_ROOT / "frontend" / "operator-console" / "package.json").read_text(
            encoding="utf-8"
        )
    )

    for command in (
        '"auto_trading_system.security.repository_policy"',
        '"compileall"',
        '"ruff"',
        '"mypy"',
        '"pytest"',
        '"build"',
        "verify-scheduled-task-scripts.ps1",
        '"install", "--frozen-lockfile"',
    ):
        assert command in check_script
    assert "$nativeExitCode = $LASTEXITCODE" in check_script
    assert "Invoke-NativeQualityCommand" in check_script
    for command in (
        "pnpm lint",
        "pnpm format:check",
        "pnpm typecheck",
        "pnpm test:coverage",
        "pnpm build",
    ):
        assert command in package["scripts"]["check"]
