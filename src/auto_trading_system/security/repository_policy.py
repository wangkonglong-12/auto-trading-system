"""Repository preflight that blocks credential artifacts and known secret values."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_ARCHIVE_SUFFIXES = frozenset({".docx", ".zip"})
_PROHIBITED_SUFFIXES = (
    ".secrets.json",
    ".private.pem",
    ".pfx",
    ".p12",
    ".crd",
)
_PROHIBITED_DIRECTORIES = (
    ("config", "local"),
    ("evidence", "local"),
    ("runtime-data",),
)
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    *(
        b"-----BEGIN " + key_type + b" PRIVATE KEY-----"
        for key_type in (b"RSA", b"EC", b"DSA", b"OPENSSH", b"ENCRYPTED")
    ),
)


class RepositoryPolicyError(RuntimeError):
    """Stable scanner failure that never contains matched content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True, order=True)
class RepositoryPolicyFinding:
    code: str
    relative_path: str


class RepositorySecretBoundary:
    """Inspect Git candidates without ever rendering the matched value."""

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        encoded: set[bytes] = set()
        for secret in known_secrets:
            if not isinstance(secret, str):
                raise TypeError("known secrets must be strings")
            if len(secret) >= 4:
                encoded.add(secret.encode())
                encoded.add(secret.encode("utf-16-le"))
        self._encoded_secrets = tuple(sorted(encoded, key=len, reverse=True))

    def inspect(
        self,
        root: Path,
        relative_paths: Iterable[str],
    ) -> tuple[RepositoryPolicyFinding, ...]:
        if not isinstance(root, Path) or not root.is_absolute() or not root.is_dir():
            raise RepositoryPolicyError(
                "REPOSITORY_ROOT_INVALID",
                "repository root must be an absolute directory",
            )
        resolved_root = root.resolve(strict=True)
        findings: set[RepositoryPolicyFinding] = set()
        for relative_text in relative_paths:
            relative = PurePosixPath(relative_text.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                findings.add(RepositoryPolicyFinding("PATH_INVALID", relative_text))
                continue
            normalized_parts = tuple(part.casefold() for part in relative.parts)
            name = normalized_parts[-1] if normalized_parts else ""
            normalized_path = "/".join(normalized_parts)
            if self._is_prohibited_name(name, normalized_parts):
                findings.add(RepositoryPolicyFinding("CREDENTIAL_ARTIFACT", normalized_path))
            candidate = resolved_root.joinpath(*relative.parts)
            if not candidate.exists() or not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                resolved_candidate = candidate.resolve(strict=True)
                if not resolved_candidate.is_relative_to(resolved_root):
                    findings.add(RepositoryPolicyFinding("PATH_INVALID", normalized_path))
                    continue
                if candidate.stat().st_size > _MAX_FILE_BYTES:
                    findings.add(RepositoryPolicyFinding("FILE_TOO_LARGE_TO_SCAN", normalized_path))
                    continue
                data = candidate.read_bytes()
            except OSError:
                findings.add(RepositoryPolicyFinding("FILE_SCAN_FAILED", normalized_path))
                continue
            self._inspect_bytes(data, normalized_path, findings)
            if candidate.suffix.casefold() in _ARCHIVE_SUFFIXES:
                self._inspect_archive(candidate, normalized_path, findings)
        return tuple(sorted(findings))

    @staticmethod
    def _is_prohibited_name(name: str, parts: tuple[str, ...]) -> bool:
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
        if name.endswith(_PROHIBITED_SUFFIXES):
            return True
        return any(parts[: len(prefix)] == prefix for prefix in _PROHIBITED_DIRECTORIES)

    def _inspect_bytes(
        self,
        data: bytes,
        relative_path: str,
        findings: set[RepositoryPolicyFinding],
    ) -> None:
        if any(marker in data for marker in _PRIVATE_KEY_MARKERS):
            findings.add(RepositoryPolicyFinding("PRIVATE_KEY_CONTENT", relative_path))
        if any(secret in data for secret in self._encoded_secrets):
            findings.add(RepositoryPolicyFinding("KNOWN_SECRET_CONTENT", relative_path))

    def _inspect_archive(
        self,
        path: Path,
        relative_path: str,
        findings: set[RepositoryPolicyFinding],
    ) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if sum(member.file_size for member in members) > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    findings.add(
                        RepositoryPolicyFinding("ARCHIVE_TOO_LARGE_TO_SCAN", relative_path)
                    )
                    return
                for member in members:
                    if member.is_dir() or member.file_size > _MAX_FILE_BYTES:
                        continue
                    self._inspect_bytes(archive.read(member), relative_path, findings)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            findings.add(RepositoryPolicyFinding("ARCHIVE_SCAN_FAILED", relative_path))


def git_candidate_paths(root: Path) -> tuple[str, ...]:
    """Return tracked and non-ignored untracked paths using NUL-safe Git output."""
    try:
        completed = subprocess.run(
            ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
            cwd=root,
            check=False,
            capture_output=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryPolicyError(
            "GIT_INVENTORY_FAILED",
            "Git candidate inventory could not be collected",
        ) from exc
    if completed.returncode != 0:
        raise RepositoryPolicyError(
            "GIT_INVENTORY_FAILED",
            "Git candidate inventory could not be collected",
        )
    return tuple(
        raw_path.decode("utf-8", errors="surrogateescape")
        for raw_path in completed.stdout.split(b"\x00")
        if raw_path
    )


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        print("repository secret check does not accept arguments", file=sys.stderr)
        return 2
    root = Path.cwd().resolve()
    known_secrets = (
        value for name, value in os.environ.items() if name.startswith("ATS_SECRET_") and value
    )
    try:
        findings = RepositorySecretBoundary(known_secrets).inspect(
            root,
            git_candidate_paths(root),
        )
    except RepositoryPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"{finding.code}: {finding.relative_path}", file=sys.stderr)
        return 3
    print("repository secret boundary verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
