"""Bounded, redacted diagnostic bundle export for operator fault evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from auto_trading_system.monitoring.redaction import SensitiveValueRedactor

_ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.(?:json|txt)$")
_MAX_ARTIFACTS = 32
_MAX_ARTIFACT_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 8_388_608


class DiagnosticBundleError(RuntimeError):
    """Stable export failure that never contains artifact content or local paths."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class DiagnosticArtifact:
    """One caller-supplied text or structured diagnostic input."""

    name: str
    content: object


@dataclass(frozen=True, slots=True)
class DiagnosticBundleResult:
    """Non-sensitive integrity facts returned after a successful export."""

    bundle_sha256: str
    artifact_count: int
    total_uncompressed_bytes: int


class DiagnosticBundleWriter:
    """Write only sanitized text/JSON artifacts into a bounded ZIP file."""

    def __init__(self, redactor: SensitiveValueRedactor) -> None:
        self._redactor = redactor

    def write(
        self,
        destination: Path,
        artifacts: tuple[DiagnosticArtifact, ...],
        *,
        created_at: datetime,
    ) -> DiagnosticBundleResult:
        self._validate_destination(destination)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise DiagnosticBundleError(
                "DIAGNOSTIC_TIME_INVALID",
                "bundle time must include a UTC offset",
            )
        if not 1 <= len(artifacts) <= _MAX_ARTIFACTS:
            raise DiagnosticBundleError(
                "DIAGNOSTIC_ARTIFACT_COUNT_INVALID",
                "artifact count is outside the supported range",
            )

        names: set[str] = set()
        rendered: list[tuple[str, bytes]] = []
        total_bytes = 0
        for artifact in artifacts:
            if (
                not isinstance(artifact, DiagnosticArtifact)
                or _ARTIFACT_NAME_PATTERN.fullmatch(artifact.name) is None
                or artifact.name.casefold() == "manifest.json"
            ):
                raise DiagnosticBundleError(
                    "DIAGNOSTIC_ARTIFACT_NAME_INVALID",
                    "artifact name is invalid or reserved",
                )
            normalized_name = artifact.name.casefold()
            if normalized_name in names:
                raise DiagnosticBundleError(
                    "DIAGNOSTIC_ARTIFACT_DUPLICATE",
                    "artifact names must be unique",
                )
            names.add(normalized_name)
            payload = self._render_artifact(artifact)
            if len(payload) > _MAX_ARTIFACT_BYTES:
                raise DiagnosticBundleError(
                    "DIAGNOSTIC_ARTIFACT_TOO_LARGE",
                    "artifact exceeds the supported size",
                )
            total_bytes += len(payload)
            if total_bytes > _MAX_TOTAL_BYTES:
                raise DiagnosticBundleError(
                    "DIAGNOSTIC_BUNDLE_TOO_LARGE",
                    "bundle exceeds the supported size",
                )
            rendered.append((artifact.name, payload))

        manifest = {
            "bundle_version": "1.0",
            "created_at": created_at.isoformat(),
            "artifacts": [
                {
                    "name": name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
                for name, payload in rendered
            ],
        }
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=".ats-diagnostic-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
            with zipfile.ZipFile(
                temp_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                for name, payload in rendered:
                    archive.writestr(name, payload)
            if destination.exists():
                raise DiagnosticBundleError(
                    "DIAGNOSTIC_DESTINATION_EXISTS",
                    "diagnostic destination already exists",
                )
            os.replace(temp_path, destination)
            temp_path = None
            bundle_bytes = destination.read_bytes()
            return DiagnosticBundleResult(
                bundle_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
                artifact_count=len(rendered),
                total_uncompressed_bytes=total_bytes + len(manifest_bytes),
            )
        except DiagnosticBundleError:
            raise
        except OSError as exc:
            raise DiagnosticBundleError(
                "DIAGNOSTIC_WRITE_FAILED",
                "diagnostic bundle could not be written",
            ) from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _render_artifact(self, artifact: DiagnosticArtifact) -> bytes:
        sanitized = self._redactor.sanitize(artifact.content)
        if isinstance(sanitized, str) and artifact.name.casefold().endswith(".txt"):
            rendered = sanitized
        else:
            rendered = json.dumps(
                sanitized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        payload = f"{rendered}\n".encode()
        if self._redactor.contains_registered_secret(payload):
            raise DiagnosticBundleError(
                "DIAGNOSTIC_REDACTION_FAILED",
                "registered secret remained after redaction",
            )
        return payload

    @staticmethod
    def _validate_destination(destination: Path) -> None:
        if not isinstance(destination, Path) or not destination.is_absolute():
            raise DiagnosticBundleError(
                "DIAGNOSTIC_DESTINATION_INVALID",
                "diagnostic destination must be an absolute path",
            )
        if destination.suffix.casefold() != ".zip":
            raise DiagnosticBundleError(
                "DIAGNOSTIC_DESTINATION_INVALID",
                "diagnostic destination must use the .zip extension",
            )
        if not destination.parent.is_dir() or destination.parent.is_symlink():
            raise DiagnosticBundleError(
                "DIAGNOSTIC_DESTINATION_INVALID",
                "diagnostic destination parent is invalid",
            )
