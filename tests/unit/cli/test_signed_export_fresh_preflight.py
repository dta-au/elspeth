"""Fresh CLI runs refuse legacy signed exports before pipeline side effects."""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app, bootstrap_and_run


def _legacy_signed_settings(tmp_path: Path) -> Path:
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
                        "exporter_version": "landscape-exporter-auth-v1",
                        "signing_mode": "hmac_sha256",
                        "signer_key_id": "legacy-key",
                        "signing_secret_ref": "AUDIT_EXPORT_TEST_KEY",
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


def test_run_refuses_signed_auth_v1_before_run_or_content_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings_path = _legacy_signed_settings(tmp_path)

    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 1, result.output
    assert "legacy signed export" in result.output
    assert not (tmp_path / "landscape.db").exists()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "audit-export.json").exists()
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / ".elspeth" / "audit-export-content-store").exists()


def test_dependency_bootstrap_refuses_signed_auth_v1_before_run_or_content_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings_path = _legacy_signed_settings(tmp_path)

    with pytest.raises(ValueError, match="legacy signed export"):
        bootstrap_and_run(settings_path)

    assert not (tmp_path / "landscape.db").exists()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "audit-export.json").exists()
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / ".elspeth" / "audit-export-content-store").exists()


def test_run_refuses_missing_auth_v2_compartment_before_run_or_content_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings_path = _legacy_signed_settings(tmp_path)
    config = yaml.safe_load(settings_path.read_text())
    config["landscape"]["export"]["exporter_version"] = "landscape-exporter-auth-v2"
    settings_path.write_text(yaml.safe_dump(config))

    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 1, result.output
    assert "compartment_id" in result.output
    assert not (tmp_path / "landscape.db").exists()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "audit-export.json").exists()
    assert not (tmp_path / "payloads").exists()
    assert not (tmp_path / ".elspeth" / "audit-export-content-store").exists()
