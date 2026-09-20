"""RAG retrieval transform — enriches rows with context from vector/keyword search.

The provider-neutral work (query, search, formatting, output fields,
telemetry) lives in ``core.RetrievalTransformBase``. This module selects the
provider from the PROVIDERS registry and runs its on_start readiness check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from elspeth.contracts import Determinism, TransformResult
from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.errors import FrameworkBugError, RetrievalNotReadyError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import ContentTrust
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from elspeth.plugins.transforms.rag.config import PROVIDERS, RAGRetrievalConfig
from elspeth.plugins.transforms.rag.core import RetrievalTransformBase

if TYPE_CHECKING:
    from elspeth.contracts.contexts import LifecycleContext
    from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalProvider, RetrievalSearcher


class RAGRetrievalTransform(RetrievalTransformBase):
    """Enriches rows with retrieval-augmented context from search providers.

    Registered as plugin name="rag_retrieval". Uses synchronous process()
    since retrieval calls are I/O-bound but single-query-per-row.

    Output fields (prefixed with output_prefix):
        {prefix}__rag_context: Formatted text from retrieved chunks.
        {prefix}__rag_score: Best relevance score (float, 0.0-1.0).
        {prefix}__rag_count: Number of chunks retrieved (int).
        {prefix}__rag_sources: JSON envelope with source provenance.
    """

    name = "rag_retrieval"
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:505b39c4063685ec"
    determinism: Determinism = Determinism.EXTERNAL_CALL
    config_model = RAGRetrievalConfig
    passes_through_input = True
    content_trust = ContentTrust.UNTRUSTED
    capability_tags: tuple[str, ...] = ("rag", "retrieval", "vector-search")

    usage_when_to_use = (
        "Use to retrieve ranked, provenance-bearing context from an existing Chroma collection or "
        "Azure Search index. Retrieved text is untrusted before LLM consumption even when it comes "
        "from an approved index."
    )
    usage_when_not_to_use = (
        "Not for corpus indexing or answer generation: populate the collection with chroma_sink or "
        "an operator-managed indexer, and add an llm transform separately when an answer is required."
    )
    example_use = (
        "transform:\n"
        "  plugin: rag_retrieval\n"
        "  options:\n"
        "    output_prefix: policy\n"
        "    query_field: question\n"
        "    provider: azure_search\n"
        "    provider_config:\n"
        "      endpoint: https://catalogue-reference.search.windows.net\n"
        "      index: approved-documents\n"
        "      api_key: {secret_ref: AZURE_SEARCH_API_KEY}\n"
        "      search_mode: hybrid\n"
        "    top_k: 5\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal no-network config for the ADR-009 forward invariant."""
        return {
            "output_prefix": "policy",
            "query_field": "rag_probe_query",
            "provider": "azure_search",
            "provider_config": {
                "endpoint": "https://invariant.example.search.windows.net",
                "index": "invariant-probe",
                "api_key": "probe-key",
            },
            "schema": {"mode": "observed"},
        }

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject a deterministic retrieval query for invariant probing."""
        return [
            self._augment_invariant_probe_row(
                probe,
                field_name="rag_probe_query",
                value="What is the policy?",
            )
        ]

    def execute_forward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: Any,
    ) -> TransformResult:
        """Exercise the real retrieval path with a provider-agnostic local double."""
        if len(probe_rows) != 1:
            raise FrameworkBugError(
                f"{self.__class__.__name__}.execute_forward_invariant_probe() "
                f"received {len(probe_rows)} rows; RAG invariant probes require exactly 1 row."
            )

        class _InvariantProvider:
            def __init__(self) -> None:
                self.last_skipped_count = 0
                self.last_skipped_reasons: list[dict[str, Any]] = []

            def search(
                self,
                query: str,
                top_k: int,
                min_score: float,
                *,
                state_id: str,
                token_id: str | None,
                member_token: WorkerMembershipToken,
                work_item: TokenWorkItem,
            ) -> list[RetrievalChunk]:
                del query, top_k, min_score, state_id, token_id
                return [
                    RetrievalChunk(
                        content="Probe context",
                        score=0.95,
                        source_id="probe-doc",
                        metadata={"kind": "invariant"},
                    )
                ]

            def close(self) -> None:
                return None

        original_provider = self._searcher
        original_on_start_called = self._on_start_called
        probe_provider = _InvariantProvider()
        try:
            self.__dict__["_searcher"] = probe_provider
            self._on_start_called = True
            return super().execute_forward_invariant_probe(probe_rows, ctx)
        finally:
            self._on_start_called = original_on_start_called
            self.__dict__["_searcher"] = original_provider
            probe_provider.close()

    def __init__(self, config: dict[str, Any]) -> None:
        self._rag_config = RAGRetrievalConfig.from_dict(config, plugin_name=self.name)
        super().__init__(config, self._rag_config)
        self._provider_label = self._rag_config.provider

    def _build_searcher(self, ctx: LifecycleContext) -> RetrievalSearcher:
        """Construct the provider from the registry and refuse an unready collection."""
        provider_name = self._rag_config.provider
        config_cls, factory = PROVIDERS[provider_name]
        provider_config = config_cls(**self._rag_config.provider_config)
        collection_name = self._configured_collection_name(provider_name, provider_config)

        try:
            provider: RetrievalProvider = factory(
                provider_config,
                execution=ctx.landscape,
                run_id=ctx.run_id,
                telemetry_emit=ctx.telemetry_emit,
                limiter=(ctx.rate_limit_registry.get_limiter(provider_name) if ctx.rate_limit_registry is not None else None),
            )
            # Held before the readiness check so close() releases a provider
            # whose collection turns out not to be ready.
            self._searcher = provider

            # Readiness check — refuse to start against empty/missing collection.
            # Two distinct failure modes: unreachable (infra problem) and empty
            # (operator error). Both crash startup, but the message distinguishes them.
            readiness = provider.check_readiness()
        except RetrievalError as exc:
            self._record_readiness_check(
                ctx,
                collection=collection_name,
                reachable=False,
                count=None,
                message=str(exc),
            )
            raise RetrievalNotReadyError(collection=collection_name, reason=str(exc)) from exc

        # Record first — the readiness result is an auditable fact regardless of outcome.
        # "If it's not recorded, it didn't happen" — an auditor can query
        # what the collection state was when this pipeline started, including failures.
        self._record_readiness_check(
            ctx,
            collection=readiness.collection,
            reachable=readiness.reachable,
            count=readiness.count,
            message=readiness.message,
        )

        # Then guard on the result
        if not readiness.reachable or readiness.count is None or readiness.count <= 0:
            raise RetrievalNotReadyError(
                collection=readiness.collection,
                reason=readiness.message,
            )
        return provider

    def _configured_collection_name(self, provider_name: str, provider_config: Any) -> str:
        """Read readiness identity from the nominal config for a known provider."""
        if provider_name == "chroma":
            from elspeth.plugins.infrastructure.clients.retrieval.chroma import ChromaSearchProviderConfig

            if not isinstance(provider_config, ChromaSearchProviderConfig):
                raise FrameworkBugError(
                    f"{self.__class__.__name__} provider chroma requires ChromaSearchProviderConfig; "
                    f"received {type(provider_config).__name__}."
                )
            return provider_config.collection
        if provider_name == "azure_search":
            from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchProviderConfig

            if not isinstance(provider_config, AzureSearchProviderConfig):
                raise FrameworkBugError(
                    f"{self.__class__.__name__} provider azure_search requires AzureSearchProviderConfig; "
                    f"received {type(provider_config).__name__}."
                )
            return provider_config.index
        raise FrameworkBugError(f"{self.__class__.__name__} has no readiness identity contract for provider {provider_name!r}.")

    def _record_readiness_check(
        self,
        ctx: LifecycleContext,
        *,
        collection: str,
        reachable: bool,
        count: int | None,
        message: str,
    ) -> None:
        """Persist readiness for every audit-enabled leader or follower context."""
        if ctx.landscape is not None:
            ctx.record_readiness_check(
                name=self.name,
                collection=collection,
                reachable=reachable,
                count=count,
                message=message,
            )

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name="rag_retrieval",
                issue_code=None,
                summary="Vector retrieval against a configured backend (Chroma, etc). Builds a query from row fields, returns ranked chunks for downstream LLM grounding.",
                composer_hints=(
                    "Name the Chroma collection in provider_config.collection or the Azure Search index in provider_config.index.",
                    "Azure Search reads provider_config.content_field and id_field (defaults content / id); an index built by the portal import wizard needs chunk / chunk_id. Set title_field and url_field to emit source_name and source_link citation metadata.",
                    "Azure Search vector and hybrid modes send the query as text, so the index must define an integrated vectorizer on vector_field; semantic mode needs semantic_config and scores by the 0-4 reranker score.",
                    "Query template uses row-field interpolation; document what fields are read so downstream consumers can wire them.",
                    "top_k and min_score interact — high min_score plus low top_k may return zero chunks. Configure on_no_results to handle the empty-result case.",
                    "The transform emits running mean/variance telemetry for retrieval scores — watch these to catch retrieval-quality regressions.",
                ),
            )
        return None
