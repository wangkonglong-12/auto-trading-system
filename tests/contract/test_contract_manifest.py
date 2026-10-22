from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "v1"
MANIFEST_PATH = CONTRACT_ROOT / "manifest.sha256.json"


def _manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_contract_manifest_covers_the_exact_protocol_bundle() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == "1.0"
    assert manifest["algorithm"] == "SHA-256"

    entries = manifest["files"]
    declared = [entry["path"] for entry in entries]
    actual = sorted(
        path.relative_to(CONTRACT_ROOT).as_posix()
        for path in CONTRACT_ROOT.rglob("*")
        if path.is_file() and path != MANIFEST_PATH
    )

    assert len(declared) == len(set(declared))
    assert declared == sorted(declared)
    assert declared == actual


def test_every_contract_file_matches_its_manifest_digest() -> None:
    for entry in _manifest()["files"]:
        relative = PurePosixPath(entry["path"])
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        payload = CONTRACT_ROOT.joinpath(*relative.parts).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
