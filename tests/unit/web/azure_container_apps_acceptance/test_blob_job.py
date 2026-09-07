"""The in-environment Job exercises both plugins and refuses missing observations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobClient, BlobServiceClient

from elspeth.core.config import load_settings_from_yaml_string
from elspeth.plugins.sinks.json_sink import JSONSinkConfig
from elspeth.web import azure_blob_acceptance_job as job


def _service(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, bytes]:
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "ManagedIdentityCredential")
    service = MagicMock(spec=BlobServiceClient)
    blob = MagicMock(spec=BlobClient)
    service.get_blob_client.return_value = blob
    content = b'{"id":1,"value":"azure-blob-managed-identity"}\n'
    blob.download_blob.return_value.readall.return_value = content
    blob.upload_blob.side_effect = ResourceExistsError("exists")
    blob.exists.side_effect = [True, False]
    monkeypatch.setattr(job.AzureAuthConfig, "create_blob_service_client", lambda self: service)
    return blob, content


def test_job_runs_both_production_configs_observes_bytes_and_deletes_unique_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blob, content = _service(monkeypatch)
    names: list[str] = []

    def pipeline(document: str, directory: Path, name: str) -> None:
        settings = load_settings_from_yaml_string(document)
        names.append(name)
        config = json.loads(document)
        if name == "source":
            JSONSinkConfig.model_validate(config["sinks"]["output"]["options"])
            (directory / "output.jsonl").write_bytes(content)
        assert settings.landscape.url.startswith("sqlite:///")

    monkeypatch.setattr(job, "_pipeline", pipeline)
    result = job.run_probe(account_url="https://acceptance.blob.core.windows.net", container="proof", directory=tmp_path)
    assert names == ["sink", "source"]
    assert result == {
        "cases_total": 2,
        "cases_passed": 2,
        "blob_sha256": hashlib.sha256(content).hexdigest(),
        "collision_rejected": True,
        "cleanup_succeeded": True,
    }
    blob.upload_blob.assert_called_once_with(b"conflicting-content", overwrite=False)
    blob.delete_blob.assert_called_once()
    blob.close.assert_called_once()


@pytest.mark.parametrize("failure", ["sink", "source", "collision", "cleanup", "bytes"])
def test_job_never_reports_success_after_failed_lifecycle_or_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    blob, content = _service(monkeypatch)
    if failure == "collision":
        blob.upload_blob.side_effect = None
    if failure == "cleanup":
        blob.exists.side_effect = [True, True]
    if failure == "bytes":
        blob.download_blob.return_value.readall.return_value = b"{}\n"

    def pipeline(document: str, directory: Path, name: str) -> None:
        if failure == name:
            raise RuntimeError("pipeline failed")
        if name == "source":
            (directory / "output.jsonl").write_bytes(content)

    monkeypatch.setattr(job, "_pipeline", pipeline)
    with pytest.raises(RuntimeError):
        job.run_probe(account_url="https://acceptance.blob.core.windows.net", container="proof", directory=tmp_path)
    blob.delete_blob.assert_called_once()
    blob.close.assert_called_once()


def test_job_refuses_ambient_non_managed_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_TOKEN_CREDENTIALS", raising=False)
    with pytest.raises(ValueError, match="managed_identity_credential_required"):
        job.run_probe(account_url="https://acceptance.blob.core.windows.net", container="proof", directory=tmp_path)
