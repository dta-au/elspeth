"""Protected live-Azure PB-09 lifecycle proof for transform:azure_content_understanding (default).

An operator-approved document URL crosses the FULL production boundary
(settings YAML -> instantiate_plugins_from_config -> ExecutionGraph ->
Orchestrator.run with a real Landscape database).  The real asynchronous
analyze/poll flow against the configured analyzer must succeed and enrich the
row with markdown, page count and bounded metadata.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.plugins._azure_live import (
    CONTENT_UNDERSTANDING_ANALYZER_ENV,
    CONTENT_UNDERSTANDING_ENDPOINT_ENV,
    DOCUMENT_URL_ENV,
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
from elspeth.plugins.transforms.azure.content_understanding import AzureContentUnderstanding

pytestmark = [pytest.mark.integration, pytest.mark.live_azure, pytest.mark.live_provider]


@pytest.fixture
def content_understanding_inputs() -> tuple[str, str, str, str]:
    """Resolve the live Content Understanding inputs, skipping when unconfigured."""
    endpoint = require_resource(CONTENT_UNDERSTANDING_ENDPOINT_ENV)
    api_key = require_declared_secret(AzureContentUnderstanding)
    analyzer_id = require_resource(CONTENT_UNDERSTANDING_ANALYZER_ENV)
    document_url = require_resource(DOCUMENT_URL_ENV)
    return endpoint, api_key, analyzer_id, document_url


def test_azure_content_understanding_live_lifecycle_extracts_markdown(
    content_understanding_inputs: tuple[str, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint, api_key, analyzer_id, document_url = content_understanding_inputs
    ensure_fingerprint_key(monkeypatch)

    row: dict[str, object] = {"id": 1, "document_url": document_url}
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
                "plugin": "azure_content_understanding",
                "input": "subject_input",
                "on_success": "output",
                "on_error": "discard",
                "options": {
                    "endpoint": endpoint,
                    "api_key": api_key,
                    "analyzer_id": analyzer_id,
                    "source_mode": "url",
                    "source_field": "document_url",
                    "content_field": "cu_markdown",
                    "page_count_field": "cu_page_count",
                    "metadata_field": "cu_meta",
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

    output_rows = read_jsonl(output_path)
    assert len(output_rows) == 1
    enriched = output_rows[0]
    assert enriched["id"] == 1
    assert enriched["document_url"] == document_url
    markdown = enriched["cu_markdown"]
    assert type(markdown) is str and markdown.strip()
    page_count = enriched["cu_page_count"]
    assert type(page_count) is int and page_count > 0
    metadata = enriched["cu_meta"]
    assert type(metadata) is dict
    assert metadata["api_version"] == "2025-11-01"
    assert metadata["result_status"] == "succeeded"
    assert type(metadata["operation_id"]) is str
