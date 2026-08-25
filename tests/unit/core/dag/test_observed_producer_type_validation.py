"""Build-time type validation across OBSERVED producers (elspeth-e6e552ce34).

Live repro this closes: csv source (mode: observed, guaranteed_fields) → gate
fork → two observed pass-through llm branches → union coalesce (observed) →
field_mapper declaring ``id: int``. Authoring validation and preview passed;
every merged row then died typed at the consumer's input preflight because the
CSV emits ``id`` as ``str`` by construction. Ruling (John, 2026-08-26): this
must fail at BUILD/PREVIEW so the planner self-repairs through ordinary
validation feedback.

Three coordinated pieces under test here:

1. Two new plugin contract facts threaded onto ``NodeInfo`` by the builder:
   ``observed_value_type`` (SOURCE-only — the structural type of every cell an
   observed-mode source emits; csv declares ``"str"``) and
   ``preserves_input_values`` (TRANSFORM-only — process() never changes the
   VALUE of a field present on the input row; adding fields is fine).
2. Two new arms in ``resolve_guaranteed_field_type``: recursion through an
   undeclaring pass-through that promises value preservation, and a structural
   answer at an observed source for fields in its own guaranteed set.
3. ``validate_observed_producer_declared_types`` — the final phase-2 pass that
   applies ``resolved_guarantee_type_mismatch`` to a typed consumer's required
   fields when the effective producer schema is observed/dynamic.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError


class TestNodeInfoContractFacts:
    """NodeInfo carries the two new plugin facts, guarded by node type."""

    def test_defaults_are_absent(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("t", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        assert graph.get_node_info("src").observed_value_type is None
        assert graph.get_node_info("src").preserves_input_values is False
        assert graph.get_node_info("t").preserves_input_values is False
        assert graph.get_node_info("t").observed_value_type is None

    def test_add_node_threads_both_facts(self) -> None:
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            observed_value_type="str",
        )
        graph.add_node(
            "t",
            node_type=NodeType.TRANSFORM,
            plugin_name="passthrough",
            preserves_input_values=True,
        )
        assert graph.get_node_info("src").observed_value_type == "str"
        assert graph.get_node_info("t").preserves_input_values is True

    def test_observed_value_type_is_source_only(self) -> None:
        """Offensive-programming guard, mirroring declared_required_fields."""
        graph = ExecutionGraph()
        with pytest.raises(GraphValidationError, match="observed_value_type"):
            graph.add_node(
                "t",
                node_type=NodeType.TRANSFORM,
                plugin_name="passthrough",
                observed_value_type="str",
            )

    def test_preserves_input_values_is_transform_only(self) -> None:
        """A source/sink carrying the transform fact is a wiring bug."""
        graph = ExecutionGraph()
        with pytest.raises(GraphValidationError, match="preserves_input_values"):
            graph.add_node(
                "src",
                node_type=NodeType.SOURCE,
                plugin_name="csv",
                preserves_input_values=True,
            )
