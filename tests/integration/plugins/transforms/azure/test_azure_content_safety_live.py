"""Protected live-Azure PB-09 lifecycle proof for transform:azure_content_safety (default).

Benign text crosses the FULL production boundary (settings YAML ->
instantiate_plugins_from_config -> ExecutionGraph -> Orchestrator.run with a
real Landscape database) and must pass the real Azure Content Safety analysis
unblocked, preserving the row through the passthrough contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.plugins._azure_live import (
    CONTENT_SAFETY_ENDPOINT_ENV,
    ensure_fingerprint_key,
    jsonl_source_options,
    local_jsonl_sink_options,
    read_jsonl,
    require_declared_secret,
    require_resource,
    run_orchestrated_pipeline,
    write_jsonl,
)

from elspeth.contracts import RunStatus
from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety

pytestmark = [pytest.mark.integration, pytest.mark.live_azure, pytest.mark.live_provider]


@pytest.fixture
def content_safety_credentials() -> tuple[str, str]:
    """Resolve the live Content Safety endpoint and key, skipping when unconfigured."""
    endpoint = require_resource(CONTENT_SAFETY_ENDPOINT_ENV)
    api_key = require_declared_secret(AzureContentSafety)
    return endpoint, api_key


def test_azure_content_safety_live_lifecycle_passes_benign_text(
    content_safety_credentials: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint, api_key = content_safety_credentials
    ensure_fingerprint_key(monkeypatch)

    row: dict[str, object] = {"id": 1, "text": "The archive contains the quarterly maintenance schedule."}
    input_path = tmp_path / "rows.jsonl"
    write_jsonl(input_path, [row])
    output_path = tmp_path / "output.jsonl"

    pipeline: dict[str, object] = {
        "sources": {
            "input": {
                "plugin": "json",
                "on_success": "subject_input",
                "options": jsonl_source_options(input_path),
            }
        },
        "transforms": [
            {
                "name": "subject",
                "plugin": "azure_content_safety",
                "input": "subject_input",
                "on_success": "output",
                "on_error": "discard",
                "options": {
                    "endpoint": endpoint,
                    "api_key": api_key,
                    "fields": ["text"],
                    "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2},
                    "schema": {"mode": "observed"},
                },
            }
        ],
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
    # Benign text must be validated, not blocked: the passthrough row survives.
    assert read_jsonl(output_path) == [row]
