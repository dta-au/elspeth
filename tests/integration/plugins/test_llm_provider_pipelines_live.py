"""Protected live-provider lane: ``source:llm`` and ``transform:llm`` variants.

One pytest node per production provider discriminator. The variant inventory
is derived from the owned production registries — ``LLMSource`` /
``LLMTransform`` ``discriminated_variants()`` over the ``provider`` Literal
union (``SOURCE_PROVIDER_CONFIGS`` and the transform ``_PROVIDERS`` map) — so
a registry drift changes the collected node set instead of silently testing a
stale inventory. Every node crosses the full production boundary via the
shared lane harness (settings YAML -> plugin instantiation -> ExecutionGraph
-> preflight -> public ``Orchestrator.run`` against the lane's live
PostgreSQL Landscape).

Resource vocabulary
-------------------
Provider identity (endpoints, models, region) uses only the closed lane
vocabulary from ``scripts/state_engine_assessment_lib/selectors.py``
(``PROVIDER_RESOURCES`` + ``COMMON_LIVE_RESOURCES``). That vocabulary carries
no API-key names, so key material uses the repository's established
production credential names rather than invented ones:

- ``AZURE_API_KEY`` — Azure OpenAI key (``WebConfig.server_secret_allowlist``,
  ``web/composer/provider_config.py``);
- ``OPENROUTER_API_KEY`` — OpenRouter key (examples/*, web secret inventory);
- ``GATEWAY_BEARER`` — gateway inbound bearer (the server-scoped
  ``credential_ref`` in ``deploy/aws-ecs/terraform``);
- bedrock is deliberately keyless (AWS default credential chain).

Secrets never appear in test code: the pipeline YAML carries ``${NAME}``
references resolved by the production env-expansion pass. Missing resources
skip at the variant fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts import RunStatus
from elspeth.contracts.enums import CallType
from elspeth.plugins.sources.llm.source import LLMSource
from elspeth.plugins.transforms.llm.transform import LLMTransform
from tests.integration.plugins._live_provider_lane import (
    json_output_sink,
    jsonl_input_source,
    read_output_rows,
    require_env,
    run_live_pipeline,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.live_provider]

_SOURCE_DISCRIMINATOR, _SOURCE_VARIANT_CONFIGS = LLMSource.discriminated_variants()
_TRANSFORM_DISCRIMINATOR, _TRANSFORM_VARIANT_CONFIGS = LLMTransform.discriminated_variants()
LLM_PROVIDER_VARIANTS = tuple(_SOURCE_VARIANT_CONFIGS)

# Closed lane vocabulary names (selectors.py) plus the established production
# credential names documented in the module docstring.
_VARIANT_RESOURCES: dict[str, tuple[str, ...]] = {
    "azure": ("ELSPETH_TEST_AZURE_LLM_ENDPOINT", "ELSPETH_TEST_AZURE_LLM_MODEL", "AZURE_API_KEY"),
    "openrouter": ("ELSPETH_TEST_OPENROUTER_MODEL", "OPENROUTER_API_KEY"),
    "bedrock": ("ELSPETH_TEST_BEDROCK_MODEL", "AWS_REGION"),
    "gateway": ("ELSPETH_TEST_LLM_GATEWAY_URL", "ELSPETH_TEST_LLM_GATEWAY_MODEL", "GATEWAY_BEARER"),
}


def _provider_options(variant: str) -> dict[str, Any]:
    """Provider-discriminated option fields, as ``${ENV}`` references.

    Field shapes mirror the production config unions exactly:
    ``AzureOpenAILLMSourceConfig``/``AzureOpenAIConfig`` (deployment_name +
    endpoint + api_key), ``OpenRouter*Config`` (model + api_key),
    ``Bedrock*Config`` (keyless ``bedrock/<id>`` model + region_name),
    ``Gateway*Config`` (logical model + ``/v1`` endpoint + bearer).
    """
    if variant == "azure":
        return {
            "provider": "azure",
            "deployment_name": "${ELSPETH_TEST_AZURE_LLM_MODEL}",
            "endpoint": "${ELSPETH_TEST_AZURE_LLM_ENDPOINT}",
            "api_key": "${AZURE_API_KEY}",
        }
    if variant == "openrouter":
        return {
            "provider": "openrouter",
            "model": "${ELSPETH_TEST_OPENROUTER_MODEL}",
            "api_key": "${OPENROUTER_API_KEY}",
        }
    if variant == "bedrock":
        return {
            "provider": "bedrock",
            "model": "${ELSPETH_TEST_BEDROCK_MODEL}",
            "region_name": "${AWS_REGION}",
        }
    if variant == "gateway":
        return {
            "provider": "gateway",
            "model": "${ELSPETH_TEST_LLM_GATEWAY_MODEL}",
            "endpoint": "${ELSPETH_TEST_LLM_GATEWAY_URL}",
            "api_key": "${GATEWAY_BEARER}",
        }
    raise AssertionError(f"unknown LLM provider variant {variant!r}")


@pytest.fixture
def llm_provider_variant(request: pytest.FixtureRequest) -> str:
    """One provider variant from the owned production discriminator union."""
    variant = str(request.param)
    assert _SOURCE_DISCRIMINATOR == "provider"
    assert _TRANSFORM_DISCRIMINATOR == "provider"
    assert tuple(_TRANSFORM_VARIANT_CONFIGS) == LLM_PROVIDER_VARIANTS
    assert variant in _SOURCE_VARIANT_CONFIGS
    require_env(*_VARIANT_RESOURCES[variant])
    return variant


def _assert_live_llm_evidence(evidence: Any, output_path: Path, response_field: str) -> None:
    assert evidence.status is RunStatus.COMPLETED
    rows = read_output_rows(output_path)
    assert len(rows) == 1
    response = rows[0][response_field]
    assert type(response) is str and response.strip()
    assert evidence.node_state_count > 0
    assert evidence.scheduler_event_count > 0
    assert evidence.token_outcome_count > 0
    assert CallType.LLM.value in evidence.call_types
    assert evidence.sink_effect_states and set(evidence.sink_effect_states) == {"finalized"}
    assert evidence.artifact_count > 0


@pytest.mark.parametrize("llm_provider_variant", LLM_PROVIDER_VARIANTS, indirect=True)
def test_llm_source_variant_completes_a_production_run(
    llm_provider_variant: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """A single static-prompt LLM source row crosses the full lifecycle live."""
    output_path = tmp_path / "output.jsonl"
    options: dict[str, Any] = {
        **_provider_options(llm_provider_variant),
        "prompt_template": "Reply with one short sentence about audit trails.",
        "response_field": "llm_response",
        "temperature": 0.0,
        "max_tokens": 128,
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    pipeline: dict[str, Any] = {
        "sources": {"subject_source": {"plugin": "llm", "on_success": "output", "options": options}},
        "sinks": {"output": json_output_sink(output_path)},
    }
    evidence = run_live_pipeline(request, pipeline, tmp_path)
    _assert_live_llm_evidence(evidence, output_path, "llm_response")


@pytest.mark.parametrize("llm_provider_variant", LLM_PROVIDER_VARIANTS, indirect=True)
def test_llm_transform_variant_completes_a_production_run(
    llm_provider_variant: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """A row-templated LLM transform crosses the full lifecycle live."""
    output_path = tmp_path / "output.jsonl"
    options: dict[str, Any] = {
        **_provider_options(llm_provider_variant),
        "prompt_template": "Summarise {{ row.document_text }} in one sentence.",
        "required_input_fields": ["document_text"],
        "response_field": "generated_summary",
        "temperature": 0.0,
        "max_tokens": 128,
        "schema": {"mode": "observed"},
    }
    pipeline: dict[str, Any] = {
        "sources": {
            "input": jsonl_input_source(
                tmp_path / "rows.jsonl",
                [{"document_text": "ELSPETH records every external call in its Landscape audit trail."}],
                on_success="subject_input",
                guaranteed_fields=["document_text"],
            )
        },
        "transforms": [
            {
                "name": "subject",
                "plugin": "llm",
                "input": "subject_input",
                "on_success": "output",
                "on_error": "discard",
                "options": options,
            }
        ],
        "sinks": {"output": json_output_sink(output_path)},
    }
    evidence = run_live_pipeline(request, pipeline, tmp_path)
    _assert_live_llm_evidence(evidence, output_path, "generated_summary")
