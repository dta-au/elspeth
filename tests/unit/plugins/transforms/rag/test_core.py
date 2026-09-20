"""The retrieval core runs against any searcher, with no provider registry."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from elspeth.contracts import Determinism
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig, RetrievalTransformBase


class _Searcher:
    def __init__(self, chunks: list[RetrievalChunk]) -> None:
        self._chunks = chunks
        self.last_skipped_count = 0
        self.last_skipped_reasons: list[dict[str, Any]] = []
        self.closed = False

    def search(self, query: str, top_k: int, min_score: float, **_audit: Any) -> list[RetrievalChunk]:
        return self._chunks

    def close(self) -> None:
        self.closed = True


class _Transform(RetrievalTransformBase):
    name = "core_probe"
    determinism = Determinism.EXTERNAL_CALL
    config_model = RetrievalOutputConfig
    _provider_label = "probe"

    def __init__(self, config: dict[str, Any], searcher: _Searcher) -> None:
        super().__init__(config, RetrievalOutputConfig.from_dict(config, plugin_name=self.name))
        self._probe_searcher = searcher

    def _build_searcher(self, ctx: Any) -> _Searcher:
        return self._probe_searcher


def test_base_class_is_not_a_discoverable_plugin() -> None:
    # Discovery raises on a concrete nameless BaseTransform subclass and skips
    # abstract ones, so abstractness is what keeps the core out of the catalogue.
    assert "name" not in RetrievalTransformBase.__dict__
    assert inspect.isabstract(RetrievalTransformBase)


def test_output_config_rejects_template_and_pattern_together() -> None:
    with pytest.raises(PluginConfigError, match="mutually exclusive"):
        RetrievalOutputConfig.from_dict(
            {
                "output_prefix": "p",
                "query_field": "q",
                "query_template": "{{ row.q }}",
                "query_pattern": "x",
                "schema": {"mode": "observed"},
            },
            plugin_name="core_probe",
        )


def test_declared_output_fields_come_from_the_prefix() -> None:
    transform = _Transform({"output_prefix": "kb", "query_field": "q", "schema": {"mode": "observed"}}, _Searcher([]))
    assert transform.declared_output_fields == frozenset({"kb__rag_context", "kb__rag_score", "kb__rag_count", "kb__rag_sources"})


def test_close_releases_the_built_searcher() -> None:
    searcher = _Searcher([])
    transform = _Transform({"output_prefix": "kb", "query_field": "q", "schema": {"mode": "observed"}}, searcher)
    transform.__dict__["_searcher"] = searcher
    transform.close()
    assert searcher.closed is True
