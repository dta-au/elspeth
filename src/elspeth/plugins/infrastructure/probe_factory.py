"""Factory for constructing collection probes from explicit config declarations.

No collection probe provider ships since 0.8.18: the only one, for Chroma, was
removed with the chromadb dependency. Settings refuse a non-empty
``collection_probes`` list at load (``ElspethSettings``), so this factory only
ever receives an empty list. The registry stays as the seam a future provider
plugs into.
"""

from __future__ import annotations

from elspeth.contracts.probes import CollectionProbe
from elspeth.core.dependency_config import CollectionProbeConfig

_PROBE_REGISTRY: dict[str, type] = {}


def build_collection_probes(
    configs: list[CollectionProbeConfig],
) -> list[CollectionProbe]:
    """Construct probes from explicit config declarations."""
    probes: list[CollectionProbe] = []
    for config in configs:
        if config.provider not in _PROBE_REGISTRY:
            raise ValueError(f"Unknown collection probe provider: {config.provider!r}. Available: {sorted(_PROBE_REGISTRY)}")
        probe_cls = _PROBE_REGISTRY[config.provider]
        probes.append(probe_cls(config.collection, config.provider_config))
    return probes
