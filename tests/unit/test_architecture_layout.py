from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "auto_trading_system"


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def test_required_architecture_packages_exist() -> None:
    required = {
        "apps",
        "config",
        "control",
        "domain",
        "execution",
        "gateways",
        "journal",
        "monitoring",
        "orders",
        "protocol",
        "reconcile",
        "recorder",
        "risk",
        "runtime",
        "strategies",
        "sync",
        "testing",
    }
    assert required <= {path.name for path in PACKAGE_ROOT.iterdir() if path.is_dir()}


def test_protocol_uses_only_standard_library_and_own_package() -> None:
    allowed = {"__future__", "auto_trading_system", "collections", "dataclasses", "datetime"}
    allowed |= {
        "decimal",
        "hashlib",
        "hmac",
        "json",
        "re",
        "secrets",
        "threading",
        "typing",
        "uuid",
    }
    for path in (PACKAGE_ROOT / "protocol").glob("*.py"):
        assert imported_roots(path) <= allowed, path


def test_domain_uses_only_standard_library_and_own_package() -> None:
    allowed = {"__future__", "auto_trading_system", "dataclasses", "datetime", "decimal"}
    allowed |= {"enum", "re", "time", "typing", "zoneinfo"}
    for path in (PACKAGE_ROOT / "domain").glob("*.py"):
        assert imported_roots(path) <= allowed, path


def test_xtquant_imports_stay_inside_qmt_adapters() -> None:
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        has_dynamic_sdk_reference = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("xtquant")
            for node in ast.walk(tree)
        )
        if "xtquant" in imported_roots(path) or has_dynamic_sdk_reference:
            normalized = path.as_posix()
            assert (
                normalized.endswith("gateways/broker/cicc_qmt.py")
                or normalized.endswith("gateways/broker/cicc_qmt_snapshot.py")
                or normalized.endswith("gateways/broker/qmt_command_transport.py")
                or normalized.endswith("gateways/market/qmt.py")
            )


def test_sqlite_imports_stay_inside_journal_adapter() -> None:
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "sqlite3" in imported_roots(path):
            assert path.as_posix().endswith("journal/sqlite.py")


def test_web_framework_imports_stay_inside_application_or_test_roots() -> None:
    for path in PACKAGE_ROOT.rglob("*.py"):
        if imported_roots(path) & {"fastapi", "uvicorn"}:
            normalized = path.as_posix()
            assert "/apps/" in normalized or "/testing/" in normalized


def test_runtime_scheduler_cannot_open_broker_storage_or_network_adapters() -> None:
    scheduler_source = (PACKAGE_ROOT / "runtime" / "scheduler.py").read_text(encoding="utf-8")

    for forbidden in ("xtquant", "gateways.", "sqlite3", "httpx", "requests"):
        assert forbidden not in scheduler_source


def test_journal_contract_contains_no_sqlite_implementation_detail() -> None:
    for path in (PACKAGE_ROOT / "journal").glob("*.py"):
        if path.name == "sqlite.py":
            continue
        assert "sqlite3" not in path.read_text(encoding="utf-8")


def test_local_control_core_does_not_open_broker_storage_or_network_adapters() -> None:
    for path in (PACKAGE_ROOT / "control").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert imported_roots(path).isdisjoint({"sqlite3", "httpx", "requests"}), path
        assert "gateways." not in source, path


def test_broker_submit_calls_are_confined_to_the_persistence_gate() -> None:
    allowed = {
        (PACKAGE_ROOT / "execution" / "order_intent_gate.py").resolve(),
        (PACKAGE_ROOT / "execution" / "dispatch_boundary.py").resolve(),
    }
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit_order"
            ):
                assert path.resolve() in allowed, path


def test_apsw_access_stays_in_the_critical_journal_adapter() -> None:
    # Runtime orchestrates maintenance; only the two storage adapters own SQL.
    allowed = {"journal/apsw_store.py", "journal/apsw_backup.py"}
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "apsw" in imported_roots(path):
            assert path.relative_to(PACKAGE_ROOT).as_posix() in allowed, path


def test_memory_state_cannot_import_storage_network_or_vendor_adapters() -> None:
    path = PACKAGE_ROOT / "runtime" / "memory_state.py"
    assert imported_roots(path).isdisjoint({"apsw", "sqlite3", "httpx", "requests", "xtquant"})
