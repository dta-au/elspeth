"""Configuration for RAG retrieval transform."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import Field, model_validator

from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig

if TYPE_CHECKING:
    from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalProvider

ProviderFactory = Callable[..., "RetrievalProvider"]
RetrievalProviderName = Literal["azure_search", "chroma"]

# Registry entry: (config class, provider class or factory callable)
_ProviderEntry = tuple[type[Any], Callable[..., Any]]


def _get_providers() -> dict[str, _ProviderEntry]:
    """Lazy provider registry — only imports providers whose deps are installed.

    Only the ABSENCE of a provider's optional third-party SDK removes it from
    the registry. A first-party retrieval module that fails its own import —
    or an installed SDK missing a transitive dependency — is a broken install
    and propagates rather than reading as "provider unavailable"
    (the same posture as ``read_litellm_model_list``).
    """
    providers: dict[str, _ProviderEntry] = {}

    # azure_search speaks the Azure Search REST API through httpx (a core
    # dependency) and needs no optional SDK: it is unconditionally available,
    # and any import failure here is a first-party bug that must surface.
    from elspeth.plugins.infrastructure.clients.retrieval.azure_search import (
        AzureSearchProvider,
        AzureSearchProviderConfig,
    )

    providers["azure_search"] = (AzureSearchProviderConfig, AzureSearchProvider)

    try:
        from elspeth.plugins.infrastructure.clients.retrieval.chroma import (
            ChromaSearchProvider,
            ChromaSearchProviderConfig,
        )

        def _chroma_factory(config: ChromaSearchProviderConfig, *, execution: Any, run_id: Any, **_kwargs: Any) -> ChromaSearchProvider:
            """Chroma uses the SDK directly — passes execution repo and run_id for audit trail.

            execution and run_id are mandatory (not defaulted to None) because Chroma
            search calls must be recorded in the audit trail (B1 fix). If the engine
            ever calls this factory without execution, it should crash at startup, not
            silently skip audit recording at query time.
            """
            return ChromaSearchProvider(config=config, execution=execution, run_id=run_id)

        providers["chroma"] = (ChromaSearchProviderConfig, _chroma_factory)
    except ModuleNotFoundError as exc:
        # Only chromadb itself being absent is the documented optional-extra
        # state ([rag] not installed). A chromadb that is installed but fails
        # its own import — a missing transitive dependency, a broken wheel —
        # or a missing first-party module propagates: the remediation for an
        # absent provider ("install elspeth[rag]") is wrong for a broken one.
        missing = exc.name or ""
        if missing != "chromadb" and not missing.startswith("chromadb."):
            raise

    return providers


PROVIDERS: dict[str, _ProviderEntry] = _get_providers()


class RAGRetrievalConfig(RetrievalOutputConfig):
    """Configuration for the rag_retrieval transform plugin."""

    provider: RetrievalProviderName = Field(description="Retrieval provider name registered in the RAG provider catalog.")
    provider_config: dict[str, Any] = Field(description="Provider-specific retrieval configuration passed to the selected provider.")

    @model_validator(mode="after")
    def validate_provider_config(self) -> Self:
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {self.provider!r}. Available: {sorted(PROVIDERS)}")
        config_cls, _ = PROVIDERS[self.provider]
        config_cls(**self.provider_config)
        return self
