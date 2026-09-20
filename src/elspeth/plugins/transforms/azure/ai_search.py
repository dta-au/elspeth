"""Azure AI Search RAG retrieval transform.

The provider-neutral work lives in ``rag.core.RetrievalTransformBase``. This
plugin owns the Azure options (typed and top-level, so the composer's planner
sees them) and the readiness path: the index count probe runs through the
platform's ``runtime_preflight`` facility as an audited, retried operation.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import Field, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.errors import FrameworkBugError, RuntimePreflightFailedError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import ContentTrust, WebConfigAuthority
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchProvider, AzureSearchProviderConfig
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig, RetrievalTransformBase

if TYPE_CHECKING:
    from elspeth.contracts.contexts import LifecycleContext


class AzureAISearchConfig(RetrievalOutputConfig):
    """Options for the azure_ai_search transform."""

    endpoint: str = Field(description="Azure AI Search service URL, https://<service>.search.windows.net.")
    index: str = Field(description="Name of the existing index to query.")
    api_key: str | None = Field(default=None, description="Query key. Mutually exclusive with use_managed_identity.")
    use_managed_identity: bool = Field(default=False, description="Authenticate with the host's managed identity.")
    client_id: str | None = Field(default=None, description="Client id of a user-assigned managed identity.")
    api_version: str = Field(default="2024-07-01", description="Azure AI Search REST API version.")
    search_mode: Literal["vector", "keyword", "hybrid", "semantic"] = Field(
        default="hybrid",
        description="vector and hybrid need an integrated vectorizer on field_vector; semantic needs semantic_config.",
    )
    request_timeout: float = Field(default=30.0, gt=0, description="Per-request timeout in seconds.")
    # Index fields are spelled ``field_*``: a ``*_field`` option is, by the base class's
    # convention, the name of a row column this transform reads.
    field_vector: str = Field(default="contentVector", description="Index vector field queried by vector and hybrid modes.")
    semantic_config: str | None = Field(default=None, description="Semantic configuration name, required for semantic mode.")
    field_content: str = Field(default="content", description="Retrievable index string field holding the chunk text.")
    field_id: str = Field(default="id", description="Retrievable index key field.")
    field_title: str | None = Field(default=None, description="Optional index field emitted as source_name citation metadata.")
    field_url: str | None = Field(default=None, description="Optional index field emitted as source_link citation metadata.")
    select: tuple[str, ...] | None = Field(default=None, description="Fields to return; must include every mapped field.")
    filter: str | None = Field(default=None, description="Literal OData $filter sent with every query.")

    def provider_config(self) -> AzureSearchProviderConfig:
        """The provider's own config model, which owns every Azure validation rule."""
        return AzureSearchProviderConfig(
            endpoint=self.endpoint,
            index=self.index,
            api_key=self.api_key,
            use_managed_identity=self.use_managed_identity,
            client_id=self.client_id,
            api_version=self.api_version,
            search_mode=self.search_mode,
            request_timeout=self.request_timeout,
            vector_field=self.field_vector,
            semantic_config=self.semantic_config,
            content_field=self.field_content,
            id_field=self.field_id,
            title_field=self.field_title,
            url_field=self.field_url,
            select=self.select,
            filter=self.filter,
        )

    @model_validator(mode="after")
    def validate_provider_options(self) -> Self:
        self.provider_config()
        return self


class AzureAISearchTransform(RetrievalTransformBase):
    """RAG retrieval against an existing Azure AI Search index."""

    name = "azure_ai_search"
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:cf98a8deff5a5b6a"
    determinism: Determinism = Determinism.EXTERNAL_CALL
    config_model = AzureAISearchConfig
    passes_through_input = True
    content_trust = ContentTrust.UNTRUSTED
    # A web author never chooses where a server credential is sent: the web
    # layer requires an operator profile that binds the endpoint and identity.
    web_config_authority = WebConfigAuthority.OPERATOR_PROFILED
    requires_runtime_preflight = True
    capability_tags: tuple[str, ...] = ("rag", "retrieval", "vector-search", "azure")
    _provider_label = "azure_ai_search"

    usage_when_to_use = (
        "Use for RAG retrieval against an existing Azure AI Search index: it returns ranked, "
        "provenance-bearing context for a downstream llm transform. Retrieved text is untrusted "
        "before LLM consumption even when it comes from an approved index."
    )
    usage_when_not_to_use = (
        "Not for indexing documents or generating answers: populate the index with Azure's own indexer, "
        "and add an llm transform after retrieval when an answer is required. Use rag_retrieval for Chroma."
    )
    example_use = (
        "transform:\n"
        "  plugin: azure_ai_search\n"
        "  options:\n"
        "    output_prefix: policy\n"
        "    query_field: question\n"
        "    endpoint: https://catalogue-reference.search.windows.net\n"
        "    index: approved-documents\n"
        "    api_key: {secret_ref: AZURE_SEARCH_API_KEY}\n"
        "    search_mode: hybrid\n"
        "    top_k: 5\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal no-network config for the ADR-009 forward invariant."""
        return {
            "output_prefix": "policy",
            "query_field": "rag_probe_query",
            "endpoint": "https://invariant.example.search.windows.net",
            "index": "invariant-probe",
            "api_key": "probe-key",
            "schema": {"mode": "observed"},
        }

    def __init__(self, config: dict[str, Any]) -> None:
        self._search_config = AzureAISearchConfig.from_dict(config, plugin_name=self.name)
        super().__init__(config, self._search_config)
        # One bucket per Search service: the host maps one-to-one to a service,
        # and a profile alias is deliberately absent from the options seen here.
        hostname = urllib.parse.urlparse(self._search_config.endpoint).hostname
        self.limiter_service_name = f"azure_ai_search:{hostname}"

    def _build_searcher(self, ctx: LifecycleContext) -> AzureSearchProvider:
        if ctx.landscape is None:
            raise FrameworkBugError("AzureAISearchTransform requires landscape — orchestrator must inject it before on_start().")
        return AzureSearchProvider(
            self._search_config.provider_config(),
            execution=ctx.landscape,
            run_id=ctx.run_id,
            telemetry_emit=ctx.telemetry_emit,
            limiter=(ctx.rate_limit_registry.get_limiter(self.limiter_service_name) if ctx.rate_limit_registry is not None else None),
        )

    def runtime_preflight(self, ctx: LifecycleContext) -> None:
        """Refuse to load rows against a missing, empty or unreachable index."""
        searcher = self._searcher
        if searcher is None:
            raise FrameworkBugError("AzureAISearchTransform runtime_preflight called before provider initialization")
        if not isinstance(searcher, AzureSearchProvider):
            raise FrameworkBugError(f"AzureAISearchTransform requires an AzureSearchProvider; received {type(searcher).__name__}.")
        if ctx.operation_id is None:
            raise FrameworkBugError("AzureAISearchTransform runtime_preflight requires an operation audit parent")
        try:
            readiness = searcher.runtime_preflight(operation_id=ctx.operation_id, coordination_token=ctx.require_coordination_token())
        except RetrievalError as exc:
            raise RuntimePreflightFailedError(plugin_name=self.name, provider=self._provider_label, cause=exc) from exc
        if readiness.count is None or readiness.count <= 0:
            raise RuntimePreflightFailedError(
                plugin_name=self.name,
                provider=self._provider_label,
                cause=RetrievalError(readiness.message, retryable=False),
            )

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name="azure_ai_search",
                issue_code=None,
                summary=(
                    "RAG retrieval against an Azure AI Search index. Builds a query from row fields and "
                    "returns ranked, cited chunks for downstream LLM grounding."
                ),
                composer_hints=(
                    "This is the Azure RAG plugin: pair it with an llm transform that reads {output_prefix}__rag_context.",
                    "field_content and field_id name INDEX fields and default to content / id; an index built by the portal import wizard needs chunk / chunk_id. Set field_title and field_url to emit source_name and source_link citation metadata.",
                    "vector and hybrid modes send the query as text, so the index must define an integrated vectorizer on field_vector; semantic mode needs semantic_config and scores by the 0-4 reranker score.",
                    "Query template uses row-field interpolation; document what fields are read so downstream consumers can wire them.",
                    "top_k and min_score interact — high min_score plus low top_k may return zero chunks. Configure on_no_results to handle the empty-result case.",
                ),
            )
        return None
