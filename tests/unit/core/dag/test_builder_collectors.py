"""Builder tests for collector nodes and the scope binding node-config key."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.contracts.types import CollectorName
from elspeth.core.config import CollectorSettings, ScopeSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


class _Source:
    name = "src"
    output_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": {"mode": "observed"}}
    _on_validation_failure = "discard"
    on_success = "rows"
    _output_schema_config = None


class _Sink:
    name = "out"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


class _MultiRowTransform:
    """Stub multi-row transform (creates_tokens=True) — a scope opener."""

    input_schema = None
    output_schema = None
    creates_tokens = True
    is_batch_aware = False
    on_success: str | None = "pages"
    on_error: str | None = "discard"
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        # .name is the PLUGIN name (matches TransformSettings.plugin below via
        # WiredTransform.__post_init__'s identity check) — NOT the node's
        # config name, which is "explode" (TransformSettings.name).
        self.name = "json_explode"
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = None


class _BatchTransform:
    """Stub batch-aware transform — the collector plugin."""

    input_schema = None
    output_schema = None
    creates_tokens = False
    is_batch_aware = True
    on_success: str | None = None
    on_error: str | None = None
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        self.name = "stitch"
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = None


def _source_settings() -> dict[str, SourceSettings]:
    return {"src": SourceSettings(plugin="csv", options={"path": "x.csv", "schema": {"mode": "observed"}}, on_success="rows")}


def _explode_settings() -> TransformSettings:
    return TransformSettings(name="explode", plugin="json_explode", input="rows", on_success="pages", on_error="discard")


def _collector_settings() -> CollectorSettings:
    # on_error deliberately omitted: None = derives-from-structure (spec §7 rule 9),
    # the canonical authored shape (2026-08-22 synthesis).
    return CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="out")


def _scope_settings() -> ScopeSettings:
    return ScopeSettings(name="document_pages", opener="explode", closer="page_stitcher", policy="require_all")


def _build(**overrides: Any) -> ExecutionGraph:
    kwargs: dict[str, Any] = {
        "sources": {"src": _Source()},
        "source_settings_map": _source_settings(),
        "transforms": [WiredTransform(plugin=_MultiRowTransform(), settings=_explode_settings())],
        "sinks": {"out": _Sink()},
        "collectors": {"page_stitcher": (_BatchTransform(), _collector_settings())},
        "scope_settings": [_scope_settings()],
    }
    kwargs.update(overrides)
    return ExecutionGraph.from_plugin_instances(**kwargs)


class TestCollectorNode:
    def test_collector_node_is_built_with_scope_binding_key(self) -> None:
        graph = _build()
        collector_ids = graph.get_collector_id_map()
        assert list(collector_ids) == [CollectorName("page_stitcher")]
        info = graph.get_node_info(collector_ids[CollectorName("page_stitcher")])
        assert info.node_type == NodeType.COLLECTOR
        assert info.config["scope"] == {
            "name": "document_pages",
            "opener": "explode",
            "policy": "require_all",
            "on_group_failure": "quarantine",
        }

    def test_collector_requires_batch_aware_plugin(self) -> None:
        non_batch = _MultiRowTransform()
        with pytest.raises(GraphValidationError, match="is_batch_aware"):
            _build(collectors={"page_stitcher": (non_batch, _collector_settings())})

    def test_scope_binding_key_is_never_present_on_non_collector_nodes(self) -> None:
        # Canonical-hash stability (spec §3): "scope" must not leak into any
        # other node's canonical config dict.
        graph = _build()
        for info in graph.get_nodes():
            if info.node_type is not NodeType.COLLECTOR:
                assert "scope" not in info.config

    def test_scope_bound_region_is_computed(self) -> None:
        # EXPAND-kind bound region (spec §7 rule 3, §6.3): the explode ->
        # page_stitcher scope opens and closes with nothing between them in
        # this fixture, so membership is empty at depth 1 — the shape
        # WS3's leader_drain fixpoint bound (graph.get_max_bound_region_depth())
        # actually consumes.
        graph = _build()
        regions = graph.get_bound_regions()
        assert len(regions) == 1
        region = regions[0]
        assert region.member_node_ids == frozenset()
        assert region.depth == 1
        assert graph.get_max_bound_region_depth() == 1
        assert graph.escalation_fixpoint_bound == 1_000 + 8 * 1
