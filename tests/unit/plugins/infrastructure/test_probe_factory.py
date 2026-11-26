"""Tests for the collection probe factory.

No collection probe provider ships since 0.8.18: the Chroma provider left with
the chromadb dependency. Settings refuse a non-empty ``collection_probes`` list
(see ``tests/unit/core/test_config_settings.py``); the factory keeps its
registry seam and refuses any provider it does not know.
"""

from __future__ import annotations

import pytest

from elspeth.core.dependency_config import CollectionProbeConfig
from elspeth.plugins.infrastructure import probe_factory
from elspeth.plugins.infrastructure.probe_factory import build_collection_probes


def test_no_probe_provider_is_registered() -> None:
    assert probe_factory._PROBE_REGISTRY == {}


def test_empty_configs_return_no_probes() -> None:
    assert build_collection_probes([]) == []


def test_a_config_naming_the_removed_provider_is_refused() -> None:
    config = CollectionProbeConfig(collection="docs", provider="chroma", provider_config={"mode": "persistent"})

    with pytest.raises(ValueError, match=r"Unknown collection probe provider: 'chroma'\. Available: \[\]"):
        build_collection_probes([config])
