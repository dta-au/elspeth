"""Protected live-provider lane: ``transform:rag_retrieval`` provider variants.

One pytest node per production retrieval discriminator. The variant inventory
derives from the owned production vocabularies — the ``RAGRetrievalConfig``
``provider`` Literal (``RetrievalProviderName``) validated against the
``PROVIDERS`` registry, ``AzureSearchAuthMode`` (``api_key`` /
``managed_identity``), and ``ChromaSearchMode`` (``ephemeral`` /
``persistent`` / ``client``) — projected to the golden variant ids exactly the
way ``scripts/state_engine_plugin_matrix._variant_map`` does
(``azure-search-<mode>`` with ``_`` -> ``-``, ``chroma-<mode>``). Every node
crosses the full production boundary via the shared lane harness.

The RAG transform's ``on_start`` readiness gate refuses an empty or missing
collection, so each Chroma node seeds its own uniquely named collection
through the same client mechanism the production provider uses (the shared
in-process ephemeral client, the same persistent directory, or the remote
HTTP server) and deletes it in ``finally``. Azure AI Search nodes are
read-only against the operator-provisioned index and use the ``*`` match-all
keyword query, so a live non-empty index deterministically yields results.

Resource vocabulary
-------------------
Endpoint/index/host identity uses only the closed lane vocabulary from
``scripts/state_engine_assessment_lib/selectors.py`` (``PROVIDER_RESOURCES``
+ ``COMMON_LIVE_RESOURCES``). That vocabulary carries no API-key names, so
the ``azure-search-api-key`` variant uses ``AZURE_SEARCH_API_KEY`` — the
credential name this plugin's own ``example_use`` establishes — rather than
an invented name; the managed-identity variant is keyless (ambient Azure
credential chain). Secrets never appear in test code: the pipeline YAML
carries ``${NAME}`` references resolved by the production env-expansion pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import chromadb
import chromadb.api
import pytest

from elspeth.contracts import RunStatus
from elspeth.contracts.enums import CallType
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchAuthMode
from elspeth.plugins.infrastructure.clients.retrieval.connection import ChromaSearchMode
from elspeth.plugins.transforms.rag.config import PROVIDERS, RetrievalProviderName
from tests.integration.plugins._live_provider_lane import (
    json_output_sink,
    jsonl_input_source,
    read_output_rows,
    require_env,
    run_live_pipeline,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.live_provider]

_AZURE_SEARCH_MODES = get_args(AzureSearchAuthMode)
_CHROMA_MODES = get_args(ChromaSearchMode)
RAG_VARIANTS = (
    *(f"azure-search-{mode.replace('_', '-')}" for mode in _AZURE_SEARCH_MODES),
    *(f"chroma-{mode}" for mode in _CHROMA_MODES),
)

_VARIANT_RESOURCES: dict[str, tuple[str, ...]] = {
    "azure-search-api-key": (
        "ELSPETH_TEST_AZURE_SEARCH_ENDPOINT",
        "ELSPETH_TEST_AZURE_SEARCH_INDEX",
        "AZURE_SEARCH_API_KEY",
    ),
    "azure-search-managed-identity": (
        "ELSPETH_TEST_AZURE_SEARCH_ENDPOINT",
        "ELSPETH_TEST_AZURE_SEARCH_INDEX",
    ),
    "chroma-ephemeral": (),
    "chroma-persistent": (),
    "chroma-client": (
        "ELSPETH_TEST_CHROMA_HOST",
        "ELSPETH_TEST_CHROMA_PORT",
        "ELSPETH_TEST_CHROMA_SSL",
        "ELSPETH_TEST_CHROMA_COLLECTION_PREFIX",
    ),
}

_SEED_DOCUMENT = "ELSPETH state-engine live lane corpus document about pipeline audit evidence."
_FILLER_DOCUMENT = "An unrelated filler passage recorded only to give the collection a second member."


@dataclass(frozen=True, slots=True)
class _PreparedRetrieval:
    """One variant's provider config, query, expectations, and teardown."""

    provider: RetrievalProviderName
    provider_config: dict[str, Any]
    query_text: str
    expect_seeded_hit: bool
    expected_call_type: str
    cleanup: Callable[[], None]


def _seed_chroma_collection(client: chromadb.api.ClientAPI, collection_name: str) -> None:
    """Create and populate a uniquely named cosine collection for retrieval."""
    collection = client.create_collection(name=collection_name, metadata={"hnsw:space": "cosine"})
    collection.add(
        ids=["live-lane-doc-1", "live-lane-doc-2"],
        documents=[_SEED_DOCUMENT, _FILLER_DOCUMENT],
        metadatas=[{"origin": "state-engine-live-lane"}, {"origin": "state-engine-live-lane"}],
    )


def _prepare_retrieval(variant: str, tmp_path: Path) -> _PreparedRetrieval:
    if variant == "azure-search-api-key":
        return _PreparedRetrieval(
            provider="azure_search",
            provider_config={
                "endpoint": "${ELSPETH_TEST_AZURE_SEARCH_ENDPOINT}",
                "index": "${ELSPETH_TEST_AZURE_SEARCH_INDEX}",
                "auth_mode": "api_key",
                "api_key": "${AZURE_SEARCH_API_KEY}",
                "search_mode": "keyword",
            },
            query_text="*",
            expect_seeded_hit=False,
            expected_call_type=CallType.HTTP.value,
            cleanup=lambda: None,
        )
    if variant == "azure-search-managed-identity":
        return _PreparedRetrieval(
            provider="azure_search",
            provider_config={
                "endpoint": "${ELSPETH_TEST_AZURE_SEARCH_ENDPOINT}",
                "index": "${ELSPETH_TEST_AZURE_SEARCH_INDEX}",
                "auth_mode": "managed_identity",
                "use_managed_identity": True,
                "search_mode": "keyword",
            },
            query_text="*",
            expect_seeded_hit=False,
            expected_call_type=CallType.HTTP.value,
            cleanup=lambda: None,
        )

    collection_name = f"elspeth-live-{uuid4().hex[:12]}"
    if variant == "chroma-ephemeral":
        # The production provider constructs ``chromadb.Client()`` for
        # ephemeral mode; identical default settings share one in-process
        # system, so seeding through the same constructor targets the exact
        # store the pipeline will read.
        client: chromadb.api.ClientAPI = chromadb.Client()
        _seed_chroma_collection(client, collection_name)
        return _PreparedRetrieval(
            provider="chroma",
            provider_config={"collection": collection_name, "mode": "ephemeral"},
            query_text=_SEED_DOCUMENT,
            expect_seeded_hit=True,
            expected_call_type=CallType.VECTOR.value,
            cleanup=lambda: client.delete_collection(collection_name),
        )
    if variant == "chroma-persistent":
        persist_directory = tmp_path / "chroma-persistent"
        client = chromadb.PersistentClient(path=str(persist_directory))
        _seed_chroma_collection(client, collection_name)
        return _PreparedRetrieval(
            provider="chroma",
            provider_config={
                "collection": collection_name,
                "mode": "persistent",
                "persist_directory": str(persist_directory),
            },
            query_text=_SEED_DOCUMENT,
            expect_seeded_hit=True,
            expected_call_type=CallType.VECTOR.value,
            cleanup=lambda: client.delete_collection(collection_name),
        )
    if variant == "chroma-client":
        values = require_env(*_VARIANT_RESOURCES["chroma-client"])
        host = values["ELSPETH_TEST_CHROMA_HOST"]
        port = int(values["ELSPETH_TEST_CHROMA_PORT"])
        ssl = values["ELSPETH_TEST_CHROMA_SSL"].strip().lower() in {"1", "true", "yes", "on"}
        remote_collection = f"{values['ELSPETH_TEST_CHROMA_COLLECTION_PREFIX']}{uuid4().hex[:12]}"
        client = chromadb.HttpClient(host=host, port=port, ssl=ssl)
        _seed_chroma_collection(client, remote_collection)
        return _PreparedRetrieval(
            provider="chroma",
            provider_config={
                "collection": remote_collection,
                "mode": "client",
                "host": host,
                "port": port,
                "ssl": ssl,
            },
            query_text=_SEED_DOCUMENT,
            expect_seeded_hit=True,
            expected_call_type=CallType.VECTOR.value,
            cleanup=lambda: client.delete_collection(remote_collection),
        )
    raise AssertionError(f"unknown rag_retrieval variant {variant!r}")


@pytest.fixture
def rag_variant(request: pytest.FixtureRequest) -> str:
    """One retrieval variant from the owned production discriminators."""
    variant = str(request.param)
    assert set(get_args(RetrievalProviderName)) == {"azure_search", "chroma"}
    assert variant in RAG_VARIANTS
    provider_key = "azure_search" if variant.startswith("azure-search-") else "chroma"
    if provider_key not in PROVIDERS:
        pytest.skip(f"retrieval provider {provider_key!r} SDK is not installed in this environment")
    require_env(*_VARIANT_RESOURCES[variant])
    return variant


@pytest.mark.parametrize("rag_variant", RAG_VARIANTS, indirect=True)
def test_rag_retrieval_variant_completes_a_production_run(
    rag_variant: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """One retrieval-enriched row crosses the full production lifecycle live."""
    prepared = _prepare_retrieval(rag_variant, tmp_path)
    try:
        output_path = tmp_path / "output.jsonl"
        options: dict[str, Any] = {
            "output_prefix": "retrieval",
            "query_field": "question",
            "provider": prepared.provider,
            "provider_config": prepared.provider_config,
            "top_k": 3,
            "min_score": 0.0,
            "on_no_results": "continue",
            "schema": {"mode": "observed"},
        }
        pipeline: dict[str, Any] = {
            "sources": {
                "input": jsonl_input_source(
                    tmp_path / "rows.jsonl",
                    [{"question": prepared.query_text}],
                    on_success="subject_input",
                )
            },
            "transforms": [
                {
                    "name": "subject",
                    "plugin": "rag_retrieval",
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
        if prepared.expect_seeded_hit:
            assert _SEED_DOCUMENT in context
        assert evidence.node_state_count > 0
        assert evidence.scheduler_event_count > 0
        assert evidence.token_outcome_count > 0
        assert prepared.expected_call_type in evidence.call_types
        assert evidence.sink_effect_states and set(evidence.sink_effect_states) == {"finalized"}
        assert evidence.artifact_count > 0
    finally:
        prepared.cleanup()
