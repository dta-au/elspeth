"""Compatibility re-export for the provider-neutral LLM model catalogue."""

from elspeth.plugins.llm.model_catalog import (
    MODEL_CATALOG_OPENROUTER,
    OPENROUTER_LITELLM_PREFIX,
    OPENROUTER_MODELS_URL,
    prime_openrouter_catalog_from_live,
    read_litellm_model_list,
    read_openrouter_catalog_snapshot_id,
    reset_live_openrouter_catalog,
)
from elspeth.plugins.llm.model_catalog import (
    _bundled_openrouter_slice as _bundled_openrouter_slice,
)
from elspeth.plugins.llm.model_catalog import (
    _bundled_openrouter_slice_sha256 as _bundled_openrouter_slice_sha256,
)
from elspeth.plugins.llm.model_catalog import (
    _read_openrouter_catalog as _read_openrouter_catalog,
)

__all__ = [
    "MODEL_CATALOG_OPENROUTER",
    "OPENROUTER_LITELLM_PREFIX",
    "OPENROUTER_MODELS_URL",
    "prime_openrouter_catalog_from_live",
    "read_litellm_model_list",
    "read_openrouter_catalog_snapshot_id",
    "reset_live_openrouter_catalog",
]
