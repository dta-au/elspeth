"""Protected live-provider lane: the ``azure_ai_search`` transform's variants.

One pytest node per production ``AzureSearchAuthMode`` discriminator
(``api_key`` / ``managed_identity``), projected to the golden variant ids
exactly the way ``scripts/state_engine_plugin_matrix._variant_map`` does
(``_`` -> ``-``). Every node crosses the full production boundary via the
shared lane harness.

The nodes are read-only against the operator-provisioned index and use the
``*`` match-all keyword query, so a live non-empty index deterministically
yields results.

Resource vocabulary
-------------------
Endpoint and index identity use only the closed lane vocabulary from
``scripts/state_engine_assessment_lib/selectors.py`` (``PROVIDER_RESOURCES``
+ ``COMMON_LIVE_RESOURCES``). The ``api-key`` variant reads
``AZURE_SEARCH_API_KEY``, the credential name the plugin's own
``example_use`` establishes; the ``managed-identity`` variant is keyless and
runs only on an Azure host that has a managed identity
(``ManagedIdentityCredential``, no fallback chain). Secrets never appear in
test code: the pipeline YAML carries ``${NAME}`` references resolved by the
production env-expansion pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from elspeth.contracts import RunStatus
from elspeth.contracts.enums import CallType
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchAuthMode
from tests.integration.plugins._live_provider_lane import (
    json_output_sink,
    jsonl_input_source,
    read_output_rows,
    require_env,
    run_live_pipeline,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.live_provider]

AZURE_AI_SEARCH_VARIANTS = tuple(mode.replace("_", "-") for mode in get_args(AzureSearchAuthMode))

_VARIANT_RESOURCES: dict[str, tuple[str, ...]] = {
    "api-key": (
        "ELSPETH_TEST_AZURE_SEARCH_ENDPOINT",
        "ELSPETH_TEST_AZURE_SEARCH_INDEX",
        "AZURE_SEARCH_API_KEY",
    ),
    "managed-identity": (
        "ELSPETH_TEST_AZURE_SEARCH_ENDPOINT",
        "ELSPETH_TEST_AZURE_SEARCH_INDEX",
    ),
}

_AUTH_OPTIONS: dict[str, dict[str, Any]] = {
    "api-key": {"api_key": "${AZURE_SEARCH_API_KEY}"},
    "managed-identity": {"use_managed_identity": True},
}


@pytest.fixture
def azure_ai_search_variant(request: pytest.FixtureRequest) -> str:
    """One azure_ai_search variant from the owned production discriminator."""
    variant = str(request.param)
    assert set(AZURE_AI_SEARCH_VARIANTS) == set(_VARIANT_RESOURCES) == set(_AUTH_OPTIONS)
    assert variant in AZURE_AI_SEARCH_VARIANTS
    require_env(*_VARIANT_RESOURCES[variant])
    return variant


@pytest.mark.parametrize("azure_ai_search_variant", AZURE_AI_SEARCH_VARIANTS, indirect=True)
def test_azure_ai_search_variant_completes_a_production_run(
    azure_ai_search_variant: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """One Azure-AI-Search-enriched row crosses the full production lifecycle live."""
    output_path = tmp_path / "output.jsonl"
    options: dict[str, Any] = {
        "output_prefix": "retrieval",
        "query_field": "question",
        "endpoint": "${ELSPETH_TEST_AZURE_SEARCH_ENDPOINT}",
        "index": "${ELSPETH_TEST_AZURE_SEARCH_INDEX}",
        **_AUTH_OPTIONS[azure_ai_search_variant],
        "search_mode": "keyword",
        "top_k": 3,
        "min_score": 0.0,
        "on_no_results": "continue",
        "schema": {"mode": "observed"},
    }
    pipeline: dict[str, Any] = {
        "sources": {
            "input": jsonl_input_source(
                tmp_path / "rows.jsonl",
                [{"question": "*"}],
                on_success="subject_input",
            )
        },
        "transforms": [
            {
                "name": "subject",
                "plugin": "azure_ai_search",
                "input": "subject_input",
                "on_success": "output",
                "on_error": "discard",
                "options": options,
            }
        ],
        "sinks": {"output": json_output_sink(output_path)},
    }
    evidence = run_live_pipeline(request, pipeline, tmp_path)

    assert evidence.status is RunStatus.COMPLETED
    rows = read_output_rows(output_path)
    assert len(rows) == 1
    retrieved_count = rows[0]["retrieval__rag_count"]
    assert type(retrieved_count) is int and retrieved_count >= 1
    context = rows[0]["retrieval__rag_context"]
    assert type(context) is str and context.strip()
    assert evidence.node_state_count > 0
    assert evidence.scheduler_event_count > 0
    assert evidence.token_outcome_count > 0
    assert CallType.HTTP.value in evidence.call_types
    assert evidence.sink_effect_states and set(evidence.sink_effect_states) == {"finalized"}
    assert evidence.artifact_count > 0
