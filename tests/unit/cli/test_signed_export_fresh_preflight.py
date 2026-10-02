"""Fresh CLI runs refuse unmarked and retired audit-export contracts early."""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app, bootstrap_and_run


def _export_settings(tmp_path: Path, *, signed: bool, exporter_version: str, compartment_id: str | None = None) -> Path:
    (tmp_path / "input.csv").write_text("id\n1\n")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "primary": {
                        "plugin": "csv",
                        "on_success": "default",
                        "options": {
                            "path": str(tmp_path / "input.csv"),
                            "on_validation_failure": "discard",
                            "schema": {"mode": "observed"},
                        },
                    }
                },
                "sinks": {
                    "default": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {"path": str(tmp_path / "output.json"), "schema": {"mode": "observed"}},
                    },
                    "archive": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {"path": str(tmp_path / "audit-export.json"), "schema": {"mode": "observed"}},
                    },
                },
                "landscape": {
                    "url": f"sqlite:///{tmp_path / 'landscape.db'}",
                    "export": {
                        "enabled": True,
                        "sink": "archive",
                        "format": "json",
                        "exporter_version": exporter_version,
                        "compartment_id": compartment_id,
                        "signing_mode": "hmac_sha256" if signed else "unsigned",
                        "signer_key_id": "test-key" if signed else "UNSIGNED",
                        **({"signing_secret_ref": "AUDIT_EXPORT_TEST_KEY"} if signed else {}),
                        "total_record_limit": 10_000,
                        "total_byte_limit": 10_485_760,
                        "chunk_limit": 100,
                        "per_chunk_record_limit": 100,
                        "per_chunk_byte_limit": 1_048_576,
                        "spool_root": ".elspeth/audit-export-spool/cli-test",
                        "content_store": {
                            "content_store_id": "audit-store-v1",
                            "namespace": "audit/export",
                            "root": ".elspeth/audit-export-content-store/cli-test",
                            "policy_version": "v1",
                            "retention_days": 30,
                            "durability": "fsync",
                        },
                    },
                },
                "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
            }
        )
    )
    return settings_path


@pytest.mark.parametrize("signed", [False, True])
def test_run_publishes_marked_auth_v2_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIT_EXPORT_TEST_KEY", "test-secret")
    settings_path = _export_settings(
        tmp_path, signed=signed, exporter_version="landscape-exporter-auth-v2", compartment_id="test-compartment"
    )

    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "landscape.db").exists()
    export_path = tmp_path / "audit-export.json"
    assert export_path.exists()
    export_text = export_path.read_text()
    algorithm = "hmac_sha256" if signed else "unsigned"
    assert '"compartment_id":"test-compartment"' in export_text
    assert f'"signature_algorithm":"{algorithm}"' in export_text


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("exporter_version", ["landscape-exporter-v1", "landscape-exporter-auth-v1"])
def test_run_refuses_legacy_exporter_before_run_or_content_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool, exporter_version: str
) -> None:
    monkeypatch.chdir(tmp_path)
    settings_path = _export_settings(tmp_path, signed=signed, exporter_version=exporter_version, compartment_id="test-compartment")

    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 1, result.output
    assert "exporter_version" in result.output
    assert not (tmp_path / "landscape.db").exists()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "audit-export.json").exists()
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / ".elspeth" / "audit-export-content-store").exists()


@pytest.mark.parametrize("signed", [False, True])
def test_dependency_bootstrap_refuses_unmarked_export_before_run_or_content_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    settings_path = _export_settings(tmp_path, signed=signed, exporter_version="landscape-exporter-auth-v2")

    with pytest.raises(ValueError, match="compartment_id"):
        bootstrap_and_run(settings_path)

    assert not (tmp_path / "landscape.db").exists()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "audit-export.json").exists()
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / ".elspeth" / "audit-export-content-store").exists()


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("compartment_id", [None, "bad\nmarker"])
def test_run_refuses_missing_or_invalid_compartment_before_run_or_content_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool, compartment_id: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    settings_path = _export_settings(tmp_path, signed=signed, exporter_version="landscape-exporter-auth-v2", compartment_id=compartment_id)

    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 1, result.output
    assert "compartment_id" in result.output
    assert not (tmp_path / "landscape.db").exists()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "audit-export.json").exists()
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / ".elspeth" / "audit-export-content-store").exists()
