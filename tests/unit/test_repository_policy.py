from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from auto_trading_system.security import repository_policy
from auto_trading_system.security.repository_policy import (
    RepositoryPolicyError,
    RepositorySecretBoundary,
    main,
)

SECRET = "real-runtime-secret-0123456789"


def test_policy_finds_forbidden_names_private_keys_and_known_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("safe placeholder", encoding="utf-8")
    source_file = tmp_path / "module.py"
    source_file.write_text(f"value = {SECRET!r}", encoding="utf-8")
    key_file = tmp_path / "readme.md"
    key_file.write_bytes(b"-----BEGIN " + b"RSA PRIVATE KEY-----\nnot-a-real-key")

    findings = RepositorySecretBoundary((SECRET,)).inspect(
        tmp_path.resolve(),
        (env_file.name, source_file.name, key_file.name),
    )

    assert {(finding.code, finding.relative_path) for finding in findings} == {
        ("CREDENTIAL_ARTIFACT", ".env.production"),
        ("KNOWN_SECRET_CONTENT", "module.py"),
        ("PRIVATE_KEY_CONTENT", "readme.md"),
    }
    assert SECRET not in repr(findings)


def test_policy_scans_compressed_document_members(tmp_path: Path) -> None:
    document = tmp_path / "design.docx"
    with zipfile.ZipFile(document, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f"<text>{SECRET}</text>")

    findings = RepositorySecretBoundary((SECRET,)).inspect(
        tmp_path.resolve(),
        (document.name,),
    )

    assert findings[0].code == "KNOWN_SECRET_CONTENT"
    assert findings[0].relative_path == "design.docx"


def test_policy_accepts_example_environment_file_and_safe_sources(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("ATS_SECRET_NODE_HMAC=replace-me", encoding="utf-8")
    source = tmp_path / "safe.py"
    source.write_text("account_id = 'acct_alpha'", encoding="utf-8")

    findings = RepositorySecretBoundary((SECRET,)).inspect(
        tmp_path.resolve(),
        (example.name, source.name),
    )

    assert findings == ()


def test_policy_flags_prohibited_directories_and_credential_suffixes(tmp_path: Path) -> None:
    findings = RepositorySecretBoundary().inspect(
        tmp_path.resolve(),
        (
            "config/local/runtime.json",
            "evidence/local/fault.zip",
            "runtime-data/local.db",
            "keys/node.private.pem",
            "backup/account.crd",
        ),
    )

    assert len(findings) == 5
    assert {finding.code for finding in findings} == {"CREDENTIAL_ARTIFACT"}


def test_policy_reports_invalid_root_and_bad_archive_without_content(tmp_path: Path) -> None:
    with pytest.raises(RepositoryPolicyError, match="REPOSITORY_ROOT_INVALID"):
        RepositorySecretBoundary().inspect(Path("relative"), ())
    archive = tmp_path / "broken.docx"
    archive.write_bytes(b"not a zip")

    findings = RepositorySecretBoundary().inspect(tmp_path.resolve(), (archive.name, "../escape"))

    assert {(finding.code, finding.relative_path) for finding in findings} == {
        ("ARCHIVE_SCAN_FAILED", "broken.docx"),
        ("PATH_INVALID", "../escape"),
    }


def test_main_fails_closed_on_known_secret_without_printing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.py"
    source.write_text(SECRET, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATS_SECRET_TEST", SECRET)
    monkeypatch.setattr(repository_policy, "git_candidate_paths", lambda _: (source.name,))

    assert main([]) == 3
    rendered = capsys.readouterr().err
    assert "KNOWN_SECRET_CONTENT: source.py" in rendered
    assert SECRET not in rendered


def test_main_rejects_arguments_and_reports_inventory_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["unexpected"]) == 2
    monkeypatch.chdir(tmp_path)

    def fail_inventory(_: Path) -> tuple[str, ...]:
        raise RepositoryPolicyError("GIT_INVENTORY_FAILED", "safe failure")

    monkeypatch.setattr(repository_policy, "git_candidate_paths", fail_inventory)
    assert main([]) == 2
    assert "GIT_INVENTORY_FAILED" in capsys.readouterr().err
