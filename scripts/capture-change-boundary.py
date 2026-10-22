"""Capture/compare file hashes without opening databases or changing either repository.

The baseline includes tracked and non-ignored untracked files because this workspace
contains user changes that are not committed. Reports contain paths and hashes only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def snapshot(root: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-c", "-o", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    records: dict[str, str] = {}
    for name in sorted(set(result.stdout.decode("utf-8").split("\0")) - {""}):
        path = root / name
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as source:
                records[name] = hashlib.file_digest(source, "sha256").hexdigest()
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    current = snapshot(root)
    report: dict[str, object] = {"root": str(root), "files": current}
    if args.compare:
        previous = json.loads(args.compare.read_text(encoding="utf-8"))
        if Path(previous["root"]).resolve() != root:
            parser.error("baseline repository does not match requested root")
        old = previous["files"]
        report = {
            "root": str(root),
            "added": sorted(current.keys() - old.keys()),
            "deleted": sorted(old.keys() - current.keys()),
            "modified": sorted(k for k in current.keys() & old.keys() if current[k] != old[k]),
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, ensure_ascii=False))
    print(f"files inspected: {len(current)}")


if __name__ == "__main__":
    main()
