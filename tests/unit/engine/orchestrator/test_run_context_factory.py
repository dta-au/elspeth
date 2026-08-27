# tests/unit/engine/orchestrator/test_run_context_factory.py
"""Tests for run_context_factory's aggregation-timeout transform lookup.

elspeth-8783933d99: the lookup filters config.transforms — a homogeneous
Sequence[RowPlugin] — so it keys on the conditions that do real work
(is_batch_aware, node_id membership in aggregation_settings) and must NOT
re-measure TransformProtocol conformance: a transform-shaped plugin missing a
protocol member was silently dropped from the timeout lookup, so its
aggregation node never fired timeout flushes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from elspeth.contracts import TransformProtocol
from elspeth.contracts.types import NodeID
from elspeth.engine.orchestrator.run_context_factory import build_agg_transform_lookup
from elspeth.engine.orchestrator.types import PipelineConfig
from tests.fixtures.nonconforming_transform import NonConformingTransform


@dataclass(frozen=True)
class _NamedPlugin:
    name: str


def _make_config(
    *,
    transforms: list[Any],
    aggregation_settings: dict[str, Any],
) -> PipelineConfig:
    return PipelineConfig(
        sources={"primary": _NamedPlugin(name="test-source")},
        transforms=transforms,
        sinks={"output": _NamedPlugin(name="output")},
        aggregation_settings=aggregation_settings,
    )


class TestBuildAggTransformLookup:
    def test_non_conforming_batch_aware_transform_is_in_lookup(self) -> None:
        """A transform missing a TransformProtocol member joins the timeout lookup."""
        transform = NonConformingTransform(node_id="agg-node-1", is_batch_aware=True)
        assert not isinstance(transform, TransformProtocol)  # precondition, not the pin
        config = _make_config(transforms=[transform], aggregation_settings={"agg-node-1": object()})

        lookup = build_agg_transform_lookup(config)

        assert set(lookup) == {"agg-node-1"}
        assert lookup["agg-node-1"].transform is transform
        assert lookup["agg-node-1"].node_id == NodeID("agg-node-1")

    def test_non_batch_aware_transform_excluded(self) -> None:
        transform = NonConformingTransform(node_id="agg-node-1", is_batch_aware=False)
        config = _make_config(transforms=[transform], aggregation_settings={"agg-node-1": object()})

        assert build_agg_transform_lookup(config) == {}

    def test_transform_outside_aggregation_settings_excluded(self) -> None:
        transform = NonConformingTransform(node_id="other-node", is_batch_aware=True)
        config = _make_config(transforms=[transform], aggregation_settings={"agg-node-1": object()})

        assert build_agg_transform_lookup(config) == {}

    def test_node_id_none_excluded(self) -> None:
        transform = NonConformingTransform(node_id=None, is_batch_aware=True)
        config = _make_config(transforms=[transform], aggregation_settings={"agg-node-1": object()})

        assert build_agg_transform_lookup(config) == {}

    def test_no_aggregation_settings_yields_empty_lookup(self) -> None:
        transform = NonConformingTransform(node_id="agg-node-1", is_batch_aware=True)
        config = _make_config(transforms=[transform], aggregation_settings={})

        assert build_agg_transform_lookup(config) == {}


class TestInitializeRunContextDerivesLookup:
    def test_call_site_derives_from_helper_without_isinstance(self) -> None:
        """initialize_run_context must DERIVE its lookup via build_agg_transform_lookup.

        Re-inlining a filtered loop at the call site would evade every
        behavior pin on the helper above; this pins the derivation. The
        isinstance assertion keeps a structural re-classification from being
        reintroduced anywhere in the method (elspeth-8783933d99).
        """
        import ast
        import inspect
        import textwrap

        from elspeth.engine.orchestrator.run_context_factory import RunContextFactory

        source = textwrap.dedent(inspect.getsource(RunContextFactory.initialize_run_context))
        tree = ast.parse(source)
        called_names = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert "build_agg_transform_lookup" in called_names, "the lookup must be derived from the helper, never re-inlined"
        assert "isinstance" not in called_names, "no structural classification at the call site"
