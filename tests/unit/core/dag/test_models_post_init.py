"""Tests for __post_init__ validations on DAG model types.

Covers: GraphValidationWarning, BranchInfo, _GateEntry, NodeInfo.
"""

from types import MappingProxyType

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.contracts.types import CoalesceName, NodeID
from elspeth.core.dag.models import BranchInfo, GraphValidationError, GraphValidationWarning, NodeInfo, _GateEntry


class TestGraphValidationWarningPostInit:
    def test_rejects_empty_code(self) -> None:
        with pytest.raises(ValueError, match="code must not be empty"):
            GraphValidationWarning(code="", message="something", node_ids=())

    def test_rejects_empty_message(self) -> None:
        with pytest.raises(ValueError, match="message must not be empty"):
            GraphValidationWarning(code="W001", message="", node_ids=())

    def test_accepts_valid(self) -> None:
        w = GraphValidationWarning(code="W001", message="test", node_ids=("n1",))
        assert w.code == "W001"


class TestBranchInfoPostInit:
    def test_rejects_empty_coalesce_name(self) -> None:
        with pytest.raises(ValueError, match="coalesce_name must not be empty"):
            BranchInfo(coalesce_name=CoalesceName(""), gate_node_id=NodeID("g1"))

    def test_rejects_empty_gate_node_id(self) -> None:
        with pytest.raises(ValueError, match="gate_node_id must not be empty"):
            BranchInfo(coalesce_name=CoalesceName("merge1"), gate_node_id=NodeID(""))

    def test_accepts_valid(self) -> None:
        b = BranchInfo(coalesce_name=CoalesceName("merge1"), gate_node_id=NodeID("gate1"))
        assert b.coalesce_name == "merge1"


class TestGateEntryPostInit:
    def test_rejects_empty_node_id(self) -> None:
        with pytest.raises(ValueError, match="node_id must not be empty"):
            _GateEntry(node_id=NodeID(""), name="g1", fork_to=None, routes=MappingProxyType({"a": "b"}))

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name must not be empty"):
            _GateEntry(node_id=NodeID("n1"), name="", fork_to=None, routes=MappingProxyType({"a": "b"}))

    def test_rejects_empty_fork_to_tuple(self) -> None:
        with pytest.raises(ValueError, match="fork_to must not be empty tuple"):
            _GateEntry(node_id=NodeID("n1"), name="g1", fork_to=(), routes=MappingProxyType({"a": "b"}))

    def test_rejects_empty_routes(self) -> None:
        with pytest.raises(ValueError, match="routes must have at least one entry"):
            _GateEntry(node_id=NodeID("n1"), name="g1", fork_to=None, routes=MappingProxyType({}))

    def test_accepts_valid(self) -> None:
        g = _GateEntry(node_id=NodeID("n1"), name="g1", fork_to=("a", "b"), routes=MappingProxyType({"x": "y"}))
        assert g.node_id == "n1"

    def test_accepts_none_fork_to(self) -> None:
        g = _GateEntry(node_id=NodeID("n1"), name="g1", fork_to=None, routes=MappingProxyType({"x": "y"}))
        assert g.fork_to is None


class TestNodeInfoDeclaredOutputFieldsGuard:
    """declared_output_fields is TRANSFORM-only (elspeth-cfcd333f83).

    Narrower than passes_through_input (TRANSFORM + AGGREGATION) because its
    only consumer pre-empts a collision check that lives solely in
    TransformExecutor._run_preflight — AggregationExecutor.execute_flush has
    no equivalent, so an aggregation carrying the declaration would be data
    with no reader.
    """

    def test_accepts_declared_output_fields_on_transform(self) -> None:
        info = NodeInfo(
            node_id=NodeID("transform_1"),
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            declared_output_fields=frozenset({"summary"}),
        )
        assert info.declared_output_fields == frozenset({"summary"})

    @pytest.mark.parametrize(
        "node_type",
        [NodeType.AGGREGATION, NodeType.SOURCE, NodeType.SINK, NodeType.GATE],
    )
    def test_rejects_declared_output_fields_on_non_transform(self, node_type: NodeType) -> None:
        with pytest.raises(GraphValidationError, match="declared_output_fields is only meaningful for TRANSFORM"):
            NodeInfo(
                node_id=NodeID("node_1"),
                node_type=node_type,
                plugin_name="whatever",
                declared_output_fields=frozenset({"summary"}),
            )

    def test_empty_declared_output_fields_allowed_on_any_node_type(self) -> None:
        """The guard fires on content, not on the attribute being present."""
        info = NodeInfo(
            node_id=NodeID("agg_1"),
            node_type=NodeType.AGGREGATION,
            plugin_name="batch_stats",
            declared_output_fields=frozenset(),
        )
        assert info.declared_output_fields == frozenset()
