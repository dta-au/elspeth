"""The builder enforces the opener check core/config.py defers to it (elspeth-9783949ed4).

``ScopeSettings`` name-membership is checked at settings load; opener
multi-row-ness "is only visible with plugin instances in hand" and the
builder never performed that deferred check. A creates_tokens=False opener
built a bound region no token could enter and the run died on its first row
with an internal OrchestrationInvariantError naming a phantom sink. Now the
builder refuses it where it already refuses a non-batch-aware collector
plugin, reading the same ``creates_tokens`` attribute the rule-5 census reads.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.schema import SchemaConfig
from elspeth.core.config import CollectorSettings, ScopeSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform

from .test_bound_regions import (
    _BoundRegionCollectorPlugin,
    _BoundRegionMockSink,
    _BoundRegionMockSource,
    _BoundRegionMultiRowTransform,
    _BoundRegionTransform,
)


def _build(opener: object) -> ExecutionGraph:
    """source → opener(pages) → collector[page_stitcher] → sink out."""
    stitcher = _BoundRegionCollectorPlugin(name="stitch_pages", output_schema_config=SchemaConfig(mode="observed", fields=None))
    return ExecutionGraph.from_plugin_instances(
        sources={"primary": _BoundRegionMockSource()},  # type: ignore[dict-item]
        source_settings_map={"primary": SourceSettings(plugin="mock_source", on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=opener,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="explode_pages",
                    plugin=opener.name,
                    input="source_out",
                    on_success="pages",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[],
        collectors={
            "page_stitcher": (
                stitcher,  # type: ignore[dict-item]
                CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="out"),
            )
        },
        scope_settings=[ScopeSettings(name="doc_pages", opener="explode_pages", closer="page_stitcher", policy="require_all")],
    )


def test_a_multi_row_opener_builds() -> None:
    graph = _build(_BoundRegionMultiRowTransform(name="explode_pages", output_schema_config=SchemaConfig(mode="observed", fields=None)))
    assert graph is not None


def test_a_single_row_opener_is_refused_at_build_with_the_opener_as_component() -> None:
    """The check the config layer's docstring promised the builder would make."""
    with pytest.raises(
        GraphValidationError, match=r"opener 'explode_pages' is not a multi-row transform \(creates_tokens=False\)"
    ) as exc_info:
        _build(_BoundRegionTransform(name="explode_pages", output_schema_config=SchemaConfig(mode="observed", fields=None)))
    assert exc_info.value.component_id == "explode_pages"
    assert exc_info.value.component_type == "transform"
