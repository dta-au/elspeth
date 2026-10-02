"""CLI contract for standalone delivered audit-export verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.audit_export import AuditExportDerivationConfig, derive_audit_export_bundle

runner = CliRunner()
HOSTILE_BOUNDED_JSON = (
    pytest.param(
        b'{"secret":' + (b"[" * 1500) + b'"deep-secret-marker"' + (b"]" * 1500) + b"}",
        "deep-secret-marker",
        id="excessive-nesting",
    ),
    pytest.param(
        b'{"secret":"surrogate-secret-marker","value":"\\ud800"}',
        "surrogate-secret-marker",
        id="lone-surrogate",
    ),
)


def _write_export(path: Path, *, signed: bool) -> None:
    completed_at = "2026-09-30T02:03:04.000005Z"
    config = AuditExportDerivationConfig(
        source_run_id="run-cli-verification",
        source_status="completed",
        source_completed_at=completed_at,
        export_format="json",
        exporter_version="landscape-exporter-auth-v2",
        serialization_version="audit-export-v3",
        chunking_algorithm_version="record-framing-v1",
        include_raw_error_rows=False,
        per_chunk_byte_limit=2048,
        per_chunk_record_limit=10,
        signing_mode="hmac_sha256" if signed else "unsigned",
        signer_key_id="audit-key-2026-q2" if signed else "UNSIGNED",
        signing_key=b"retained-secret" if signed else None,
        compartment_id="test-compartment",
    )
    records = (
        {
            "completed_at": completed_at,
            "record_type": "run",
            "run_id": config.source_run_id,
            "status": config.source_status,
        },
        {"record_type": "audit_export_config", "public_config": config.public_snapshot_config()},
        {
            "policy": "omitted",
            "reason": "not_requested",
            "record_type": "auth_event_coverage",
            "selected_count": None,
            "selection_basis": None,
            "selection_cutoff": None,
        },
    )
    path.write_bytes(derive_audit_export_bundle(records, config).json_target_bytes)


def test_verify_command_uses_exact_historical_signer_reference_and_reports_json(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    _write_export(target, signed=True)

    result = runner.invoke(
        app,
        [
            "audit-export",
            "verify",
            str(target),
            "--key-ref",
            "active-key-2026-q3=ACTIVE_KEY",
            "--key-ref",
            "audit-key-2026-q2=RETAINED_KEY",
            "--json",
        ],
        env={"ACTIVE_KEY": "new-secret", "RETAINED_KEY": "retained-secret"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["code"] == "ok"
    assert payload["authenticated"] is True
    assert payload["signer_key_id"] == "audit-key-2026-q2"
    assert payload["format"] == "json"
    assert payload["artifact_digest"] == hashlib.sha256(target.read_bytes()).hexdigest()

    console = runner.invoke(
        app,
        ["audit-export", "verify", str(target), "--key-ref", "audit-key-2026-q2=RETAINED_KEY"],
        env={"RETAINED_KEY": "retained-secret"},
    )
    assert console.exit_code == 0
    assert f"artifact_digest={payload['artifact_digest']}" in console.stdout


def test_verify_command_fails_closed_for_unknown_historical_signer(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    _write_export(target, signed=True)

    result = runner.invoke(
        app,
        ["audit-export", "verify", str(target), "--key-ref", "active-key-2026-q3=ACTIVE_KEY", "--json"],
        env={"ACTIVE_KEY": "new-secret"},
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "authenticated": False,
        "code": "unknown_signer",
        "message": "no retained audit-export verification key for signer 'audit-key-2026-q2'",
        "status": "failed",
    }


def test_verify_command_requires_authentication_unless_unsigned_override_is_explicit(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    _write_export(target, signed=False)

    refused = runner.invoke(app, ["audit-export", "verify", str(target), "--json"])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["code"] == "unsigned_refused"

    console_refused = runner.invoke(app, ["audit-export", "verify", str(target)])
    assert console_refused.exit_code == 1
    assert "FAILED [unsigned_refused]" in console_refused.output

    allowed = runner.invoke(app, ["audit-export", "verify", str(target), "--allow-unsigned", "--json"])
    assert allowed.exit_code == 0
    payload = json.loads(allowed.stdout)
    assert payload["authenticated"] is False
    assert payload["status"] == "verified"


def test_verify_command_reports_wrong_key_and_input_io_as_stable_failures(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    _write_export(target, signed=True)
    wrong_key = runner.invoke(
        app,
        ["audit-export", "verify", str(target), "--key-ref", "audit-key-2026-q2=RETAINED_KEY", "--json"],
        env={"RETAINED_KEY": "wrong-secret"},
    )
    assert wrong_key.exit_code == 1
    assert json.loads(wrong_key.stdout)["code"] == "invalid_export"
    assert "wrong-secret" not in wrong_key.output
    assert "RETAINED_KEY" not in wrong_key.output

    console_wrong_key = runner.invoke(
        app,
        ["audit-export", "verify", str(target), "--key-ref", "audit-key-2026-q2=RETAINED_KEY"],
        env={"RETAINED_KEY": "wrong-secret"},
    )
    assert console_wrong_key.exit_code == 1
    assert "FAILED [invalid_export]" in console_wrong_key.output
    assert "wrong-secret" not in console_wrong_key.output
    assert "RETAINED_KEY" not in console_wrong_key.output

    missing = runner.invoke(app, ["audit-export", "verify", str(tmp_path / "missing.jsonl"), "--json"])
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["code"] == "invalid_export"


@pytest.mark.parametrize(("content", "secret_marker"), HOSTILE_BOUNDED_JSON)
def test_verify_command_normalizes_bounded_parser_failures_without_echoing_input(
    tmp_path: Path,
    content: bytes,
    secret_marker: str,
) -> None:
    target = tmp_path / "hostile.jsonl"
    target.write_bytes(content)
    expected_message = "audit export final manifest is not valid canonical JSON"

    json_result = runner.invoke(app, ["audit-export", "verify", str(target), "--json"])
    assert json_result.exit_code == 1
    assert json.loads(json_result.stdout) == {
        "authenticated": False,
        "code": "invalid_export",
        "message": expected_message,
        "status": "failed",
    }
    assert secret_marker not in json_result.output

    console_result = runner.invoke(app, ["audit-export", "verify", str(target)])
    assert console_result.exit_code == 1
    assert console_result.stdout == ""
    assert console_result.stderr == f"FAILED [invalid_export]: {expected_message}\n"
    assert secret_marker not in console_result.output


def test_verify_command_rejects_duplicate_signer_mappings_as_usage_error(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    _write_export(target, signed=True)

    result = runner.invoke(
        app,
        [
            "audit-export",
            "verify",
            str(target),
            "--key-ref",
            "audit-key-2026-q2=RETAINED_KEY",
            "--key-ref",
            "audit-key-2026-q2=OTHER_KEY",
        ],
    )

    assert result.exit_code == 2
    assert "duplicate key reference" in result.output


def test_verify_command_help_documents_authenticated_default_and_unsigned_override() -> None:
    result = runner.invoke(app, ["audit-export", "verify", "--help"])

    assert result.exit_code == 0
    help_text = strip_ansi(result.output)
    assert "--key-ref" in help_text
    assert "historical signer" in help_text
    assert "--allow-unsigned" in help_text
    assert "integrity-only" in help_text
