"""Protected live-Azure PB-09 lifecycle proof for transform:azure_prompt_shield (default).

Benign user text crosses the FULL production boundary (settings YAML ->
instantiate_plugins_from_config -> ExecutionGraph -> Orchestrator.run with a
real Landscape database) and must pass the real Azure Prompt Shield analysis
with no attack detected, preserving the row through the passthrough contract.
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
from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield

pytestmark = [pytest.mark.integration, pytest.mark.live_azure, pytest.mark.live_provider]


@pytest.fixture
def prompt_shield_credentials() -> tuple[str, str]:
    """Resolve the live Content Safety endpoint and key, skipping when unconfigured."""
    endpoint = require_resource(CONTENT_SAFETY_ENDPOINT_ENV)
    api_key = require_declared_secret(AzurePromptShield)
    return endpoint, api_key


def test_azure_prompt_shield_live_lifecycle_passes_benign_prompt(
    prompt_shield_credentials: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint, api_key = prompt_shield_credentials
    ensure_fingerprint_key(monkeypatch)

    row: dict[str, object] = {"id": 1, "text": "Please summarise the published quarterly maintenance schedule."}
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
                "plugin": "azure_prompt_shield",
                "input": "subject_input",
                "on_success": "output",
                "on_error": "discard",
                "options": {
                    "endpoint": endpoint,
                    "api_key": api_key,
                    "fields": ["text"],
                    "analysis_type": "user_prompt",
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
    # No attack detected: the passthrough row survives to the output sink.
    assert read_jsonl(output_path) == [row]
