"""Protected live-Azure PB-09 lifecycle proof for sink:azure_blob.

One node per production auth-discriminator variant.  Each node publishes a
uniquely-prefixed disposable blob through the FULL production boundary
(settings YAML -> instantiate_plugins_from_config -> ExecutionGraph ->
Orchestrator.run with a real Landscape database), exercising the recoverable
sink-effect protocol end to end, then externally observes the published blob.
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
    jsonl_source_options,
    require_storage_target,
    run_orchestrated_pipeline,
    write_jsonl,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_azure, pytest.mark.live_provider]


@pytest.fixture(params=AZURE_BLOB_AUTH_VARIANTS)
def azure_blob_auth(request: pytest.FixtureRequest) -> tuple[str, AzureStorageTarget, dict[str, object]]:
    """Resolve one auth variant's live credentials, skipping when unconfigured."""
    variant = request.param
    target = require_storage_target()
    return variant, target, auth_options(variant, target)


def test_azure_blob_sink_live_lifecycle_publishes_blob(
    azure_blob_auth: tuple[str, AzureStorageTarget, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant, target, auth = azure_blob_auth
    ensure_fingerprint_key(monkeypatch)

    row: dict[str, object] = {"id": 1, "value": f"azure-blob-sink-{variant}"}
    input_path = tmp_path / "rows.jsonl"
    write_jsonl(input_path, [row])
    blob_path = target.unique_blob_path("output.jsonl")

    pipeline: dict[str, object] = {
        "sources": {
            "input": {
                "plugin": "json",
                "on_success": "subject_sink",
                "options": jsonl_source_options(input_path),
            }
        },
        "sinks": {
            "subject_sink": {
                "plugin": "azure_blob",
                "on_write_failure": "discard",
                "options": {
                    **auth,
                    "container": target.container,
                    "blob_path": blob_path,
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                },
            }
        },
    }

    service = ambient_blob_service_client(target)
    try:
        evidence = run_orchestrated_pipeline(pipeline, tmp_path)
        assert evidence.status is RunStatus.COMPLETED
        evidence.assert_completed_lifecycle()

        # External observation: the finalized effect must be visible remotely.
        published = service.get_container_client(target.container).download_blob(blob_path).readall()
        assert [json.loads(line) for line in published.decode("utf-8").splitlines() if line.strip()] == [row]
    finally:
        delete_blob_if_present(service, target, blob_path)
        service.close()
