"""Protected live-Azure PB-09 lifecycle proof for source:azure_blob.

One node per production auth-discriminator variant.  Each node uploads a
disposable, uniquely-prefixed blob, then reads it back through the FULL
production boundary (settings YAML -> instantiate_plugins_from_config ->
ExecutionGraph -> Orchestrator.run with a real Landscape database) using the
variant's genuine Pydantic auth configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from elspeth.contracts import RunStatus
from tests.integration.plugins._azure_live import (
    AZURE_BLOB_AUTH_VARIANTS,
    AzureStorageTarget,
    ambient_blob_service_client,
    auth_options,
    delete_blob_if_present,
    ensure_fingerprint_key,
    local_jsonl_sink_options,
    read_jsonl,
    require_storage_target,
    run_orchestrated_pipeline,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_azure, pytest.mark.live_provider]


@pytest.fixture(params=AZURE_BLOB_AUTH_VARIANTS)
def azure_blob_auth(request: pytest.FixtureRequest) -> tuple[str, AzureStorageTarget, dict[str, object]]:
    """Resolve one auth variant's live credentials, skipping when unconfigured."""
    variant = request.param
    target = require_storage_target()
    return variant, target, auth_options(variant, target)


def test_azure_blob_source_live_lifecycle_round_trip(
    azure_blob_auth: tuple[str, AzureStorageTarget, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant, target, auth = azure_blob_auth
    ensure_fingerprint_key(monkeypatch)

    row: dict[str, object] = {"id": 1, "value": f"azure-blob-source-{variant}"}
    blob_path = target.unique_blob_path("input.jsonl")
    output_path = tmp_path / "output.jsonl"
    service = ambient_blob_service_client(target)
    try:
        service.get_container_client(target.container).upload_blob(blob_path, json.dumps(row).encode("utf-8") + b"\n")

        pipeline: dict[str, object] = {
            "sources": {
                "subject_source": {
                    "plugin": "azure_blob",
                    "on_success": "output",
                    "options": {
                        **auth,
                        "container": target.container,
                        "blob_path": blob_path,
                        "format": "jsonl",
                        "schema": {"mode": "observed"},
                        "on_validation_failure": "discard",
                    },
                }
            },
            "sinks": {
                "output": {
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": local_jsonl_sink_options(output_path),
                }
            },
        }

        evidence = run_orchestrated_pipeline(pipeline, tmp_path)
        assert evidence.status is RunStatus.COMPLETED
        evidence.assert_completed_lifecycle()
        assert read_jsonl(output_path) == [row]
    finally:
        delete_blob_if_present(service, target, blob_path)
        service.close()
