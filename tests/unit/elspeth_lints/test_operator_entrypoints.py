"""Editable entrypoints must refuse before reading operator key files or data."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth_lints.core import cli, judge_signature_diagnosis
from elspeth_lints.core.allowlist import JudgeMetadataKeyUnavailableError, _hmac_key_bytes_from_env, _judge_metadata_hmac_key


@pytest.mark.parametrize(
    "arguments",
    [
        ["sign-bundle", "missing.json", "--owner", "operator", "--dry-run"],
        ["rekey", "--in", "missing.json", "--old-key-env", "OLD_ARBITRARY", "--new-key-env", "NEW_ARBITRARY"],
        ["check", "--rules", "nothing"],
    ],
)
def test_editable_cli_refuses_key_custody(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", "synthetic-only-" * 4)

    assert cli.main(arguments) == 2

    assert "/usr/local/libexec/elspeth-operator" in capsys.readouterr().err


def test_editable_diagnosis_refuses_before_opening_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing-private.env"

    assert judge_signature_diagnosis.main(["--env-file", str(missing)]) == 2

    error = capsys.readouterr().err
    assert "/usr/local/libexec/elspeth-operator" in error
    assert "is not a file" not in error


def test_keyless_check_remains_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", raising=False)

    assert cli.main(["check", "--rules", "nothing"]) == 0


@pytest.mark.parametrize("name", ["ELSPETH_JUDGE_METADATA_HMAC_KEY", "ARBITRARY_REKEY_NAME"])
def test_library_key_read_requires_protected_runtime(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, "synthetic-only-" * 4)

    with pytest.raises(JudgeMetadataKeyUnavailableError, match="/usr/local/libexec/elspeth-operator"):
        if name == "ELSPETH_JUDGE_METADATA_HMAC_KEY":
            _judge_metadata_hmac_key()
        else:
            _hmac_key_bytes_from_env(name)


def test_migration_renderer_uses_protected_entrypoint(tmp_path: Path) -> None:
    command = judge_signature_diagnosis._migrate_command(root=tmp_path / "source", allowlist_dir=tmp_path / "allowlist")

    assert "/usr/local/libexec/elspeth-operator" in command
    assert "PYTHONPATH" not in command
    assert ".venv/bin/python" not in command
