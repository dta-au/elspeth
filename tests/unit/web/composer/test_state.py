"""Tests for CompositionState and supporting data models."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, ClassVar

import pytest

from elspeth.contracts.sink import FAILSINK_ELIGIBLE_SINK_PLUGINS
from elspeth.core.config import CoalesceSettings
from elspeth.plugins.infrastructure.templates import TemplateError
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    EdgeType,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationEntry,
    ValidationSummary,
    queue_node_contract_error,
    route_destination_facts,
)
from elspeth.web.composer.yaml_generator import generate_yaml
from tests.unit.web.composer._probe_lifecycle_helpers import DelegatingPluginManagerDouble


class TestSourceSpec:
    def test_frozen(self) -> None:
        s = SourceSpec(plugin="csv", on_success="t1", options={}, on_validation_failure="discard")
        with pytest.raises(AttributeError):
            s.plugin = "json"  # type: ignore[misc]

    def test_options_deep_frozen(self) -> None:
        s = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"key": "val"}},
            on_validation_failure="discard",
        )
        with pytest.raises(TypeError):
            s.options["new"] = "x"  # type: ignore[index]

    def test_options_nested_frozen(self) -> None:
        s = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"key": "val"}},
            on_validation_failure="discard",
        )
        with pytest.raises(TypeError):
            s.options["nested"]["mutate"] = "x"

    def test_from_dict_round_trip(self) -> None:
        s = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"key": "val"}},
            on_validation_failure="quarantine",
        )
        restored = SourceSpec.from_dict(
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"nested": {"key": "val"}},
                "on_validation_failure": "quarantine",
            }
        )
        assert restored == s


class TestCompositionStateNamedSources:
    def _source(self, plugin: str, on_success: str) -> SourceSpec:
        return SourceSpec(
            plugin=plugin,
            on_success=on_success,
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )

    def test_sources_mapping_preserves_named_source_order_without_singular_facade(self) -> None:
        state = CompositionState(
            source=None,
            sources={
                "customers": self._source("csv", "customer_rows"),
                "orders": self._source("json", "order_rows"),
            },
            nodes=(),
            edges=(),
            outputs=(OutputSpec(name="customer_rows", plugin="json", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )

        assert tuple(state.sources) == ("customers", "orders")
        assert state.to_dict()["sources"]["orders"]["on_success"] == "order_rows"
        assert not hasattr(state, "source")

    def test_named_source_mutations_add_update_and_remove_one_source(self) -> None:
        state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)

        state = state.with_named_source("customers", self._source("csv", "customer_rows"))
        state = state.with_named_source("orders", self._source("json", "order_rows"))
        updated = state.with_named_source("customers", self._source("csv", "updated_customer_rows"))
        removed = updated.without_named_source("orders")

        assert tuple(updated.sources) == ("customers", "orders")
        assert updated.sources["customers"].on_success == "updated_customer_rows"
        assert tuple(removed.sources) == ("customers",)
        assert removed.sources["customers"].on_success == "updated_customer_rows"

    def test_from_dict_restores_sources_mapping(self) -> None:
        original = CompositionState(
            source=None,
            sources={"customers": self._source("csv", "customer_rows"), "orders": self._source("json", "order_rows")},
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=7,
        )

        restored = CompositionState.from_dict(original.to_dict())

        assert restored == original

    def test_validation_warnings_and_suggestions_cover_all_named_sources(self) -> None:
        """Named-source advisory checks must not stop at the compatibility source."""
        state = CompositionState(
            source=None,
            sources={
                "customers": self._source("csv", "customer_rows"),
                "orders": SourceSpec(
                    plugin="json",
                    on_success="order_rows",
                    options={"path": "/data/orders.json"},
                    on_validation_failure="missing_failures",
                ),
            },
            nodes=(),
            edges=(),
            outputs=(OutputSpec(name="customer_rows", plugin="json", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert any(e.component == "source:orders" and e.error_code == "quarantine_unknown_output" for e in result.errors)
        assert any(s.component == "source:orders" and "no explicit schema" in s.message for s in result.suggestions)

    def test_sources_mapping_is_the_only_domain_and_serialized_source_shape(self) -> None:
        """CompositionState must not expose a singular first-source facade."""
        state = CompositionState(
            sources={
                "customers": self._source("csv", "customer_rows"),
                "orders": self._source("json", "order_rows"),
            },
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        serialized = state.to_dict()
        restored = CompositionState.from_dict(serialized)

        assert not hasattr(state, "source")
        assert "source" not in serialized
        assert tuple(restored.sources) == ("customers", "orders")


class TestNodeSpec:
    def _make_transform(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "transform_1",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "source_out",
            "on_success": "sink_main",
            "on_error": None,
            "options": {"field": "name"},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _make_gate(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "gate_1",
            "node_type": "gate",
            "plugin": None,
            "input": "source_out",
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": "row['score'] >= 0.5",
            "routes": {"high": "sink_good", "low": "sink_bad"},
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def test_frozen(self) -> None:
        n = self._make_transform()
        with pytest.raises(AttributeError):
            n.id = "new_id"  # type: ignore[misc]

    def test_options_deep_frozen(self) -> None:
        n = self._make_transform(options={"nested": {"k": "v"}})
        with pytest.raises(TypeError):
            n.options["new"] = 1  # type: ignore[index]

    def test_routes_deep_frozen(self) -> None:
        n = self._make_gate()
        with pytest.raises(TypeError):
            n.routes["extra"] = "val"  # type: ignore[index]

    def test_fork_to_is_tuple(self) -> None:
        n = self._make_gate(fork_to=("path_a", "path_b"))
        assert isinstance(n.fork_to, tuple)
        assert n.fork_to == ("path_a", "path_b")

    def test_branches_is_tuple(self) -> None:
        n = NodeSpec(
            id="coal_1",
            node_type="coalesce",
            plugin=None,
            input="join_point",
            on_success="sink_main",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=("path_a", "path_b"),
            policy="require_all",
            merge="nested",
        )
        assert isinstance(n.branches, tuple)

    def test_from_dict_with_optional_fields(self) -> None:
        """from_dict reconstructs optional fields; missing ones default to None."""
        d = {
            "id": "g1",
            "node_type": "gate",
            "plugin": None,
            "input": "in",
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": "row['x'] > 1",
            "routes": {"high": "s1"},
            "fork_to": ["path_a", "path_b"],
        }
        n = NodeSpec.from_dict(d)
        assert n.condition == "row['x'] > 1"
        assert n.fork_to == ("path_a", "path_b")
        assert n.branches is None
        assert n.policy is None
        assert n.merge is None

    def test_from_dict_converts_list_to_tuple(self) -> None:
        """to_dict() serialises tuples as lists; from_dict() must convert back."""
        d: dict[str, object] = {
            "id": "c1",
            "node_type": "coalesce",
            "plugin": None,
            "input": "join",
            "on_success": "out",
            "on_error": None,
            "options": {},
            "branches": ["a", "b"],
            "policy": "require_all",
            "merge": "nested",
        }
        n = NodeSpec.from_dict(d)
        assert isinstance(n.branches, tuple)
        assert n.branches == ("a", "b")


class TestEdgeSpec:
    def test_frozen(self) -> None:
        e = EdgeSpec(
            id="e1",
            from_node="source",
            to_node="t1",
            edge_type="on_success",
            label=None,
        )
        with pytest.raises(AttributeError):
            e.id = "e2"  # type: ignore[misc]

    def test_from_dict_round_trip(self) -> None:
        e = EdgeSpec(
            id="e1",
            from_node="source",
            to_node="t1",
            edge_type="on_success",
            label="main",
        )
        restored = EdgeSpec.from_dict(
            {
                "id": "e1",
                "from_node": "source",
                "to_node": "t1",
                "edge_type": "on_success",
                "label": "main",
            }
        )
        assert restored == e


class TestOutputSpec:
    def test_frozen(self) -> None:
        o = OutputSpec(name="out", plugin="csv", options={}, on_write_failure="discard")
        with pytest.raises(AttributeError):
            o.name = "new"  # type: ignore[misc]

    def test_options_deep_frozen(self) -> None:
        o = OutputSpec(
            name="out",
            plugin="csv",
            options={"nested": {"k": 1}},
            on_write_failure="discard",
        )
        with pytest.raises(TypeError):
            o.options["new"] = 2  # type: ignore[index]

    def test_from_dict_round_trip(self) -> None:
        o = OutputSpec(
            name="out",
            plugin="csv",
            options={"path": "/out.csv"},
            on_write_failure="quarantine",
        )
        restored = OutputSpec.from_dict(
            {
                "name": "out",
                "plugin": "csv",
                "options": {"path": "/out.csv"},
                "on_write_failure": "quarantine",
            }
        )
        assert restored == o


class TestPipelineMetadata:
    def test_frozen(self) -> None:
        m = PipelineMetadata()
        with pytest.raises(AttributeError):
            m.name = "new"  # type: ignore[misc]

    def test_from_dict_round_trip(self) -> None:
        m = PipelineMetadata(name="My Pipeline", description="Desc")
        restored = PipelineMetadata.from_dict(
            {
                "name": "My Pipeline",
                "description": "Desc",
            }
        )
        assert restored == m

    def test_from_dict_crashes_on_missing_fields(self) -> None:
        """Missing fields crash — this is Tier 1 data from to_dict()."""
        with pytest.raises(KeyError):
            PipelineMetadata.from_dict({})


class TestValidationSummary:
    def test_valid(self) -> None:
        v = ValidationSummary(is_valid=True, errors=())
        assert v.is_valid is True
        assert v.errors == ()

    def test_with_errors(self) -> None:
        v = ValidationSummary(is_valid=False, errors=(ValidationEntry("test", "No source configured.", "high"),))
        assert v.is_valid is False
        assert len(v.errors) == 1


class TestEdgeContract:
    def test_frozen(self) -> None:
        from elspeth.web.composer.state import EdgeContract

        ec = EdgeContract(
            from_id="source",
            to_id="add_world",
            producer_guarantees=("text",),
            consumer_requires=("text",),
            missing_fields=(),
            satisfied=True,
        )
        with pytest.raises(AttributeError):
            ec.satisfied = False  # type: ignore[misc]

    def test_to_dict_uses_from_key(self) -> None:
        """EdgeContract.to_dict() serializes from_id as 'from' (JSON key)."""
        from elspeth.web.composer.state import EdgeContract

        ec = EdgeContract(
            from_id="source",
            to_id="add_world",
            producer_guarantees=("text",),
            consumer_requires=("text",),
            missing_fields=(),
            satisfied=True,
        )
        d = ec.to_dict()
        assert d["from"] == "source"
        assert d["to"] == "add_world"
        assert d["producer_guarantees"] == ["text"]
        assert d["consumer_requires"] == ["text"]
        assert d["missing_fields"] == []
        assert d["satisfied"] is True

    def test_to_dict_empty_fields(self) -> None:
        from elspeth.web.composer.state import EdgeContract

        ec = EdgeContract(
            from_id="source",
            to_id="sink",
            producer_guarantees=(),
            consumer_requires=(),
            missing_fields=(),
            satisfied=True,
        )
        d = ec.to_dict()
        assert d["producer_guarantees"] == []
        assert d["consumer_requires"] == []
        assert d["missing_fields"] == []


class TestValidationSummaryEdgeContracts:
    def test_default_empty(self) -> None:
        vs = ValidationSummary(is_valid=True, errors=())
        assert vs.edge_contracts == ()

    def test_with_edge_contracts(self) -> None:
        from elspeth.web.composer.state import EdgeContract

        ec = EdgeContract(
            from_id="source",
            to_id="t1",
            producer_guarantees=("text",),
            consumer_requires=("text",),
            missing_fields=(),
            satisfied=True,
        )
        vs = ValidationSummary(is_valid=True, errors=(), edge_contracts=(ec,))
        assert len(vs.edge_contracts) == 1
        assert vs.edge_contracts[0].satisfied is True


class TestCompositionState:
    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(self) -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success="transform_1",
            options={"path": "/data/in.csv"},
            on_validation_failure="quarantine",
        )

    def _make_node(self, id: str = "transform_1") -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin="passthrough",
            input="source_out",
            on_success="sink_main",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_edge(self, id: str = "e1") -> EdgeSpec:
        return EdgeSpec(
            id=id,
            from_node="source",
            to_node="transform_1",
            edge_type="on_success",
            label=None,
        )

    def _make_output(self, name: str = "main_output") -> OutputSpec:
        return OutputSpec(
            name=name,
            plugin="csv",
            options={"path": "/out.csv"},
            on_write_failure="quarantine",
        )

    # --- Immutability ---

    def test_frozen(self) -> None:
        state = self._empty_state()
        with pytest.raises(AttributeError):
            state.version = 2  # type: ignore[misc]

    def test_nodes_tuple_frozen(self) -> None:
        """nodes is a tuple — cannot append."""
        state = self._empty_state()
        assert isinstance(state.nodes, tuple)

    def test_metadata_frozen(self) -> None:
        """metadata is a frozen dataclass — deep freeze via freeze_fields."""
        state = self._empty_state()
        with pytest.raises(AttributeError):
            state.metadata.name = "mutated"  # type: ignore[misc]

    # --- with_source ---

    def test_with_source_returns_new_instance(self) -> None:
        state = self._empty_state()
        src = self._make_source()
        new_state = state.with_source(src)
        assert new_state is not state
        assert new_state.sources["source"] is src
        assert state.sources == {}  # original unchanged

    def test_with_source_increments_version(self) -> None:
        state = self._empty_state()
        new_state = state.with_source(self._make_source())
        assert new_state.version == 2

    # --- with_node ---

    def test_with_node_adds(self) -> None:
        state = self._empty_state()
        node = self._make_node()
        new_state = state.with_node(node)
        assert len(new_state.nodes) == 1
        assert new_state.nodes[0].id == "transform_1"
        assert new_state.version == 2

    def test_with_node_replaces_existing(self) -> None:
        state = self._empty_state()
        node1 = self._make_node("t1")
        node2 = self._make_node("t1")  # same ID
        state2 = state.with_node(node1)
        state3 = state2.with_node(node2)
        assert len(state3.nodes) == 1
        assert state3.version == 3

    def test_with_node_preserves_order(self) -> None:
        state = self._empty_state()
        state = state.with_node(self._make_node("a"))
        state = state.with_node(self._make_node("b"))
        state = state.with_node(self._make_node("c"))
        assert [n.id for n in state.nodes] == ["a", "b", "c"]

    # --- without_node ---

    def test_without_node_removes(self) -> None:
        state = self._empty_state().with_node(self._make_node("t1"))
        new_state = state.without_node("t1")
        assert new_state is not None
        assert len(new_state.nodes) == 0
        assert new_state.version == 3

    def test_without_node_nonexistent_returns_none(self) -> None:
        state = self._empty_state()
        result = state.without_node("nonexistent")
        assert result is None

    # --- with_edge ---

    def test_with_edge_adds(self) -> None:
        state = self._empty_state()
        edge = self._make_edge()
        new_state = state.with_edge(edge)
        assert len(new_state.edges) == 1
        assert new_state.version == 2

    def test_with_edge_replaces_by_id(self) -> None:
        state = self._empty_state()
        e1 = EdgeSpec(id="e1", from_node="source", to_node="t1", edge_type="on_success", label=None)
        e1_updated = EdgeSpec(id="e1", from_node="source", to_node="t2", edge_type="on_success", label=None)
        state2 = state.with_edge(e1).with_edge(e1_updated)
        assert len(state2.edges) == 1
        assert state2.edges[0].to_node == "t2"

    def test_with_edge_preserves_order(self) -> None:
        """Updating an existing edge must preserve its position, not append."""
        state = self._empty_state()
        e1 = EdgeSpec(id="e1", from_node="source", to_node="t1", edge_type="on_success", label=None)
        e2 = EdgeSpec(id="e2", from_node="t1", to_node="t2", edge_type="on_success", label=None)
        e3 = EdgeSpec(id="e3", from_node="t2", to_node="sink", edge_type="on_success", label=None)
        state = state.with_edge(e1).with_edge(e2).with_edge(e3)
        assert [e.id for e in state.edges] == ["e1", "e2", "e3"]

        # Update e2 — should stay at index 1, not move to end
        e2_updated = EdgeSpec(id="e2", from_node="t1", to_node="t2_new", edge_type="on_success", label="updated")
        updated = state.with_edge(e2_updated)
        assert [e.id for e in updated.edges] == ["e1", "e2", "e3"]
        assert updated.edges[1].to_node == "t2_new"
        assert updated.edges[1].label == "updated"

    # --- without_edge ---

    def test_without_edge_removes(self) -> None:
        state = self._empty_state().with_edge(self._make_edge("e1"))
        new_state = state.without_edge("e1")
        assert new_state is not None
        assert len(new_state.edges) == 0

    def test_without_edge_nonexistent_returns_none(self) -> None:
        state = self._empty_state()
        result = state.without_edge("nonexistent")
        assert result is None

    # --- with_output ---

    def test_with_output_adds(self) -> None:
        state = self._empty_state()
        output = self._make_output()
        new_state = state.with_output(output)
        assert len(new_state.outputs) == 1
        assert new_state.version == 2

    def test_with_output_replaces_by_name(self) -> None:
        state = self._empty_state()
        o1 = self._make_output("out")
        o2 = OutputSpec(name="out", plugin="json", options={}, on_write_failure="discard")
        state2 = state.with_output(o1).with_output(o2)
        assert len(state2.outputs) == 1
        assert state2.outputs[0].plugin == "json"

    def test_with_output_preserves_order(self) -> None:
        """Updating an existing output must preserve its position, not append."""
        state = self._empty_state()
        o1 = self._make_output("alpha")
        o2 = self._make_output("beta")
        o3 = self._make_output("gamma")
        state = state.with_output(o1).with_output(o2).with_output(o3)
        assert [o.name for o in state.outputs] == ["alpha", "beta", "gamma"]

        # Update beta — should stay at index 1, not move to end
        o2_updated = OutputSpec(name="beta", plugin="json", options={"format": "lines"}, on_write_failure="discard")
        updated = state.with_output(o2_updated)
        assert [o.name for o in updated.outputs] == ["alpha", "beta", "gamma"]
        assert updated.outputs[1].plugin == "json"

    # --- without_output ---

    def test_without_output_removes(self) -> None:
        state = self._empty_state().with_output(self._make_output("out"))
        new_state = state.without_output("out")
        assert new_state is not None
        assert len(new_state.outputs) == 0

    def test_without_output_nonexistent_returns_none(self) -> None:
        result = self._empty_state().without_output("nope")
        assert result is None

    # --- with_metadata ---

    def test_with_metadata_partial_update(self) -> None:
        state = self._empty_state()
        new_state = state.with_metadata({"name": "My Pipeline"})
        assert new_state.metadata.name == "My Pipeline"
        assert new_state.metadata.description == ""  # unchanged
        assert new_state.version == 2

    def test_with_metadata_full_update(self) -> None:
        state = self._empty_state()
        new_state = state.with_metadata({"name": "P1", "description": "Desc"})
        assert new_state.metadata.name == "P1"
        assert new_state.metadata.description == "Desc"

    # --- to_dict ---

    def test_to_dict_unwraps_frozen_containers(self) -> None:
        """to_dict() converts MappingProxyType -> dict and tuple -> list."""
        state = self._empty_state()
        src = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"k": "v"}},
            on_validation_failure="discard",
        )
        state = state.with_source(src)
        state = state.with_node(self._make_node("t1"))
        state = state.with_output(self._make_output("out"))

        d = state.to_dict()
        assert isinstance(d, dict)
        assert isinstance(d["nodes"], list)
        assert isinstance(d["sources"]["source"]["options"], dict)
        assert isinstance(d["sources"]["source"]["options"]["nested"], dict)
        assert isinstance(d["outputs"], list)

    def test_to_dict_roundtrip_yaml(self) -> None:
        """to_dict() output is yaml.dump()-safe (no MappingProxyType errors)."""
        import yaml

        state = self._empty_state()
        src = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"deep": {"k": "v"}}},
            on_validation_failure="quarantine",
        )
        state = state.with_source(src)
        d = state.to_dict()
        yaml_str = yaml.dump(d, default_flow_style=False)
        assert "csv" in yaml_str

    def test_mutation_refreezes_containers(self) -> None:
        """Mutation methods must re-freeze since dataclasses.replace() skips __post_init__."""
        state = self._empty_state()
        src = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"k": "v"}},
            on_validation_failure="discard",
        )
        new_state = state.with_source(src)
        assert isinstance(new_state.nodes, tuple)
        with pytest.raises(TypeError):
            new_state.sources["source"].options["new"] = "x"  # type: ignore[index]

    # --- from_dict round-trip ---

    def test_from_dict_round_trip_empty(self) -> None:
        """Empty state round-trips through to_dict/from_dict."""
        state = self._empty_state()
        restored = CompositionState.from_dict(state.to_dict())
        assert restored == state

    def test_from_dict_round_trip_fully_populated(self) -> None:
        """Fully populated state round-trips through to_dict/from_dict."""
        gate = NodeSpec(
            id="gate_1",
            node_type="gate",
            plugin=None,
            input="source_out",
            on_success=None,
            on_error=None,
            options={},
            condition="row['score'] >= 0.5",
            routes={"high": "sink_good", "low": "sink_bad"},
            fork_to=("path_a", "path_b"),
            branches=None,
            policy=None,
            merge=None,
        )
        coalesce = NodeSpec(
            id="coal_1",
            node_type="coalesce",
            plugin=None,
            input="join_point",
            on_success="main_output",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=("path_a", "path_b"),
            policy="require_all",
            merge="nested",
        )
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="transform_1",
                options={"path": "/data/in.csv", "nested": {"key": "val"}},
                on_validation_failure="quarantine",
            ),
            nodes=(self._make_node("transform_1"), gate, coalesce),
            edges=(
                self._make_edge("e1"),
                EdgeSpec(id="e2", from_node="gate_1", to_node="sink_good", edge_type="route_true", label="high"),
            ),
            outputs=(
                self._make_output("main_output"),
                OutputSpec(name="sink_good", plugin="json", options={"indent": 2}, on_write_failure="discard"),
            ),
            metadata=PipelineMetadata(name="Test Pipeline", description="A fully populated test state"),
            version=42,
        )
        restored = CompositionState.from_dict(state.to_dict())
        assert restored == state

    def test_from_dict_round_trip_none_optional_fields(self) -> None:
        """NodeSpec optional fields omitted by to_dict() reconstruct as None."""
        node = self._make_node("t1")
        state = self._empty_state().with_node(node)
        restored = CompositionState.from_dict(state.to_dict())
        restored_node = restored.nodes[0]
        assert restored_node.condition is None
        assert restored_node.routes is None
        assert restored_node.fork_to is None
        assert restored_node.branches is None
        assert restored_node.policy is None
        assert restored_node.merge is None

    def test_from_dict_containers_are_frozen(self) -> None:
        """from_dict() output has deep-frozen containers (not plain dicts)."""
        state = self._empty_state()
        src = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"nested": {"k": "v"}},
            on_validation_failure="discard",
        )
        state = state.with_source(src)
        restored = CompositionState.from_dict(state.to_dict())
        assert restored.sources["source"].options is not None
        with pytest.raises(TypeError):
            restored.sources["source"].options["new"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            restored.sources["source"].options["nested"]["mutate"] = "y"


class TestStage1Validation:
    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(self, on_success: str = "t1", on_validation_failure: str = "discard") -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={},
            on_validation_failure=on_validation_failure,
        )

    def _make_transform(
        self,
        id: str,
        input: str,
        on_success: str,
        on_error: str = "discard",
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin="passthrough",
            input=input,
            on_success=on_success,
            on_error=on_error,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_output(self, name: str = "main") -> OutputSpec:
        return OutputSpec(name=name, plugin="csv", options={}, on_write_failure="discard")

    def _make_edge(
        self,
        id: str,
        from_node: str,
        to_node: str,
        edge_type: EdgeType = "on_success",
    ) -> EdgeSpec:
        return EdgeSpec(id=id, from_node=from_node, to_node=to_node, edge_type=edge_type, label=None)

    def _coalesce_route_state(self, *, on_success: str | None) -> CompositionState:
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_in"))
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="merge_point",
                node_type="coalesce",
                plugin=None,
                input="join",
                on_success=on_success,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=("path_a", "path_b"),
                policy="require_all",
                merge="nested",
            )
        )
        return state.with_output(self._make_output("main"))

    def test_empty_state_has_errors(self) -> None:
        result = self._empty_state().validate()
        assert not result.is_valid
        assert any(e.message == "No source configured." for e in result.errors)
        assert any(e.message == "No sinks configured." for e in result.errors)

    def test_minimal_valid_pipeline(self) -> None:
        """source -> transform -> sink, fully connected."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_connection_only_runtime_pipeline_is_valid_without_ui_edges(self) -> None:
        """Runtime connection fields, not UI edges, determine Stage 1 validity."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_connection_only_coalesce_pipeline_is_valid_without_ui_edges(self) -> None:
        """Coalesce terminal routes are valid when declared in runtime fields."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_in"))
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="merge_point",
                node_type="coalesce",
                plugin=None,
                input="join",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=("path_a", "path_b"),
                policy="require_all",
                merge="nested",
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_coalesce_timeout_survives_serialization_round_trip(self) -> None:
        state = self._coalesce_route_state(on_success="main")
        coalesce = next(node for node in state.nodes if node.node_type == "coalesce")
        state = state.with_node(replace(coalesce, timeout_seconds=5.0))

        restored = CompositionState.from_dict(state.to_dict())

        assert restored == state
        assert next(node for node in restored.nodes if node.node_type == "coalesce").timeout_seconds == 5.0

    @pytest.mark.parametrize(
        "timeout_seconds",
        # 10**400 and its negation are only reachable from a persisted session
        # payload (JSON has no integer ceiling and NodeSpec.from_dict does not
        # cross the Pydantic tool boundary). float() overflows on them, which
        # used to abort validate() with OverflowError instead of rejecting.
        [True, float("nan"), float("inf"), 0.0, -1.0, 10**400, -(10**400)],
    )
    def test_coalesce_rejects_invalid_timeout(self, timeout_seconds: object) -> None:
        state = self._coalesce_route_state(on_success="main")
        coalesce = next(node for node in state.nodes if node.node_type == "coalesce")
        state = state.with_node(replace(coalesce, timeout_seconds=timeout_seconds))

        result = state.validate()

        assert any(error.error_code == "coalesce_timeout_invalid" for error in result.errors)

    @pytest.mark.parametrize(
        ("node", "error_code"),
        [
            pytest.param(
                NodeSpec(
                    id="transform_1",
                    node_type="transform",
                    plugin="passthrough",
                    input="transform_1",
                    on_success="main",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    timeout_seconds=5.0,
                ),
                "node_timeout_unsupported",
                id="transform",
            ),
            pytest.param(
                NodeSpec(
                    id="gate_1",
                    node_type="gate",
                    plugin=None,
                    input="gate_1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "main", "false": "main"},
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    timeout_seconds=5.0,
                ),
                "node_timeout_unsupported",
                id="gate",
            ),
            pytest.param(
                NodeSpec(
                    id="aggregation_1",
                    node_type="aggregation",
                    plugin="batch_counter",
                    input="aggregation_1",
                    on_success="main",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 100, "timeout_seconds": 5.0},
                    timeout_seconds=5.0,
                ),
                "node_timeout_unsupported",
                id="aggregation",
            ),
            pytest.param(
                NodeSpec(
                    id="queue_1",
                    node_type="queue",
                    plugin=None,
                    input="queue_1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    timeout_seconds=5.0,
                ),
                "queue_config_invalid",
                id="queue",
            ),
        ],
    )
    def test_only_barrier_nodes_accept_top_level_timeout(self, node: NodeSpec, error_code: str) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success=node.input))
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert any(error.error_code == error_code and "timeout_seconds" in error.message for error in result.errors), result.errors

    @pytest.mark.parametrize(
        "node",
        [
            pytest.param(
                NodeSpec(
                    id="gate_1",
                    node_type="gate",
                    plugin="fork",
                    input="gate_1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="row['x'] > 1",
                    routes={"true": "fork", "false": "main"},
                    fork_to=("main", "alt"),
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                id="gate",
            ),
            pytest.param(
                NodeSpec(
                    id="coalesce_1",
                    node_type="coalesce",
                    plugin="passthrough",
                    input="coalesce_1",
                    on_success="main",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=("path_a", "path_b"),
                    policy="require_all",
                    merge="union",
                ),
                id="coalesce",
            ),
        ],
    )
    def test_structural_nodes_reject_a_plugin(self, node: NodeSpec) -> None:
        """gate and coalesce are built-in node_types wired with plugin=null.

        Queues (``queue_node_contract_error``) and row_unions (their
        forbidden-fields block) already reject a plugin; gate and coalesce
        silently persisted one, so a state could carry ``('g', 'gate', 'fork')``
        while ``is_valid`` — an authored token that neither generated YAML nor
        the runtime ever sees.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success=node.input))
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("alt"))

        result = state.validate()

        matching = [e for e in result.errors if e.error_code == "structural_node_plugin_forbidden"]
        assert len(matching) == 1, result.errors
        assert matching[0].component == f"node:{node.id}"
        assert "plugin" in matching[0].message and "plugin=null" in matching[0].message
        assert node.plugin not in matching[0].message, "the rejected plugin token is not echoed"

    def test_structural_node_without_plugin_is_not_flagged(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_1"))
        state = state.with_node(
            NodeSpec(
                id="gate_1",
                node_type="gate",
                plugin=None,
                input="gate_1",
                on_success=None,
                on_error=None,
                options={},
                condition="row['x'] > 1",
                routes={"true": "fork", "false": "main"},
                fork_to=("main", "alt"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("alt"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(e.error_code == "structural_node_plugin_forbidden" for e in result.errors)

    def test_multiple_fork_gates_do_not_collide_on_fork_route_keyword(self) -> None:
        """Two gates routing to the reserved 'fork' keyword are not duplicate producers.

        Regression test for elspeth-b6940369a7: route target 'fork' is the
        fork-mode keyword, not a connection, so any number of gates may use it.
        Before the fix the web validator reported "Duplicate producer for
        connection 'fork'" and rejected multi-fork topologies the engine builds.

        Topology mirrors an engine-valid build (verified via
        from_plugin_instances): a boolean router gate splits into two fork
        gates whose branches terminate directly at sinks — no coalesce, so the
        only thing under test is the two gates both routing to 'fork'.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="router_in"))
        state = state.with_node(
            NodeSpec(
                id="router",
                node_type="gate",
                plugin=None,
                input="router_in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['x'] > 0",
                routes={"true": "fa_in", "false": "fb_in"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="gate_a",
                node_type="gate",
                plugin=None,
                input="fa_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("sink_a1", "sink_a2"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="gate_b",
                node_type="gate",
                plugin=None,
                input="fb_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("sink_b1", "sink_b2"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        for sink_name in ("sink_a1", "sink_a2", "sink_b1", "sink_b2"):
            state = state.with_output(self._make_output(sink_name))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_gate_route_to_discard_is_valid_without_output_named_discard(self) -> None:
        """Gate routes may target virtual 'discard' without declaring a sink."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_in"))
        state = state.with_node(
            NodeSpec(
                id="quality_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['keep']",
                routes={"true": "main", "false": "discard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_dangling_edge_from_node(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "nonexistent", "main"))
        result = state.validate()
        assert not result.is_valid
        assert any("nonexistent" in e.message and "from_node" in e.message for e in result.errors)

    def test_dangling_edge_to_node(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "nonexistent"))
        result = state.validate()
        assert not result.is_valid
        assert any("nonexistent" in e.message and "to_node" in e.message for e in result.errors)

    def test_duplicate_node_ids(self) -> None:
        """Two nodes with same id — caught by validation, not by with_node (which replaces)."""
        node = self._make_transform("dup", "in", "out")
        state = CompositionState(
            source=self._make_source(),
            nodes=(node, node),
            edges=(),
            outputs=(self._make_output(),),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate node ID" in e.message for e in result.errors)

    def test_duplicate_output_names(self) -> None:
        out = self._make_output("dup")
        state = CompositionState(
            source=self._make_source(),
            nodes=(),
            edges=(),
            outputs=(out, out),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate output name" in e.message for e in result.errors)

    def test_duplicate_edge_ids(self) -> None:
        edge = self._make_edge("dup", "source", "main")
        state = CompositionState(
            source=self._make_source(),
            nodes=(),
            edges=(edge, edge),
            outputs=(self._make_output(),),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate edge ID" in e.message for e in result.errors)

    def test_gate_missing_condition(self) -> None:
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes={"high": "s1"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("condition" in e.message for e in result.errors)

    def test_gate_malformed_condition_syntax_error(self) -> None:
        """validate() catches condition with invalid Python syntax."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['x'] >== 5",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Invalid gate condition syntax" in e.message for e in result.errors)

    def test_gate_injection_condition_security_error(self) -> None:
        """validate() catches injection attempts in conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="__import__('os').system('rm -rf /')",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_forbidden_function_call_condition(self) -> None:
        """validate() catches forbidden function calls (eval, exec, etc.)."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="eval('1+1')",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_valid_condition_passes_validation(self) -> None:
        """validate() accepts well-formed gate conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['score'] >= 0.85",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        # Only structural errors may remain (connection completeness etc.),
        # but no expression-related errors
        expr_errors = [e for e in result.errors if "gate condition" in e.message.lower()]
        assert expr_errors == []

    def test_gate_lambda_condition_rejected(self) -> None:
        """validate() catches lambda expressions in conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="lambda: True",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_comprehension_condition_rejected(self) -> None:
        """validate() catches list comprehensions in conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="[x for x in range(10)]",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_missing_routes(self) -> None:
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['x'] > 1",
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("routes" in e.message for e in result.errors)

    def test_transform_with_condition_is_error(self) -> None:
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="in",
            on_success="out",
            on_error=None,
            options={},
            condition="row['x'] > 1",
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("condition" in e.message for e in result.errors)

    def test_coalesce_missing_branches(self) -> None:
        node = NodeSpec(
            id="c1",
            node_type="coalesce",
            plugin=None,
            input="join",
            on_success="out",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy="require_all",
            merge="nested",
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "c1"))
        result = state.validate()
        assert not result.is_valid
        assert any("branches" in e.message for e in result.errors)

    def test_aggregation_missing_plugin(self) -> None:
        node = NodeSpec(
            id="a1",
            node_type="aggregation",
            plugin=None,
            input="in",
            on_success="out",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "a1"))
        result = state.validate()
        assert not result.is_valid
        assert any("plugin" in e.message for e in result.errors)

    def test_unknown_node_type_is_invalid(self) -> None:
        """Stage 1 must reject node types outside the closed runtime set."""
        node = NodeSpec.from_dict(
            {
                "id": "mystery",
                "node_type": "bogus",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {},
                "condition": None,
                "routes": None,
                "fork_to": None,
                "branches": None,
                "policy": None,
                "merge": None,
            }
        )
        state = self._empty_state().with_source(self._make_source(on_success="source_out"))
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "mystery"))

        result = state.validate()

        assert not result.is_valid
        assert any("unknown node_type 'bogus'" in e.message for e in result.errors)

    def test_unreachable_node(self) -> None:
        """Node exists but no edge points to it and source.on_success doesn't match."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="other"))
        state = state.with_node(self._make_transform("t1", "somewhere", "main"))
        state = state.with_output(self._make_output())
        result = state.validate()
        assert not result.is_valid
        assert any("not reachable" in e.message for e in result.errors)

    def test_validate_after_from_dict_round_trip(self) -> None:
        """W-4A-2: validate() on reconstructed state matches original."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))

        restored = CompositionState.from_dict(state.to_dict())
        result = restored.validate()
        assert result.is_valid, result.errors

    def test_edge_only_pipeline_is_invalid_when_runtime_connections_do_not_match(self) -> None:
        """UI edges cannot rescue runtime wiring that generate_yaml() will not emit."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="wrong_connection"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))

        result = state.validate()

        assert not result.is_valid
        assert any("runtime connection" in e.message for e in result.errors)

    @pytest.mark.parametrize(
        ("case_name", "expected_message"),
        [
            (
                "source_on_success",
                "Source on_success 'dangling' is neither a sink nor a known connection",
            ),
            (
                "transform_on_success",
                "Transform 't1' on_success 'dangling' is neither a sink nor a known connection",
            ),
            (
                "aggregation_on_success",
                "Aggregation 'agg1' on_success 'dangling' is neither a sink nor a known connection",
            ),
            (
                "coalesce_unknown_sink",
                "Coalesce 'merge_point' on_success references unknown sink 'dangling'",
            ),
            (
                "coalesce_connection_target",
                "Coalesce 'merge_point' has on_success='next_step'. Coalesce on_success must point to a sink when configured.",
            ),
            (
                "transform_on_error",
                "Transform 't1' on_error 'missing_error_sink' references unknown sink",
            ),
        ],
    )
    def test_validate_rejects_runtime_unresolvable_route_destinations(
        self,
        case_name: str,
        expected_message: str,
    ) -> None:
        """Stage 1 rejects terminal routes that the runtime DAG builder rejects."""
        if case_name == "source_on_success":
            state = self._empty_state().with_source(self._make_source(on_success="dangling")).with_output(self._make_output("main"))
        elif case_name == "transform_on_success":
            state = self._empty_state()
            state = state.with_source(self._make_source(on_success="t1"))
            state = state.with_node(self._make_transform("t1", "t1", "dangling"))
            state = state.with_output(self._make_output("main"))
        elif case_name == "aggregation_on_success":
            state = self._empty_state()
            state = state.with_source(self._make_source(on_success="agg1"))
            state = state.with_node(
                NodeSpec(
                    id="agg1",
                    node_type="aggregation",
                    plugin="batch_counter",
                    input="agg1",
                    on_success="dangling",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 1},
                )
            )
            state = state.with_output(self._make_output("main"))
        elif case_name == "coalesce_unknown_sink":
            state = self._coalesce_route_state(on_success="dangling")
        elif case_name == "coalesce_connection_target":
            state = self._coalesce_route_state(on_success="next_step")
            state = state.with_node(self._make_transform("after_merge", "next_step", "main"))
        elif case_name == "transform_on_error":
            state = self._empty_state()
            state = state.with_source(self._make_source(on_success="t1"))
            state = state.with_node(self._make_transform("t1", "t1", "main", on_error="missing_error_sink"))
            state = state.with_output(self._make_output("main"))
        else:
            raise AssertionError(f"Unhandled route validation case: {case_name}")

        result = state.validate()

        assert not result.is_valid, case_name
        assert any(expected_message in error.message for error in result.errors), (case_name, result.errors)

    # --- Warning rules (W1-W4) ---

    def test_validate_output_no_incoming_edge_warns(self) -> None:
        """W1: Output with no edge targeting it produces a warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("orphan"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("orphan" in w.message and "never receive data" in w.message for w in result.warnings)

    def test_validate_source_on_success_mismatch_warns(self) -> None:
        """W2: Source on_success doesn't match any node input."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="nonexistent"))
        state = state.with_node(self._make_transform("t1", "other_input", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any("nonexistent" in w.message and "does not match" in w.message for w in result.warnings)

    def test_validate_format_extension_mismatch_warns(self) -> None:
        """W4: Sink plugin/filename extension mismatch."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "results"))
        output = OutputSpec(
            name="results",
            plugin="csv",
            options={"path": "/output/data.json"},
            on_write_failure="discard",
        )
        state = state.with_output(output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "results"))
        result = state.validate()
        assert result.is_valid
        assert any("extension suggests a different format" in w.message for w in result.warnings)

    def test_validate_transform_missing_required_options_warns(self) -> None:
        """W5: Transform that requires config has empty options."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        # value_transform requires 'operations' key
        incomplete_transform = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="value_transform",
            input="t1",
            on_success="main",
            on_error="discard",
            options={},  # Empty - should trigger warning
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(incomplete_transform)
        output = OutputSpec(name="main", plugin="csv", options={"path": "out.csv"}, on_write_failure="discard")
        state = state.with_output(output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid  # Still structurally valid
        assert any("value_transform" in w.message and "incomplete" in w.message for w in result.warnings)

    def test_validate_transform_empty_operations_warns(self) -> None:
        """W5: Transform has the required key but it's empty."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        # value_transform with empty operations list
        empty_ops_transform = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="value_transform",
            input="t1",
            on_success="main",
            on_error="discard",
            options={"operations": []},  # Empty list - should trigger warning
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(empty_ops_transform)
        output = OutputSpec(name="main", plugin="csv", options={"path": "out.csv"}, on_write_failure="discard")
        state = state.with_output(output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("value_transform" in w.message and "empty" in w.message for w in result.warnings)

    def test_validate_file_sink_missing_path_warns(self) -> None:
        """W6: File sink without path configured."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # CSV sink with no path
        no_path_output = OutputSpec(name="main", plugin="csv", options={}, on_write_failure="discard")
        state = state.with_output(no_path_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid  # Structurally valid but won't run
        assert any("no path configured" in w.message for w in result.warnings)

    def test_validate_file_sink_empty_path_warns(self) -> None:
        """W6: File sink with empty string path."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # JSON sink with empty path
        empty_path_output = OutputSpec(name="main", plugin="json", options={"path": ""}, on_write_failure="discard")
        state = state.with_output(empty_path_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("empty path" in w.message for w in result.warnings)

    def test_validate_non_file_sink_no_path_ok(self) -> None:
        """Non-file sinks (like database) don't require path."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # Database sink - path is not a required option
        db_output = OutputSpec(
            name="main", plugin="database", options={"url": "sqlite:///:memory:", "table": "out"}, on_write_failure="discard"
        )
        state = state.with_output(db_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        # Should NOT warn about missing path for non-file sinks
        assert not any("no path configured" in w.message for w in result.warnings)

    # --- W7: on_write_failure reference validation ---

    def test_validate_on_write_failure_nonexistent_output_is_error(self) -> None:
        """A dangling failsink raises RouteValidationError at runtime init, so
        Stage 1 reports it as an error (elspeth-eb4127fb49)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        bad_output = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="nonexistent")
        state = state.with_output(bad_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_unknown_output" for e in result.errors)

    def test_validate_on_write_failure_self_reference_is_error(self) -> None:
        """A self-referencing failsink is a deterministic runtime rejection."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        self_ref = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="main")
        state = state.with_output(self_ref)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_self_reference" for e in result.errors)

    def test_validate_on_write_failure_ineligible_plugin_is_error(self) -> None:
        """A non-file failsink target is a deterministic runtime rejection."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="backup")
        backup_out = OutputSpec(
            name="backup", plugin="database", options={"url": "sqlite:///:memory:", "table": "t"}, on_write_failure="discard"
        )
        state = state.with_output(main_out)
        state = state.with_output(backup_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_ineligible_plugin" for e in result.errors)

    @pytest.mark.parametrize("plugin_name", sorted(FAILSINK_ELIGIBLE_SINK_PLUGINS))
    def test_validate_on_write_failure_shared_policy_plugins_are_valid(self, plugin_name: str) -> None:
        """Every centrally failsink-capable plugin is accepted by Stage 1."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="database", options={"table": "rows"}, on_write_failure="errors")
        errors_out = OutputSpec(name="errors", plugin=plugin_name, options={"path": f"/errors.{plugin_name}"}, on_write_failure="discard")
        state = state.with_output(main_out)
        state = state.with_output(errors_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))

        result = state.validate()

        assert not any("on_write_failure" in e.message for e in (*result.errors, *result.warnings))

    def test_validate_on_write_failure_chain_is_error(self) -> None:
        """A chained failsink is a deterministic runtime rejection."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="errors")
        errors_out = OutputSpec(name="errors", plugin="csv", options={"path": "/errors.csv"}, on_write_failure="overflow")
        overflow_out = OutputSpec(name="overflow", plugin="csv", options={"path": "/overflow.csv"}, on_write_failure="discard")
        state = state.with_output(main_out)
        state = state.with_output(errors_out)
        state = state.with_output(overflow_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_chain" for e in result.errors)

    def test_validate_on_write_failure_valid_no_warning(self) -> None:
        """W7: Valid failsink reference produces no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="errors")
        errors_out = OutputSpec(name="errors", plugin="csv", options={"path": "/errors.csv"}, on_write_failure="discard")
        state = state.with_output(main_out)
        state = state.with_output(errors_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        # No on_write_failure findings at any severity
        assert not any("on_write_failure" in e.message for e in (*result.errors, *result.warnings))

    def test_validate_on_write_failure_discard_no_warning(self) -> None:
        """W7: on_write_failure='discard' is always valid, no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("on_write_failure" in w.message for w in result.warnings)

    # --- W8: on_validation_failure reference validation ---

    def test_validate_on_validation_failure_nonexistent_output_is_error(self) -> None:
        """A dangling quarantine destination raises RouteValidationError at
        runtime init, so Stage 1 reports it as an error."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1", on_validation_failure="nonexistent_sink"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "quarantine_unknown_output" for e in result.errors)

    def test_validate_on_validation_failure_discard_no_warning(self) -> None:
        """W8: on_validation_failure='discard' is always valid, no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1", on_validation_failure="discard"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("on_validation_failure" in w.message for w in result.warnings)

    def test_validate_on_validation_failure_valid_output_no_warning(self) -> None:
        """W8: on_validation_failure references a valid output, no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1", on_validation_failure="quarantine"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_output(
            OutputSpec(name="quarantine", plugin="csv", options={"path": "/quarantine.csv"}, on_write_failure="discard")
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("on_validation_failure" in w.message for w in result.warnings)

    # --- Suggestion rules (S1-S3) ---

    def test_validate_no_error_routing_suggests(self) -> None:
        """S1: Transforms now require on_error (section 7), so a valid pipeline
        always has explicit error routing and S1 cannot fire.  Verify S1 is
        absent when on_error='discard' is set."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main", on_error="discard"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert not any("error routing" in s.message for s in result.suggestions)

    def test_validate_single_output_suggests(self) -> None:
        """S2: Pipeline with single EXTERNAL output gets a backup suggestion.

        Local file sinks (csv, json) don't trigger this because if the
        filesystem fails, a backup file will fail too. External sinks
        (database, azure_blob) benefit from a local recovery file.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # Use external sink (database) to trigger S2 suggestion
        external_output = OutputSpec(
            name="main",
            plugin="database",
            options={"url": "sqlite:///:memory:", "table": "output"},
            on_write_failure="discard",
        )
        state = state.with_output(external_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any("local file output" in s.message for s in result.suggestions)

    def test_validate_single_file_output_no_suggestion(self) -> None:
        """S2: Pipeline with single LOCAL file output gets no backup suggestion.

        Local file sinks don't benefit from a backup file - if the filesystem
        is failing, the backup will fail too.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))  # csv = local file sink
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        # Should NOT suggest backup for local file sinks
        assert not any("local file output" in s.message for s in result.suggestions)

    def test_validate_no_schema_config_suggests(self) -> None:
        """S3: Source without schema_config in options gets a suggestion."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any("no explicit schema" in s.message for s in result.suggestions)

    def test_validate_schema_alias_suppresses_suggestion(self) -> None:
        """S3: Source with composer-facing ``schema`` alias must not trigger the suggestion.

        The composer/runtime boundary uses ``schema`` (user-facing) while
        plugin config parsing normalizes to ``schema_config``. The detection
        helper must accept either alias so correctly configured sources do
        not draw a false advisory through the LLM prompt.
        """
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="quarantine",
        )
        state = self._empty_state()
        state = state.with_source(source)
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("no explicit schema" in s.message for s in result.suggestions)

    def test_validate_schema_config_alias_suppresses_suggestion(self) -> None:
        """S3: Source with internal ``schema_config`` alias also suppresses the suggestion.

        Plugin config parsing may land internal shapes in composer state
        (e.g. after serialization round-trips). Both alias names must be
        recognized so internal and external shapes agree.
        """
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"schema_config": {"mode": "observed"}},
            on_validation_failure="quarantine",
        )
        state = self._empty_state()
        state = state.with_source(source)
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("no explicit schema" in s.message for s in result.suggestions)

    # --- Interaction tests ---

    def test_validate_warnings_dont_block(self) -> None:
        """Warnings don't affect is_valid."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("orphan"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid is True
        assert len(result.warnings) > 0

    def test_validate_errors_and_warnings_coexist(self) -> None:
        """A state with both errors and warnings populates both."""
        state = self._empty_state()
        # No source = error, orphan output = warning
        state = state.with_output(self._make_output("orphan"))
        result = state.validate()
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert any("never receive data" in w.message for w in result.warnings)

    # --- Mandatory field enforcement (section 7 positive checks) ---

    def test_validate_transform_missing_plugin_errors(self) -> None:
        """Transform with plugin=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin=None,
            input="t1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("plugin" in e.message.lower() and "t1" in e.message for e in result.errors)

    def test_validate_transform_missing_on_success_errors(self) -> None:
        """Transform with on_success=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success=None,
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_success" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_transform_blank_on_success_errors(self) -> None:
        """Transform with on_success='' must fail validation (engine rejects blank)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success="",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_success" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_transform_blank_on_error_errors(self) -> None:
        """Transform with on_error='  ' must fail validation (engine rejects blank)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success="main",
            on_error="  ",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_error" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_transform_missing_on_error_errors(self) -> None:
        """Transform with on_error=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success="main",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_error" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_aggregation_missing_trigger_is_end_of_source_only(self) -> None:
        """Aggregation with trigger=None means end-of-source-only flush."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert result.is_valid

    def test_validate_aggregation_empty_trigger_is_end_of_source_only(self) -> None:
        """Aggregation with trigger={} means end-of-source-only flush."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={},
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert result.is_valid

    def test_validate_aggregation_end_of_source_condition_errors(self) -> None:
        """end_of_source must not be accepted in the boolean condition slot."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"condition": "end_of_source"},
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("end_of_source" in e.message and "agg1" in e.message for e in result.errors)

    def test_validate_aggregation_invalid_output_mode_errors(self) -> None:
        """Aggregation with invalid output_mode must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 10},
            output_mode="invalid_mode",
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("output_mode" in e.message and "agg1" in e.message for e in result.errors)

    def test_validate_aggregation_with_trigger_timeout_passes(self) -> None:
        """Aggregation keeps its nested trigger timeout when top-level timeout is forbidden."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 100, "timeout_seconds": 5.0},
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))
        state = state.with_edge(self._make_edge("e2", "agg1", "main"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_validate_aggregation_missing_on_error_errors(self) -> None:
        """Aggregation with on_error=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_error" in e.message and "agg1" in e.message for e in result.errors)

    def test_validate_clean_pipeline_no_warnings(self) -> None:
        """Well-formed pipeline with gates, error routing, schema, and
        multiple outputs has empty warnings and suggestions."""
        state = self._empty_state()
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            # `mode` is REQUIRED by `get_raw_schema_config`, the parser BOTH
            # surfaces share (core/dag/builder.py:161, core/dag/graph.py:199).
            # This fixture previously omitted it and still asserted is_valid —
            # a pipeline the runtime would reject at build time, called clean.
            # Nothing parsed it because Stage 1's schema parse was lazy; the
            # eager syntax sweep (elspeth-33738eedb6) now reaches it.
            options={"path": "/in.csv", "schema_config": {"mode": "observed", "fields": []}},
            on_validation_failure="quarantine",
        )
        state = state.with_source(source)
        state = state.with_node(self._make_transform("t1", "t1", "gate_in"))
        gate = NodeSpec(
            id="gate_1",
            node_type="gate",
            plugin=None,
            input="gate_in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['score'] >= 0.5",
            routes={"true": "main", "false": "errors"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(gate)
        # Use properly configured outputs with paths (W6 semantic completeness)
        main_output = OutputSpec(name="main", plugin="csv", options={"path": "outputs/main.csv"}, on_write_failure="discard")
        errors_output = OutputSpec(name="errors", plugin="csv", options={"path": "outputs/errors.csv"}, on_write_failure="discard")
        quarantine_output = OutputSpec(
            name="quarantine", plugin="csv", options={"path": "outputs/quarantine.csv"}, on_write_failure="discard"
        )
        state = state.with_output(main_output)
        state = state.with_output(errors_output)
        state = state.with_output(quarantine_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "gate_1"))
        state = state.with_edge(self._make_edge("e3", "gate_1", "main", edge_type="route_true"))
        state = state.with_edge(self._make_edge("e4", "gate_1", "errors", edge_type="route_false"))
        result = state.validate()
        assert result.is_valid, result.errors
        assert result.warnings == ()
        assert result.suggestions == ()

    def _gate_pipeline(self, *, condition: str, routes: dict[str, str]) -> CompositionState:
        """Minimal source -> gate -> sink pipeline for route-parity checks."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g1"))
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="g1",
                on_success=None,
                on_error=None,
                options={},
                condition=condition,
                routes=routes,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e0", "source", "g1"))
        return state

    def test_gate_boolean_condition_custom_labels_invalid(self) -> None:
        """Boolean gate condition with non-true/false labels is rejected (parity with GateSettings).

        Regression for elspeth-08e17b9253: composer validate() previously
        green-lit a shape runtime GateSettings.validate_boolean_routes rejects.
        """
        result = self._gate_pipeline(condition="row['x'] > 0", routes={"high": "main", "low": "main"}).validate()
        assert result.is_valid is False
        assert any("boolean condition" in e.message and e.severity == "high" for e in result.errors), [e.message for e in result.errors]

    def test_gate_numeric_condition_invalid(self) -> None:
        """Provably-numeric gate condition can never be a route label; rejected for any labels."""
        result = self._gate_pipeline(condition="row['x'] + 1", routes={"a": "main"}).validate()
        assert result.is_valid is False
        assert any("numeric value" in e.message and e.severity == "high" for e in result.errors), [e.message for e in result.errors]

    def test_gate_boolean_condition_true_false_labels_valid(self) -> None:
        """Boolean gate condition with exactly {true,false} labels stays valid."""
        result = self._gate_pipeline(condition="row['x'] > 0", routes={"true": "main", "false": "main"}).validate()
        assert result.is_valid is True, [e.message for e in result.errors]

    def test_gate_string_route_condition_custom_labels_valid(self) -> None:
        """POSITIVE CONTROL: a string-returning condition with custom labels is NOT over-rejected."""
        result = self._gate_pipeline(
            condition='"high" if row["x"] > 0 else "low"',
            routes={"high": "main", "low": "main"},
        ).validate()
        assert result.is_valid is True, [e.message for e in result.errors]


class TestWebScrapeAbuseContactValidation:
    """Mechanical backstop for skill-prompt rule in pipeline_composer.md.

    Rejects RFC 2606 / RFC 6761 reserved-domain emails in
    `web_scrape.http.abuse_contact`. Pairs with the prompt-level rule that
    forbids fabricating wire-visible identity values; without this validator
    the LLM has unlimited rationalisation room to ship `ops@example.com` and
    similar fabrications. See elspeth-457c8688ef and observation
    obs-69697091d9 for context.
    """

    def _state_with_web_scrape(
        self,
        abuse_contact: str | None,
        *,
        http_present: bool = True,
        plugin: str = "web_scrape",
        options_override: dict[str, Any] | None = None,
    ) -> CompositionState:
        """Build a minimal state with a single transform node carrying the
        given abuse_contact under options.http.

        Other validation rules will report unrelated errors (no source, no
        sinks, etc.); the tests assert only on the abuse_contact rule's
        message presence/absence.
        """
        if options_override is not None:
            options: dict[str, Any] = options_override
        elif http_present:
            http_block: dict[str, Any] = {"scraping_reason": "test", "allowed_hosts": "public_only"}
            if abuse_contact is not None:
                http_block["abuse_contact"] = abuse_contact
            options = {
                "schema": {"mode": "fixed", "fields": ["url: str"]},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "content_fingerprint",
                "format": "markdown",
                "http": http_block,
            }
        else:
            options = {
                "schema": {"mode": "fixed", "fields": ["url: str"]},
                "url_field": "url",
            }
        node = NodeSpec(
            id="fetch_pages",
            node_type="transform",
            plugin=plugin,
            input="url_rows",
            on_success="scraped_content",
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        return CompositionState(
            source=None,
            nodes=(node,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _abuse_contact_error_messages(self, state: CompositionState) -> list[str]:
        return [e.message for e in state.validate().errors if "abuse_contact" in e.message]

    def _web_scrape_identity_error_messages(self, state: CompositionState) -> list[str]:
        return [e.message for e in state.validate().errors if "web_scrape.http." in e.message]

    @pytest.mark.parametrize(
        "address",
        [
            "ops@example.com",
            "compliance@example.com",
            "abuse@example.org",
            "ops@example.net",
            "user@something.test",
            "user@deep.something.test",
            "admin@something.invalid",
            "root@localhost",
            "user@host.localhost",
            "user@something.example",
        ],
    )
    def test_rejects_rfc_reserved_domains(self, address: str) -> None:
        """All RFC 2606/6761 reserved labels and their subdomains must be rejected."""
        state = self._state_with_web_scrape(address)
        messages = self._abuse_contact_error_messages(state)
        assert messages, f"Expected reject for {address!r}, got no abuse_contact error"
        msg = messages[0]
        assert "fabricated identity" in msg
        assert "abuse_contact" in msg

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("abuse_contact", "<OPERATOR_REQUIRED>"),
            ("abuse_contact", "operator required"),
            ("scraping_reason", "<OPERATOR_REQUIRED>"),
            ("scraping_reason", "operator required"),
        ],
    )
    def test_rejects_wire_visible_operator_required_placeholders(self, field_name: str, value: str) -> None:
        """The skill's sentinel values must be blocking validation errors, not persisted defaults."""
        http_block = {
            "abuse_contact": "ops@somecompany.gov.au",
            "scraping_reason": "User-authorised page colour lookup",
            "allowed_hosts": "public_only",
        }
        http_block[field_name] = value
        state = self._state_with_web_scrape(
            None,
            options_override={
                "schema": {"mode": "fixed", "fields": ["url: str"]},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "content_fingerprint",
                "format": "markdown",
                "http": http_block,
            },
        )

        messages = self._web_scrape_identity_error_messages(state)
        assert messages, f"Expected reject for web_scrape.http.{field_name}={value!r}"
        assert f"web_scrape.http.{field_name}" in messages[0]
        assert "placeholder" in messages[0]

    @pytest.mark.parametrize(
        "address",
        [
            "OPS@EXAMPLE.COM",
            "ops@Example.Com",
            "User@SOMETHING.TEST",
        ],
    )
    def test_case_insensitive_reject(self, address: str) -> None:
        """Domain matching must be case-insensitive — uppercase variants are still RFC-reserved."""
        state = self._state_with_web_scrape(address)
        assert self._abuse_contact_error_messages(state), f"Expected reject for {address!r}"

    @pytest.mark.parametrize(
        "address",
        [
            "abuse-contact-unset@elspeth.foundryside.dev",
            "ops@somecompany.gov.au",
            "abuse@example.foundryside.dev",  # 'example' as a label, not the reserved TLD
            "user@reallytest.example-mail.org",  # not endswith ".test" / ".example.org"
            "ops@notlocalhost.com",
            "ops@subdomain.example.io",  # 'example' inside string but not reserved
        ],
    )
    def test_accepts_real_domains(self, address: str) -> None:
        """Real, deliverable domains must pass even when they contain reserved
        labels as substrings (only label-boundary matches count)."""
        state = self._state_with_web_scrape(address)
        assert not self._abuse_contact_error_messages(state), f"Real domain {address!r} was incorrectly rejected"

    def test_skips_non_web_scrape_transform(self) -> None:
        """Rule is plugin-scoped — a passthrough or other transform with an
        accidental http.abuse_contact field is none of this rule's business."""
        state = self._state_with_web_scrape(
            "ops@example.com",
            plugin="passthrough",
        )
        assert not self._abuse_contact_error_messages(state), "Rule should not fire on non-web_scrape plugins"

    def test_skips_when_http_block_missing(self) -> None:
        """When http is absent entirely, the plugin-schema rule reports it; this
        rule must not double-report or fire on a node it cannot inspect."""
        state = self._state_with_web_scrape(None, http_present=False)
        assert not self._abuse_contact_error_messages(state)

    def test_skips_when_abuse_contact_missing(self) -> None:
        """abuse_contact absent inside http — plugin schema flags it; this
        rule remains silent."""
        state = self._state_with_web_scrape(None, http_present=True)
        assert not self._abuse_contact_error_messages(state)

    def test_skips_when_abuse_contact_wrong_type(self) -> None:
        """Non-string value (e.g. a secret_ref dict) — plugin schema handles
        type validation; this rule is value-shape-tolerant."""
        state = self._state_with_web_scrape(
            None,
            options_override={
                "schema": {"mode": "fixed", "fields": ["url: str"]},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "content_fingerprint",
                "format": "markdown",
                "http": {
                    "abuse_contact": {"secret_ref": "ABUSE_CONTACT"},
                    "scraping_reason": "test",
                    "allowed_hosts": "public_only",
                },
            },
        )
        assert not self._abuse_contact_error_messages(state)

    def test_skips_when_email_malformed(self) -> None:
        """No `@` character — let the plugin's email-format rule report it
        (this rule only cares about the domain part of a real-shaped email)."""
        state = self._state_with_web_scrape("not-an-email")
        assert not self._abuse_contact_error_messages(state)

    def test_error_severity_is_high(self) -> None:
        """The rule must produce a blocking (high-severity) error — Tier-1
        audit-integrity defects are not advisory."""
        state = self._state_with_web_scrape("ops@example.com")
        abuse_errors = [e for e in state.validate().errors if "abuse_contact" in e.message]
        assert abuse_errors
        assert abuse_errors[0].severity == "high"

    def test_error_names_the_node_and_field(self) -> None:
        """Message must identify the offending node id and field path so the
        operator (or composer LLM) can locate the violation."""
        state = self._state_with_web_scrape("ops@example.com")
        abuse_errors = [e for e in state.validate().errors if "abuse_contact" in e.message]
        assert abuse_errors
        assert abuse_errors[0].component == "node:fetch_pages"
        assert "web_scrape.http.abuse_contact" in abuse_errors[0].message

    def test_pipeline_with_reserved_address_is_invalid(self) -> None:
        """End-to-end: a fully-formed pipeline carrying a fabricated
        abuse_contact must fail validate() with is_valid=False, regardless of
        otherwise-valid structure."""
        # Single transform node with a reserved-domain address — even with
        # missing source/sinks, is_valid must be False *and* an
        # abuse_contact error must be among the reasons.
        state = self._state_with_web_scrape("ops@example.com")
        result = state.validate()
        assert not result.is_valid
        assert any("abuse_contact" in e.message for e in result.errors)

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("abuse_contact", "<OPERATOR_REQUIRED>"),
            ("abuse_contact", "operator required"),
            ("scraping_reason", "<OPERATOR_REQUIRED>"),
            ("scraping_reason", "operator required"),
        ],
    )
    def test_rejects_wire_visible_identity_placeholders(self, field_name: str, value: str) -> None:
        """Composer validation must block placeholder values before preview/execution."""
        state = self._state_with_web_scrape("ops@somecompany.gov.au")
        http = dict(state.nodes[0].options["http"])
        http[field_name] = value
        options = dict(state.nodes[0].options)
        options["http"] = http
        node = replace(state.nodes[0], options=options)
        state = replace(state, nodes=(node,))

        messages = self._web_scrape_identity_error_messages(state)
        assert messages, f"Expected reject for {field_name}={value!r}, got no web_scrape identity error"
        assert field_name in messages[0]
        assert "placeholder" in messages[0]

    def test_accepts_real_wire_visible_identity_values(self) -> None:
        state = self._state_with_web_scrape("ops@somecompany.gov.au")
        messages = self._web_scrape_identity_error_messages(state)
        assert not messages


class TestPromptTemplateUnboundVariables:
    """LLM prompt templates render with exactly ``{row, lookup}`` under
    StrictUndefined (``PromptTemplate.render``), so a bare ``{{ text }}``
    raises ``TemplateError: Undefined variable`` at runtime and the model
    receives none of the row's data. Composer validation must reject such
    templates with the closed, repairable ``prompt_template_unbound_variables``
    code instead of letting the pipeline crash live (R2-F17 compounding
    finding, elspeth-bea314a89b).
    """

    def _state_with_llm(self, prompt_template: str) -> CompositionState:
        node = NodeSpec(
            id="classify",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="classified",
            on_error="discard",
            options={"prompt_template": prompt_template, "model": "test-model"},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        return CompositionState(
            source=None,
            nodes=(node,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _unbound_errors(self, state: CompositionState) -> list[ValidationEntry]:
        return [e for e in state.validate().errors if e.error_code == "prompt_template_unbound_variables"]

    def test_rejects_bare_variable_template(self) -> None:
        """The acceptance-run shape: every interpolation is an unbound bare name."""
        state = self._state_with_llm("Classify: {{ text }}")
        errors = self._unbound_errors(state)
        assert errors, "Expected prompt_template_unbound_variables for bare {{ text }}"
        entry = errors[0]
        assert entry.component == "node:classify"
        assert entry.severity == "high"
        assert "'text'" in entry.message
        assert "row." in entry.message

    def test_rejects_mixed_template_with_unbound_name(self) -> None:
        """A template can reference row fields AND still crash on a stray bare
        name — StrictUndefined raises on the unbound one regardless."""
        state = self._state_with_llm("Compare {{ row.summary }} against {{ reference }}")
        errors = self._unbound_errors(state)
        assert errors
        assert "'reference'" in errors[0].message
        assert "'summary'" not in errors[0].message

    def test_names_all_unbound_variables_sorted(self) -> None:
        state = self._state_with_llm("{{ zeta }} then {{ alpha }}")
        errors = self._unbound_errors(state)
        assert errors
        assert errors[0].message.index("'alpha'") < errors[0].message.index("'zeta'")

    @pytest.mark.parametrize(
        "template",
        [
            "Classify: {{ row.text }}",
            'Classify: {{ row["Original Header"] }}',
            "Instructions: {{ lookup.instructions }}",
            "Rate how {{interpretation:cool}} this row is.",
            "Rate how {{ interpretation: primary colour }} this page is.",
            "Static prompt with no interpolation at all.",
            "{% set t = row.text %}Classify: {{ t }}",
            "{% for x in row %}{{ x }}{% endfor %}",
            "{{ range(3) | join(', ') }}",  # env global, defined at render time
            "",
        ],
    )
    def test_accepts_bound_or_static_templates(self, template: str) -> None:
        state = self._state_with_llm(template)
        assert not self._unbound_errors(state), f"False positive for {template!r}"

    def test_accepts_local_assigned_in_every_if_branch(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% else %}{% set verdict = "NO" %}{% endif %}{{ verdict }}'

        assert not self._unbound_errors(self._state_with_llm(template))

    def test_rejects_local_assigned_in_only_one_if_branch(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% endif %}{{ verdict }}'

        errors = self._unbound_errors(self._state_with_llm(template))

        assert len(errors) == 1
        assert errors[0].component == "node:classify"
        assert "'verdict'" in errors[0].message

    def test_masked_interpretation_placeholder_does_not_hide_unbound_names(self) -> None:
        """Placeholders are masked before parsing, but bare names elsewhere in
        the same template must still be caught."""
        state = self._state_with_llm("Rate how {{interpretation:cool}} this {{ item }} is.")
        errors = self._unbound_errors(state)
        assert errors
        assert "'item'" in errors[0].message

    def test_syntax_error_template_is_not_this_rules_business(self) -> None:
        """Unparseable templates are reported by other layers; this rule must
        stay silent rather than mask the syntax problem."""
        state = self._state_with_llm("Classify: {{ text")
        assert not self._unbound_errors(state)

    def test_skips_nodes_without_prompt_template(self) -> None:
        node = NodeSpec(
            id="rename",
            node_type="transform",
            plugin="field_mapper",
            input="rows",
            on_success="renamed",
            on_error="discard",
            options={"mapping": {"a": "b"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            source=None,
            nodes=(node,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        assert not self._unbound_errors(state)

    def test_multi_query_nodes_are_owned_by_the_multi_query_rule(self) -> None:
        """With ``queries`` present the multi-query sibling rule owns the node.

        A bare ``{{ text }}`` is unbound in multi-query mode too —
        ``PromptTemplate.render`` wraps the per-query context under ``row``
        (transform.py ``_execute_one_query`` → templates.py ``render``), so the
        binding idiom is ``{{ row.text }}`` with ``text`` an ``input_fields``
        key. The error must therefore still surface, emitted by
        ``_validate_multi_query_template_variable_bindings`` (see
        ``TestMultiQueryTemplateVariableBindings``)."""
        state = self._state_with_llm("Classify {{ text }}.")
        options = dict(state.nodes[0].options)
        options["queries"] = [{"name": "classify", "input_fields": {"text": "body"}}]
        node = replace(state.nodes[0], options=options)
        state = replace(state, nodes=(node,))
        errors = self._unbound_errors(state)
        assert errors, "Bare names crash multi-query renders too — the sibling rule must flag them"
        assert "'text'" in errors[0].message

    def test_skips_non_string_prompt_template(self) -> None:
        """A mistyped prompt_template is the plugin schema's problem — this
        rule only reasons about string templates."""
        state = self._state_with_llm("Classify: {{ row.text }}")
        options = dict(state.nodes[0].options)
        options["prompt_template"] = {"not": "a-string"}
        node = replace(state.nodes[0], options=options)
        state = replace(state, nodes=(node,))
        assert not self._unbound_errors(state)


class TestMultiQueryTemplateVariableBindings:
    """Multi-query LLM templates render with ``row`` bound to the query's
    synthetic context (``build_template_context``: input_fields variables plus
    ``source_row``) and ``lookup`` — under StrictUndefined. Two distinct
    defects must be caught at compose time (elspeth-bea314a89b follow-up):

    * a top-level name outside ``{row, lookup}`` + environment globals never
      binds (same failure as single-prompt; code
      ``prompt_template_unbound_variables``);
    * a ``row.<name>`` reference outside that query's ``input_fields`` keys +
      ``{source_row}`` raises ``Undefined variable`` when that query renders
      (new code ``query_template_unbound_row_fields``).

    Each query's effective template is its ``template`` override when present,
    else the node-level ``prompt_template`` — a node-level template used by no
    well-formed query never renders and must not be flagged.
    """

    def _state(self, prompt_template: str, queries: Any) -> CompositionState:
        node = NodeSpec(
            id="assess",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="assessed",
            on_error="discard",
            options={"prompt_template": prompt_template, "model": "test-model", "queries": queries},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        return CompositionState(
            source=None,
            nodes=(node,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _errors(self, state: CompositionState, code: str) -> list[ValidationEntry]:
        return [e for e in state.validate().errors if e.error_code == code]

    def test_bare_name_in_query_override_is_rejected(self) -> None:
        """The task-shaped defect: an override interpolating a bare input_fields
        variable — the binding idiom is ``{{ row.text }}``, never ``{{ text }}``."""
        state = self._state(
            "Assess: {{ row.input_1 }}",
            [{"name": "classify", "input_fields": {"text": "body", "input_1": "body"}, "template": "Classify {{ text }}"}],
        )
        errors = self._errors(state, "prompt_template_unbound_variables")
        assert errors, "Expected prompt_template_unbound_variables for bare {{ text }} in a query override"
        entry = errors[0]
        assert entry.component == "node:assess"
        assert entry.severity == "high"
        assert "'classify'" in entry.message
        assert "'text'" in entry.message
        assert "input_fields" in entry.message

    def test_legacy_positional_bare_name_in_node_template_flagged_once(self) -> None:
        """The legacy positional idiom ``{{ input_1 }}`` is a bare top-level
        name; a shared node-level template must yield ONE entry, not one per
        query that falls back to it."""
        state = self._state(
            "Assess: {{ input_1 }}",
            {
                "q1": {"input_fields": {"input_1": "col_a"}},
                "q2": {"input_fields": {"input_1": "col_b"}},
            },
        )
        errors = self._errors(state, "prompt_template_unbound_variables")
        assert len(errors) == 1
        assert "'input_1'" in errors[0].message

    def test_node_template_used_by_no_query_is_not_flagged(self) -> None:
        """When every query overrides the template, the node-level
        prompt_template never renders — flagging it would be a false positive
        (the shipped multi-query examples carry exactly this dead slot)."""
        state = self._state(
            "Assess: {{ input_1 }}",
            [{"name": "q1", "input_fields": {"text": "body"}, "template": "Classify {{ row.text }}"}],
        )
        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_unbound_row_field_in_override_is_rejected(self) -> None:
        state = self._state(
            "Assess: {{ row.input_1 }}",
            [
                {
                    "name": "diagnose",
                    "input_fields": {"input_1": "background"},
                    "template": "Background: {{ row.input_1 }} Symptoms: {{ row.input_2 }}",
                }
            ],
        )
        errors = self._errors(state, "query_template_unbound_row_fields")
        assert errors, "Expected query_template_unbound_row_fields for row.input_2 outside input_fields"
        entry = errors[0]
        assert entry.component == "node:assess"
        assert entry.severity == "high"
        assert "'diagnose'" in entry.message
        assert "'input_2'" in entry.message
        assert "'input_1'" in entry.message  # names the bound set so the repair is obvious
        assert "source_row" in entry.message

    def test_bound_variables_source_row_lookup_and_globals_accepted(self) -> None:
        state = self._state(
            "Assess: {{ row.input_1 }}",
            [
                {
                    "name": "q1",
                    "input_fields": {"input_1": "background", "input-2": "symptoms"},
                    "template": (
                        "{{ row.input_1 }} / {{ row['input-2'] }} / {{ row.source_row.raw_column }} "
                        "/ {{ lookup.rubric }} / {{ range(3) | join(', ') }}"
                    ),
                }
            ],
        )
        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_accepts_query_local_assigned_in_every_if_branch(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% else %}{% set verdict = "NO" %}{% endif %}{{ verdict }}'
        state = self._state(
            "Unused node template",
            [{"name": "q1", "input_fields": {"flag": "source_flag"}, "template": template}],
        )

        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_rejects_query_local_assigned_in_only_one_if_branch(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% endif %}{{ verdict }}'
        state = self._state(
            "Unused node template",
            [{"name": "q1", "input_fields": {"flag": "source_flag"}, "template": template}],
        )

        errors = self._errors(state, "prompt_template_unbound_variables")

        assert len(errors) == 1
        assert "'q1'" in errors[0].message
        assert "'verdict'" in errors[0].message
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_accepts_shared_node_template_local_assigned_in_every_if_branch(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% else %}{% set verdict = "NO" %}{% endif %}{{ verdict }}'
        state = self._state(template, {"q1": {"input_fields": {"flag": "source_flag"}}})

        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_rejects_shared_node_template_local_assigned_in_only_one_if_branch(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% endif %}{{ verdict }}'
        state = self._state(template, {"q1": {"input_fields": {"flag": "source_flag"}}})

        errors = self._errors(state, "prompt_template_unbound_variables")

        assert len(errors) == 1
        assert "'verdict'" in errors[0].message
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_shared_node_template_checked_against_each_querys_bindings(self) -> None:
        """The same node-level template can be fine for one query and broken
        for another — the row-field check is per query."""
        state = self._state(
            "Assess: {{ row.input_1 }}",
            {
                "ok_query": {"input_fields": {"input_1": "col_a"}},
                "broken_query": {"input_fields": {"text": "col_b"}},
            },
        )
        errors = self._errors(state, "query_template_unbound_row_fields")
        assert len(errors) == 1
        assert "'broken_query'" in errors[0].message
        assert "'ok_query'" not in errors[0].message

    def test_mapping_form_query_override_is_checked(self) -> None:
        state = self._state(
            "Assess: {{ row.input_1 }}",
            {"classify": {"input_fields": {"input_1": "body"}, "template": "{{ row.nope }}"}},
        )
        errors = self._errors(state, "query_template_unbound_row_fields")
        assert errors
        assert "'classify'" in errors[0].message
        assert "'nope'" in errors[0].message

    def test_malformed_query_entries_are_skipped(self) -> None:
        """Entry shape is QueryDefinition's contract; this rule stays silent on
        malformed entries rather than double-reporting them."""
        state = self._state(
            "Assess: {{ row.input_1 }}",
            [
                "not-a-mapping",
                {"name": "bad_fields", "input_fields": "oops", "template": "{{ row.x }}"},
                {"name": "no_fields", "template": "{{ row.y }}"},
            ],
        )
        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_queries_of_unexpected_shape_are_skipped(self) -> None:
        state = self._state("Assess: {{ row.input_1 }}", "not-a-collection")
        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_syntax_error_override_is_not_this_rules_business(self) -> None:
        state = self._state(
            "Assess: {{ row.input_1 }}",
            [{"name": "q1", "input_fields": {"input_1": "body"}, "template": "Classify {{ row.input_1"}],
        )
        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_non_string_override_skips_the_query(self) -> None:
        """A mistyped ``template`` is QueryDefinition's problem; the query is
        skipped outright — guessing that it falls back to the node template
        would flag a template the (invalid) config never declared it to use."""
        state = self._state(
            "Assess: {{ input_1 }}",
            [{"name": "q1", "input_fields": {"input_1": "body"}, "template": 42}],
        )
        assert not self._errors(state, "prompt_template_unbound_variables")
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_interpretation_placeholder_masked_in_node_template(self) -> None:
        """``{{interpretation:...}}`` placeholders are resolved upstream of
        rendering and must not parse as Jinja2 names — but real defects beside
        them must still be caught."""
        state = self._state(
            "Rate how {{interpretation:severe}} this is: {{ row.input_1 }} vs {{ row.missing }}",
            {"q1": {"input_fields": {"input_1": "body"}}},
        )
        errors = self._errors(state, "query_template_unbound_row_fields")
        assert errors
        assert "'missing'" in errors[0].message
        assert "interpretation" not in errors[0].message

    def test_dynamic_row_access_is_not_flagged(self) -> None:
        """``row[expr]`` cannot be proven unbound at parse time — only the
        concrete names feeding it are checked (here ``selector`` is bound)."""
        state = self._state(
            "Assess: {{ row.input_1 }}",
            [{"name": "q1", "input_fields": {"input_1": "body", "selector": "kind"}, "template": "{{ row[row.selector] }}"}],
        )
        assert not self._errors(state, "query_template_unbound_row_fields")

    def test_single_prompt_nodes_are_not_this_rules_business(self) -> None:
        """Without ``queries`` the single-prompt sibling rule owns the node —
        this rule must not double-report."""
        node_options = {"prompt_template": "Classify {{ text }}", "model": "test-model"}
        node = NodeSpec(
            id="classify",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="out",
            on_error="discard",
            options=node_options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            source=None,
            nodes=(node,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        errors = self._errors(state, "prompt_template_unbound_variables")
        assert len(errors) == 1  # from the single-prompt rule, exactly once
        assert not self._errors(state, "query_template_unbound_row_fields")


class TestSchemaContractValidation:
    """Tests for schema contract validation (pass 9) in CompositionState.validate()."""

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(
        self,
        on_success: str = "t1",
        plugin: str = "csv",
        options: dict[str, Any] | None = None,
        on_validation_failure: str = "discard",
    ) -> SourceSpec:
        opts = dict(options or {})
        if plugin == "csv":
            opts = {"path": "/data/input.csv", **opts}
        elif plugin == "text":
            opts = {"path": "/data/input.txt", "column": "text", **opts}
        return SourceSpec(
            plugin=plugin,
            on_success=on_success,
            options=opts,
            on_validation_failure=on_validation_failure,
        )

    def _make_transform(
        self,
        id: str,
        input: str,
        on_success: str,
        plugin: str = "value_transform",
        options: dict[str, Any] | None = None,
        on_error: str = "discard",
    ) -> NodeSpec:
        opts = dict(options or {})
        if plugin == "value_transform":
            opts = {
                "schema": {"mode": "observed"},
                "operations": [{"target": "_placeholder", "expression": "row['text']"}],
                **opts,
            }
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin=plugin,
            input=input,
            on_success=on_success,
            on_error=on_error,
            options=opts,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_gate(
        self,
        id: str,
        input: str,
        routes: dict[str, str],
        condition: str = "True",
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="gate",
            plugin=None,
            input=input,
            on_success=None,
            on_error=None,
            options={},
            condition=condition,
            routes=routes,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_coalesce(
        self,
        id: str,
        input: str,
        on_success: str | None,
        branches: tuple[str, ...] | None = None,
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="coalesce",
            plugin=None,
            input=input,
            on_success=on_success,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=branches if branches is not None else (input,),
            policy="require_all",
            merge="nested",
        )

    def _make_output(self, name: str = "main") -> OutputSpec:
        return OutputSpec(
            name=name,
            plugin="csv",
            options={"path": f"outputs/{name}.csv", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def _make_typed_edge_state(self, producer_type: str, consumer_type: str) -> CompositionState:
        """Build csv(fixed age:<producer_type>) -> value_transform(fixed age:<consumer_type>) -> sink.

        The two calls differ ONLY in the declared field type, so a test that
        pins the mismatch against its own type-agreeing control cannot pass by
        accident on an unrelated error (elspeth-f2eb8fef9f). See the clean-probe
        rule in the module docstring of the agreement suite.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1_in",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": [f"age: {producer_type}"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1_in",
                "main",
                plugin="value_transform",
                options={
                    "schema": {"mode": "fixed", "fields": [f"age: {consumer_type}"]},
                    "operations": [{"field": "age", "operation": "upper"}],
                },
            )
        )
        return state.with_output(self._make_output("main"))

    def test_edge_field_type_mismatch_is_rejected(self) -> None:
        """A plain producer/consumer field-TYPE conflict must not validate green.

        elspeth-f2eb8fef9f. Stage 1's edge-contract accounting compares field
        NAMES; the runtime compares TYPES via
        ``core/dag/schema_validation.py::validate_single_edge`` ->
        ``contracts/data.py::check_compatibility``. Before this fix a plain
        two-node pipeline whose producer declared ``age: int`` and whose
        consumer declared ``age: str`` returned ``is_valid=True`` with ZERO
        errors — byte-identical to the type-agreeing control below — while the
        DAG build raised ``EdgeContractError`` "Type mismatches: age (expected
        str, got int)". No coalesce, no row_union, no special topology.
        """
        result = self._make_typed_edge_state("int", "str").validate()

        assert not result.is_valid
        assert any(error.error_code == "edge_field_type_incompatible" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def test_edge_field_type_check_does_not_false_red_on_nullable(self) -> None:
        """A nullable-but-required producer field must not be read as a type conflict.

        Regression pin. The first implementation reconstructed both sides as
        PluginSchema models via ``build_coalesce_schema`` and called
        ``check_compatibility``. That factory widens a field to ``X | None``
        when ``fd.nullable or not fd.required`` — correct for coalesce output,
        where a branch can lose a ``last_wins`` collision — but the factory that
        builds ordinary source/transform schemas
        (``plugins/infrastructure/schema_factory.py::_get_python_type``) widens
        ONLY on ``not required`` and never reads ``nullable``. So this pipeline,
        whose real schemas are both plain ``int``, was REJECTED as ``int |
        None`` vs ``int``.

        A false red is worse than the gap it closed: it misdirects the LLM
        authoring loop toward a defect that does not exist, and the runtime
        would have accepted the pipeline.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1_in",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [{"name": "age", "field_type": "int", "required": True, "nullable": True}],
                    }
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1_in",
                "main",
                plugin="value_transform",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [{"name": "age", "field_type": "int", "required": True, "nullable": False}],
                    },
                    "operations": [{"field": "age", "operation": "upper"}],
                },
            )
        )
        result = state.with_output(self._make_output("main")).validate()

        assert not any(error.error_code == "edge_field_type_incompatible" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def test_edge_field_type_agreement_is_accepted(self) -> None:
        """Positive control for :meth:`test_edge_field_type_mismatch_is_rejected`.

        Identical topology and identical field NAME; only the declared type
        agrees. This is what makes the mismatch test falsifiable — without it a
        blanket rejection would pass the test above.
        """
        result = self._make_typed_edge_state("int", "int").validate()

        assert result.is_valid, [(e.error_code, e.message) for e in result.errors]
        assert not any(error.error_code == "edge_field_type_incompatible" for error in result.errors)

    def _make_unreferenced_source_schema_state(self, source_fields: list[str]) -> CompositionState:
        """Build csv(fixed, <source_fields>) -> sink, where the SINK DECLARES NO SCHEMA.

        The absent sink schema is the whole point: Stage 1's
        ``contract_config_invalid`` parse was incidental to contract checking,
        so with nothing to compare against, the source's block was never parsed
        (elspeth-33738eedb6). ``_make_output`` always declares one, so this
        state builds its own sink.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": source_fields}},
            )
        )
        return state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv"},
                on_write_failure="discard",
            )
        )

    def test_malformed_source_schema_is_rejected_without_a_consumer_schema(self) -> None:
        """A malformed field spec must be rejected even when nothing consumes it.

        elspeth-33738eedb6. ``age_no_colon_no_type`` is not a valid field spec.
        Before this fix it validated GREEN whenever no downstream consumer
        declared a schema — because the parse fired only when something forced
        the schema to be resolved for a comparison — and died later at plugin
        construction with ``PluginConfigError``. ``source -> sink`` with a
        plain unschema'd sink is among the most common pipeline shapes.
        """
        result = self._make_unreferenced_source_schema_state(["age_no_colon_no_type"]).validate()

        assert not result.is_valid
        assert any(error.error_code == "contract_config_invalid" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def test_wellformed_source_schema_is_accepted_without_a_consumer_schema(self) -> None:
        """Positive control for the test above — identical but for the field spec."""
        result = self._make_unreferenced_source_schema_state(["age: int"]).validate()

        assert result.is_valid, [(e.error_code, e.message) for e in result.errors]

    def test_malformed_source_schema_is_reported_once(self) -> None:
        """The eager parse must not double-report against the lazy contract parsers.

        With a consumer schema present, both the eager sweep and the contract
        loop's own ``_parse_*`` helpers can observe the same broken spec. One
        authoring defect must yield one error.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": ["age_no_colon_no_type"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv", "schema": {"mode": "fixed", "fields": ["age: int"]}},
                on_write_failure="discard",
            )
        )

        result = state.validate()

        source_config_errors = [
            error for error in result.errors if error.error_code == "contract_config_invalid" and "age_no_colon_no_type" in error.message
        ]
        assert len(source_config_errors) == 1, [(e.component, e.message) for e in result.errors]

    def test_schema_validation_closes_every_constructed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every schema-inspection instance is owned and closed exactly once."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "mapper",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["body: str"],
                        "required_fields": ["body"],
                    },
                    "mapping": {"text": "body"},
                    "select_only": True,
                    "strict": True,
                },
            )
        )
        state = state.with_output(self._make_output())

        state.validate()

        assert len(tracking.instances) >= 2, "fixture did not exercise the schema probe sites"
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)

    def test_declared_field_type_probe_closes_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The standalone producer-field probe owns its constructed transform."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.composer.state import _producer_declared_field_type
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )
        producer = self._make_transform(
            "mapper",
            "mapper_in",
            "main",
            plugin="field_mapper",
            options={
                "schema": {"mode": "fixed", "fields": ["body: str"]},
                "mapping": {"text": "body"},
                "select_only": True,
                "strict": True,
            },
        )

        field_type = _producer_declared_field_type(
            "mapper",
            "field_mapper",
            {},
            node_by_id={"mapper": producer},
            field_name="body",
        )

        assert field_type == "str"
        assert len(tracking.instances) == 1
        assert tracking.instances[0].close_count == 1

    def _make_coalesce_schema_mode_state(
        self,
        *,
        source_schema: dict[str, Any],
        transformed_branch_schema: dict[str, Any],
        merge: str | None = "union",
        policy: str | None = "require_all",
        branch_order: tuple[str, str] = ("path_a", "path_b"),
        branch_plugin: str = "value_transform",
    ) -> CompositionState:
        """Build a legal transformed fork/coalesce shape for schema-mode parity tests."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": source_schema},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        branch_options: dict[str, Any] = {"schema": transformed_branch_schema}
        if branch_plugin == "value_transform":
            branch_options["operations"] = [{"target": "value", "expression": "row['value']"}]
        state = state.with_node(
            self._make_transform(
                "branch_b",
                "path_b",
                "path_b_done",
                plugin=branch_plugin,
                options=branch_options,
            )
        )
        branch_connections = {"path_a": "path_a", "path_b": "path_b_done"}
        state = state.with_node(
            NodeSpec(
                id="merge_results",
                node_type="coalesce",
                plugin=None,
                input="path_a",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={branch_name: branch_connections[branch_name] for branch_name in branch_order},
                policy=policy,
                merge=merge,
            )
        )
        state = state.with_output(self._make_output("main"))
        return state

    def _make_web_scrape_to_line_explode_state(
        self,
        *,
        scrape_options: dict[str, Any] | None = None,
        line_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        scrape_opts = {
            "schema": {"mode": "flexible", "fields": ["url: str"]},
            "required_input_fields": ["url"],
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "format": "text",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "pipeline-tests@elspeth.foundryside.dev",
                "scraping_reason": "test scrape",
                "allowed_hosts": "public_only",
            },
        }
        scrape_opts.update(scrape_options or {})
        split_opts = {
            "schema": {
                "mode": "flexible",
                "fields": [
                    "url: str",
                    "content: str",
                    "content_fingerprint: str",
                ],
            },
            "required_input_fields": ["content"],
            "source_field": "content",
            "output_field": "line",
            "include_index": True,
            "index_field": "line_index",
        }
        split_opts.update(line_options or {})

        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="scrape_in",
                options={"schema": {"mode": "fixed", "fields": ["url: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "scrape_page",
                "scrape_in",
                "explode_in",
                plugin="web_scrape",
                options=scrape_opts,
            )
        )
        state = state.with_node(
            self._make_transform(
                "split_lines",
                "explode_in",
                "main",
                plugin="line_explode",
                options=split_opts,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "scrape_page"))
        state = state.with_edge(self._make_edge("e2", "scrape_page", "split_lines"))
        state = state.with_edge(self._make_edge("e3", "split_lines", "main"))
        return state

    def test_line_explode_rejects_compact_web_scrape_text(self) -> None:
        """A text scrape with the default space separator is not line-framed.

        After Phase 6 the message no longer mentions ``text_separator`` or
        ``\\n`` — fix prose belongs in PluginAssistance, addressed by
        requirement_code. The state-level surface only has to surface the
        structured violation; the agent retrieves prose via
        ``get_plugin_assistance``.
        """
        state = self._make_web_scrape_to_line_explode_state()

        result = state.validate()

        assert not result.is_valid
        assert any(
            error.component == "node:split_lines"
            and "line_explode" in error.message
            and "line_explode.source_field.line_framed_text" in error.message
            for error in result.errors
        )

    def test_line_explode_accepts_newline_framed_web_scrape_text(self) -> None:
        state = self._make_web_scrape_to_line_explode_state(
            scrape_options={"text_separator": "\n"},
        )

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any("line_explode.source_field.line_framed_text" in error.message for error in result.errors)

    def test_line_explode_accepts_markdown_web_scrape_content(self) -> None:
        state = self._make_web_scrape_to_line_explode_state(
            scrape_options={"format": "markdown"},
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def _make_edge(
        self,
        id: str,
        from_node: str,
        to_node: str,
        edge_type: EdgeType = "on_success",
    ) -> EdgeSpec:
        return EdgeSpec(id=id, from_node=from_node, to_node=to_node, edge_type=edge_type, label=None)

    def test_fixed_schema_satisfies_requirement(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        assert not any("contract" in e.message.lower() for e in result.errors)

    def test_text_explicit_guaranteed_fields_satisfies_requirement(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                plugin="text",
                options={
                    "column": "text",
                    "schema": {"mode": "observed", "guaranteed_fields": ["text"]},
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_field_mapper_computed_output_contract_satisfies_sink_requirement(self) -> None:
        """Composer preview must honor field_mapper's computed output contract."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"text": "body"},
                    "strict": True,
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.from_id == "map_body"
        assert sink_contract.producer_guarantees == ("body",)
        assert sink_contract.consumer_requires == ("body",)
        assert sink_contract.satisfied is True

    def test_named_non_first_source_contract_violation_is_reported(self) -> None:
        """Schema validation must inspect every named source, not only the compatibility source."""
        state = CompositionState(
            source=None,
            sources={
                "customers": self._make_source(
                    on_success="customer_rows",
                    options={"schema": {"mode": "fixed", "fields": ["customer_id: str"]}},
                    on_validation_failure="discard",
                ),
                "orders": self._make_source(
                    on_success="order_rows",
                    plugin="json",
                    options={"schema": {"mode": "fixed", "fields": ["refund_id: str"]}},
                    on_validation_failure="discard",
                ),
            },
            nodes=(
                self._make_transform(
                    "validate_orders",
                    "order_rows",
                    "main",
                    options={"required_input_fields": ["order_id"]},
                ),
            ),
            edges=(),
            outputs=(self._make_output("main"), self._make_output("customer_rows")),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not result.is_valid
        contract = next(edge for edge in result.edge_contracts if edge.to_id == "validate_orders")
        assert contract.from_id == "source:orders"
        assert contract.producer_guarantees == ("refund_id",)
        assert contract.consumer_requires == ("order_id",)
        assert contract.missing_fields == ("order_id",)
        assert any(
            error.component == "source:orders" and "'source:orders' -> 'validate_orders'" in error.message for error in result.errors
        )

    def test_contract_probe_constructor_exception_falls_back_instead_of_crashing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Constructor-time probe failures must not escape Stage 1 validation."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"text": "body"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        class _BrokenManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise TemplateError("invalid template syntax")

            def get_transforms(self) -> list[type]:
                # ADR-007: composer now queries the plugin registry to compute
                # the known-pass-through set. For this mock, return an empty
                # list so the probe-failure path takes the non-pass-through
                # branch (medium warning, raw_guaranteed fallback) — matches
                # the v2 behavior the surrounding test pins.
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _BrokenManager(),
        )

        result = state.validate()

        # The probe failure surfaces as a warning, never a crash. The raw
        # fallback for this mapper abstains (observed schema, no guarantees,
        # no participation), so the sink required-fields check defers to
        # runtime per-row validation — no error, no edge-contract verdict —
        # mirroring validate_sink_required_fields' abstention clause.
        assert result.is_valid, result.errors
        assert any("computed contract probe" in warning.message.lower() for warning in result.warnings)
        assert not any(ec.to_id == "output:main" for ec in result.edge_contracts)

    def test_contract_probe_unexpected_constructor_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Framework bugs in transform constructors must not be certified by raw fallback."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "fixed", "fields": ["body: str"]},
                    "mapping": {"text": "body"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "fixed", "fields": ["body: str"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        class _BrokenManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise RuntimeError("framework bug inside transform __init__")

            def get_transforms(self) -> list[type]:
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _BrokenManager(),
        )

        with pytest.raises(RuntimeError, match="framework bug inside transform __init__"):
            state.validate()

    def test_rule_c_unexpected_constructor_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rule C must apply the same probe-exception discipline as its siblings.

        Rule C (per-transform self-consistency for ``field_mapper`` with
        ``select_only: True``) constructs the transform to read its computed
        emit set. When that construction raises an unexpected exception (i.e.
        not in the closed set adjudicated by ``_is_config_probe_exception``),
        the discipline established by f3137ae8 — and already implemented by
        the producer-probe sites at ``state.py:884`` and ``state.py:1057`` and
        the semantic-validator helpers in ``_semantic_validator.py`` — is
        that the exception MUST propagate so the bug surfaces at composer-time
        rather than being silently deferred to ``/execute``. Per CLAUDE.md
        (plugin-as-system-code policy: a plugin method that raises is a bug
        we MUST know about), Rule C swallowing every exception with a bare
        ``except Exception: continue`` would conceal genuine framework bugs.

        ``_check_schema_contracts`` is invoked directly (rather than via
        ``state.validate()``) because the orchestration in ``validate()`` runs
        ``validate_semantic_contracts`` *before* the schema-contract pass, and
        ``_instantiate_consumer`` already implements the discipline — so a
        ``state.validate()``-level test would see the exception propagate from
        the earlier pass regardless of Rule C's behaviour, and silently miss
        the regression. Calling ``_check_schema_contracts`` in isolation pins
        Rule C as the discipline under test.

        Pipeline shape: source → field_mapper(select_only=True, declares an
        output field absent from the mapping) → sink. The field_mapper's
        upstream is the source sentinel (no producer-probe call), and the
        sink uses ``mode: observed`` with no required_fields (so neither
        sink-Rule-A nor sink-Rule-B reaches ``_producer_emit_profile``). Rule C
        is therefore the only probe site that calls ``create_transform``
        for the broken plugin.
        """
        from elspeth.web.composer.state import _check_schema_contracts

        source = self._make_source(
            on_success="map_select",
            plugin="text",
            options={"schema": {"mode": "observed"}},
        )
        field_mapper_node = self._make_transform(
            "map_select",
            "map_select",
            "main",
            plugin="field_mapper",
            options={
                "schema": {
                    "mode": "fixed",
                    "fields": ["body: str", "batch_size: int"],
                    "required_fields": ["body", "batch_size"],
                },
                "mapping": {"text": "body"},
                "select_only": True,
                "strict": True,
            },
        )
        sink = OutputSpec(
            name="main",
            plugin="csv",
            options={
                "path": "outputs/main.csv",
                "schema": {"mode": "observed"},
            },
            on_write_failure="discard",
        )

        class _BrokenManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise RuntimeError("framework bug inside field_mapper __init__")

            def get_transforms(self) -> list[type]:
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _BrokenManager(),
        )

        with pytest.raises(RuntimeError, match="framework bug inside field_mapper __init__"):
            _check_schema_contracts({"source": source}, (field_mapper_node,), (sink,))

    def test_contract_probe_redacts_exception_detail_from_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression (P2c): the constructor-time exception message is the
        plugin author's free-form text (plugin options, DSN fragments,
        filesystem paths, occasionally a mis-typed secret) and MUST NOT
        be surfaced to the preview response. The warning surfaced to the
        composer UI carries only ``type(exc).__name__`` — the class name
        is enough triage signal ("something about this plugin's config
        is wrong") without leaking the option values through a Stage 1
        preview endpoint.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"text": "body"},
                    "api_key": {"secret_ref": "CONSTRUCTION_PROBE_SECRET_SENTINEL"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        # A representative secret-bearing exception message: an API URL
        # with a bearer token fragment, a DSN, and a filesystem path.
        # Production constructors have raised all three shapes.
        leaked_substrings = (
            "Authorization: Bearer sk-SUPER-SECRET-TOKEN-123",
            "postgres://admin:hunter2@db.internal:5432/prod",  # secret-scan: allow-this-line
            "/home/appuser/.ssh/id_rsa",
            "CONSTRUCTION_PROBE_SECRET_SENTINEL",
        )

        class _LeakyManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise TemplateError(f"plugin '{plugin_name}' failed to initialize: " + " | ".join(leaked_substrings))

            def get_transforms(self) -> list[type]:
                # ADR-007: composer now queries the plugin registry for the
                # known-pass-through set. Empty list keeps the probe-failure
                # path on the v2 (non-pass-through) branch — the redaction
                # test pins that path specifically.
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _LeakyManager(),
        )

        result = state.validate()

        # The warning still fires (triage signal preserved).
        probe_warnings = [w for w in result.warnings if "computed contract probe" in w.message.lower()]
        assert probe_warnings, "Probe-failure warning must still be emitted"
        # But none of the exception detail leaks into the message.
        for warning in probe_warnings:
            for leak in leaked_substrings:
                assert leak not in warning.message, (
                    f"Contract warning leaked plugin-option detail: {warning.message!r} "
                    f"contained {leak!r}. str(exc) must be replaced with type(exc).__name__."
                )
            # And the class name IS present (triage surface intact).
            assert "TemplateError" in warning.message

    def test_text_heuristic_rescues_original_bug_scenario(self) -> None:
        """Reported text-source scenario passes via the shared observed-text rule."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                plugin="text",
                options={"column": "text", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={
                    "required_input_fields": ["text"],
                    "operations": [
                        {
                            "target": "combined",
                            "expression": "row['text'] + ' world'",
                        }
                    ],
                },
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()
        assert result.is_valid, result.errors
        edge_contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert edge_contract.satisfied is True
        assert edge_contract.producer_guarantees == ("text",)
        assert edge_contract.consumer_requires == ("text",)

    def test_no_required_input_fields_skips_check(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_empty_required_input_fields_skips_to_schema_fallback(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": []},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_source_direct_to_sink_records_contract(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        result = state.validate()
        assert result.is_valid, result.errors
        assert len(result.edge_contracts) == 1
        sink_contract = result.edge_contracts[0]
        assert sink_contract.from_id == "source"
        assert sink_contract.to_id == "output:main"
        assert sink_contract.satisfied is True

    def test_sink_required_fields_satisfied(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        result = state.validate()
        assert result.is_valid, result.errors
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.satisfied is True
        assert "text" in sink_contract.consumer_requires

    def test_consumer_schema_required_fields_satisfied(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"schema": {"mode": "observed", "required_fields": ["text"]}},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        edge_contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert edge_contract.satisfied is True
        assert edge_contract.consumer_requires == ("text",)

    def test_observed_schema_no_guarantees_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema contract violation" in e.message.lower() for e in result.errors)
        assert any("text" in e.message for e in result.errors)

    def test_partial_match_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text", "score"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("score" in e.message for e in result.errors)

    def test_optional_field_not_guaranteed(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str?"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)

    def test_no_schema_config_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source(options={}))
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema contract violation" in e.message.lower() for e in result.errors)

    def test_malformed_schema_emits_error(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "invalid_mode"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema" in e.message.lower() for e in result.errors)

    def test_sink_required_fields_violation_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        result = state.validate()
        assert not result.is_valid
        assert any("sink" in e.message.lower() and "text" in e.message.lower() for e in result.errors)
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.satisfied is False
        assert "text" in sink_contract.missing_fields

    def test_sink_required_fields_abstaining_producer_defers_to_runtime(self) -> None:
        """An abstaining producer must not fail the sink required-fields check.

        Mirror of the runtime abstention clause in
        ``core/dag/schema_validation.py::validate_sink_required_fields``:
        when the producer's guarantee vote is (no fields, did not participate),
        the static check defers to per-row runtime validation instead of
        rejecting. A select_only field_mapper with an observed schema and no
        local guaranteed_fields abstains exactly this way — the tutorial's
        accepted transform chain (elspeth-3283f2eaec) was permanently blocked
        because the composer hard-failed where the runtime would build and run.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": ["url", "summary"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"url": "url", "summary": "summary"},
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["url"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        # No static claim either way: the edge renders as "not yet checked",
        # not as a satisfied contract the composer cannot actually vouch for.
        assert not any(ec.to_id == "output:main" for ec in result.edge_contracts)

    @pytest.mark.parametrize(
        ("proven_sources", "expected_targets", "expected_missing"),
        [
            (("raw_url", "raw_summary"), ("summary", "url"), ()),
            (("raw_url",), ("url",), ("summary",)),
        ],
    )
    def test_guided_select_only_mapper_declares_only_proven_guarantees(
        self,
        proven_sources: tuple[str, ...],
        expected_targets: tuple[str, ...],
        expected_missing: tuple[str, ...],
    ) -> None:
        """Guided cleanup earns a positive verdict without guessing missing fields."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": list(proven_sources)}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"raw_url": "url", "raw_summary": "summary"},
                    "schema": {"mode": "observed", "guaranteed_fields": list(proven_sources)},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["url", "summary"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert result.is_valid is (not expected_missing)
        assert sink_contract.satisfied is (not expected_missing)
        assert sink_contract.producer_guarantees == expected_targets
        assert sink_contract.missing_fields == expected_missing

    def test_sink_required_fields_inherited_participation_still_fails(self) -> None:
        """A pass-through downstream of a participating producer cannot abstain.

        Runtime parity (``walk_effective_guarantee_vote``): a pass-through
        transform's participation is its OWN vote OR any predecessor's. Here
        the source participates with explicit zero guarantees
        (``guaranteed_fields: []``) and feeds an own-abstaining passthrough,
        so the runtime marks the sink's upstream as participated-with-empty
        and ``validate_sink_required_fields`` rejects the missing field. The
        composer must reject too — treating this as abstention would pass
        preview for a pipeline the runtime refuses to build.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": []}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("text" in e.message and "output:main" in e.message for e in result.errors)
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.satisfied is False
        assert "text" in sink_contract.missing_fields

    def test_contract_probe_ignores_authoring_metadata(self) -> None:
        """Composer-only authoring keys must not break the contract probe.

        The guided flow stages ``interpretation_requirements`` inside node
        options; every plugin config rejects unknown keys, so probing with
        unstripped options is a guaranteed ValueError -> a spurious
        "Computed contract probe ... failed" warning surfaced to the user
        (and a fail-closed Stage-1 rejection for pass-through plugins).
        The probe must strip authoring metadata the same way YAML export does.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": ["url", "summary"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"url": "url", "summary": "summary"},
                    "schema": {"mode": "observed"},
                    "interpretation_requirements": [
                        {
                            "id": "drop_raw_html_review",
                            "kind": "pipeline_decision",
                            "user_term": "drop_raw_html_fields",
                            "status": "resolved",
                            "draft": "Drop the scraped raw HTML fields.",
                            "event_id": "e831b8ee-2c3e-449b-975e-d213fb7eecad",
                            "accepted_value": "Drop the scraped raw HTML fields.",
                            "accepted_artifact_hash": "871d0cf160b91d8dadc0f58504a86bbed14b9fe28d05bdd0a7c918b1e5b07aa9",
                        }
                    ],
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["url"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        assert not any("contract probe" in w.message.lower() for w in result.warnings), [w.message for w in result.warnings]

    def test_rule_c_self_consistency_fires_through_authoring_metadata(self) -> None:
        """Rule C must not be silently skipped by composer-only option keys.

        The per-node select_only self-consistency check probes the plugin
        constructor; with ``interpretation_requirements`` left in options the
        probe raised (extra keys forbidden) and Rule C silently skipped the
        exact guided nodes it exists to check. The probe must strip authoring
        metadata like every other contract probe.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": ["url"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"url": "url"},
                    # Declares 'bogus' as a required output field the mapping
                    # will never emit -> Rule C violation.
                    "schema": {"mode": "fixed", "fields": ["url: str", "bogus: str"]},
                    "interpretation_requirements": [
                        {
                            "id": "drop_fields_review",
                            "kind": "pipeline_decision",
                            "user_term": "drop_fields",
                            "status": "resolved",
                            "draft": "Keep only url.",
                            "event_id": "00000000-0000-0000-0000-000000000001",
                            "accepted_value": "Keep only url.",
                            "accepted_artifact_hash": "0" * 64,
                        }
                    ],
                },
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert any("Transform contract violation" in e.message and "bogus" in e.message for e in result.errors), [
            e.message for e in result.errors
        ]

    def test_consumer_schema_required_fields_violation_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={
                    "required_input_fields": [],
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)

    def test_malformed_consumer_schema_emits_error(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"schema": {"mode": "invalid_mode"}},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema" in e.message.lower() for e in result.errors)
        assert not any(ec.to_id == "t1" for ec in result.edge_contracts)

    def test_multiple_transforms_can_share_sink_target(self) -> None:
        """Shared sink targets stay outside the internal producer namespace.

        Uses ``mode: flexible`` for t1/t2 input contracts: this test exercises
        shared-sink-target wiring, not strict input schemas. With ``mode: fixed``
        the auto-injected ``_placeholder`` field that ``_make_transform`` adds
        via its default value_transform operation would be rejected by t2's
        locked input contract (Rule A) — surfacing as a real runtime violation
        but unrelated to the wiring property under test.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "t2",
                options={
                    "required_input_fields": ["text"],
                    "schema": {"mode": "flexible", "fields": ["text: str"]},
                },
                on_error="errors",
            )
        )
        state = state.with_node(
            self._make_transform(
                "t2",
                "t2",
                "main",
                options={
                    "required_input_fields": ["text"],
                    "schema": {"mode": "flexible", "fields": ["text: str"]},
                },
                on_error="errors",
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(
            OutputSpec(
                name="errors",
                plugin="csv",
                options={
                    "path": "outputs/errors.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "t2"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any("duplicate producer" in e.message.lower() for e in result.errors)
        error_sink_contracts = [ec for ec in result.edge_contracts if ec.to_id == "output:errors"]
        assert len(error_sink_contracts) == 2
        assert all(ec.satisfied for ec in error_sink_contracts)

    def test_same_gate_multiple_routes_to_same_sink_emit_one_contract(self) -> None:
        """Composer preview dedupes indistinguishable gate->sink contract rows."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="router",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "errors", "false": "errors"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="errors",
                plugin="csv",
                options={
                    "path": "outputs/errors.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "router"))

        result = state.validate()

        assert result.is_valid, result.errors
        error_sink_contracts = [ec for ec in result.edge_contracts if ec.to_id == "output:errors"]
        assert len(error_sink_contracts) == 1
        assert error_sink_contracts[0].from_id == "source"
        assert error_sink_contracts[0].satisfied is True

    def test_coalesce_placeholder_input_is_not_counted_as_consumer(self) -> None:
        """Coalesce.input is a composer placeholder, not a runtime consumer claim."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_a", "branch_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("ta", "branch_a", "a_out"))
        state = state.with_node(self._make_transform("tb", "branch_b", "b_out"))
        state = state.with_node(
            NodeSpec(
                id="merge",
                node_type="coalesce",
                plugin=None,
                input="branch_a",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"branch_a": "a_out", "branch_b": "b_out"},
                policy="require_all",
                merge="nested",
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "fork_gate"))
        state = state.with_edge(self._make_edge("e2", "fork_gate", "ta"))
        state = state.with_edge(self._make_edge("e3", "fork_gate", "tb"))
        state = state.with_edge(self._make_edge("e4", "ta", "merge"))
        state = state.with_edge(self._make_edge("e5", "tb", "merge"))
        state = state.with_edge(self._make_edge("e6", "merge", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any("duplicate consumer" in e.message.lower() for e in result.errors)

    # --- Topology cases ---

    def test_gate_inherits_source_guarantees(self) -> None:
        """Gate route targets inherit source guarantees through walk-back."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "main", "false": "errors"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "main",
                "out",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out"))
        state = state.with_output(self._make_output("errors"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "t1"))

        result = state.validate()

        assert result.is_valid, result.errors
        t1_contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert t1_contract.from_id == "source"
        assert t1_contract.satisfied is True

    def test_route_gate_two_routes_inherit_guarantees(self) -> None:
        """Both route-gate paths inherit the same upstream guarantees."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "path_a", "false": "path_b"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "path_a",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "out_b",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_output(self._make_output("out_b"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert result.is_valid, result.errors
        consumer_contracts = [ec for ec in result.edge_contracts if ec.to_id in {"ta", "tb"}]
        assert len(consumer_contracts) == 2
        assert {ec.from_id for ec in consumer_contracts} == {"source"}
        assert all(ec.satisfied for ec in consumer_contracts)

    def test_fork_gate_contract_check_skips_with_warning(self) -> None:
        """Fork-gate downstream contract checks stay unresolved with a warning."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "path_a",
                "mid_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "mid_b",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="rejoin",
                node_type="coalesce",
                plugin=None,
                input="mid_a",
                on_success="merged",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"path_a": "mid_a", "path_b": "mid_b"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(self._make_output("merged"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert any("fork" in w.message.lower() and "contract" in w.message.lower() for w in result.warnings)
        assert not any(ec.to_id in {"ta", "tb"} for ec in result.edge_contracts)

    @pytest.mark.parametrize("branch_order", [("path_a", "path_b"), ("path_b", "path_a")])
    def test_union_coalesce_rejects_mixed_observed_explicit_branch_schemas_regardless_of_order(
        self,
        branch_order: tuple[str, str],
    ) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            branch_order=branch_order,
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_schema_mode_mixed"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"
        assert entries[0].severity == "high"
        assert "observed" in entries[0].message.lower()
        assert "explicit" in entries[0].message.lower()

    @pytest.mark.parametrize(
        ("source_schema", "transformed_branch_schema"),
        [
            ({"mode": "observed"}, {"mode": "observed"}),
            (
                {"mode": "fixed", "fields": ["id: int", "value: int"]},
                {"mode": "fixed", "fields": ["id: int", "value: int"]},
            ),
        ],
    )
    def test_union_coalesce_accepts_homogeneous_branch_schema_modes(
        self,
        source_schema: dict[str, Any],
        transformed_branch_schema: dict[str, Any],
    ) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema=source_schema,
            transformed_branch_schema=transformed_branch_schema,
        )

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_nested_coalesce_allows_mixed_branch_schema_modes(self) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            merge="nested",
        )

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_union_coalesce_abstains_when_a_branch_schema_mode_is_unresolved(self) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            branch_plugin="not_registered",
        )

        result = state.validate()

        assert not any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_union_coalesce_unresolved_branch_does_not_hide_known_mixed_modes(self) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
        )
        gate = next(node for node in state.nodes if node.id == "fork_gate")
        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        state = state.with_node(replace(gate, fork_to=("path_a", "path_b", "path_c")))
        state = state.with_node(
            self._make_transform(
                "branch_c",
                "path_c",
                "path_c_done",
                plugin="not_registered",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            replace(
                coalesce,
                branches={"path_a": "path_a", "path_b": "path_b_done", "path_c": "path_c_done"},
            )
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_schema_mode_mixed"]
        assert len(entries) == 1, result.errors

    def test_union_coalesce_rejects_incompatible_shared_field_types(self) -> None:
        """Stage 1 mirrors the runtime's union type-compatibility rule.

        Battery round-6 g03 (elspeth-85f3cc3022): the composer declared the
        same field with different types on two branches it authored in one
        ``set_pipeline`` call. Stage 1 reported ``is_valid=True``, so the
        mutation envelope told the compose loop the pipeline was clean and it
        stopped; only the DAG build rejected it. The type rule is now checked
        where the authoring surface can act on it.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"
        assert entries[0].severity == "high"
        assert "value" in entries[0].message
        assert "'int'" in entries[0].message
        assert "'str'" in entries[0].message

    def test_union_coalesce_type_entry_carries_structured_repair_facts(self) -> None:
        """The planner's feedback strips messages, so the facts must be structured.

        Without these the closed code names the failing NODE but never the
        FIELD, and the repair is unreachable for a field a plugin contributed
        rather than the author declaring it.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "coalesce_union_type_incompatible")
        assert entry.coalesce_union_type is not None
        detail = entry.coalesce_union_type
        assert detail.field == "value"
        assert {detail.branch_a, detail.branch_b} == {"path_a", "path_b"}
        assert {detail.type_a, detail.type_b} == {"int", "str"}
        assert entry.to_dict()["coalesce_union_type"] == {
            "field": "value",
            "branch_a": detail.branch_a,
            "type_a": detail.type_a,
            "branch_b": detail.branch_b,
            "type_b": detail.type_b,
        }

    def test_union_coalesce_type_conflict_is_policy_independent(self) -> None:
        """The verdict must not depend on the coalesce policy.

        Composer derives ``require_all`` from the policy alone while the
        runtime uses ``has_all_branch_semantics``. The two cannot disagree on
        a composer-authored pipeline (no ``quorum_count`` field exists), but
        this pins the stronger property the mirror actually relies on: the type
        conflict is raised before ``require_all`` is read at all, so no policy
        can turn the rejection on or off.
        """
        for policy in ("require_all", "quorum", "best_effort", "first"):
            state = self._make_coalesce_schema_mode_state(
                source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
                transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            )
            coalesce = next(node for node in state.nodes if node.id == "merge_results")
            state = state.with_node(replace(coalesce, policy=policy))

            result = state.validate()

            assert any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors), (
                f"policy={policy} did not reject",
            )

    def test_union_coalesce_accepts_compatible_shared_field_types(self) -> None:
        """Identical declared types across branches stay valid."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_union_coalesce_type_check_abstains_on_unresolved_branch(self) -> None:
        """One resolvable branch is not enough to prove a conflict."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            branch_plugin="not_registered",
        )

        result = state.validate()

        assert not any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors)

    def test_union_coalesce_mode_mixed_suppresses_the_type_entry(self) -> None:
        """The runtime raises the mode conflict first, so only it is reported.

        Emitting both would hand the repair loop a second, phantom target on a
        node whose real defect is the mode mismatch.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
        )

        result = state.validate()

        assert any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)
        assert not any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors)

    def test_nested_coalesce_ignores_incompatible_shared_field_types(self) -> None:
        """Only union merge merges typed branch fields; nested keys by branch."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            merge="nested",
        )

        result = state.validate()

        assert not any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors)

    def test_unset_coalesce_merge_normalizes_to_the_runtime_default(self) -> None:
        """An omitted ``merge`` becomes ``"union"``, because that is what the runtime runs.

        ``CoalesceSettings.merge`` defaults to ``"union"`` (``core/config.py``),
        so a coalesce authored without the optional field IS a union merge at
        run time. Carrying ``None`` through composer state made every union
        rule read the node as "not a union" and skip it. Normalising once at
        construction is what keeps a THIRD union rule, added later, from
        inheriting the same hole.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge=None,
        )

        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        assert coalesce.merge == "union"
        # Non-coalesce nodes keep None: ``merge`` is forbidden on them, and
        # row_union rejects the field outright (``row_union_field_forbidden``).
        assert next(node for node in state.nodes if node.id == "fork_gate").merge is None

    def test_unset_coalesce_merge_survives_yaml_generation(self) -> None:
        """The unset field crashed YAML generation outright — not, as assumed, ``merge: null``.

        ``to_dict()`` emits ``merge`` only when it is set, while
        ``yaml_generator`` reads ``c["merge"]`` unconditionally, so an unset
        merge raised ``KeyError`` before pydantic ever saw the config. That
        made the runtime's answer an internal crash rather than a repair
        signal. Normalising at construction closes the path that runs through
        ``state.to_dict()``; a caller injecting its own ``state_dict`` still
        bypasses ``NodeSpec`` entirely.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge=None,
        )

        node_dict = next(node for node in state.to_dict()["nodes"] if node["id"] == "merge_results")
        assert node_dict["merge"] == "union"
        assert "merge: union" in generate_yaml(state)

    def test_unset_coalesce_policy_normalizes_to_the_runtime_default(self) -> None:
        """An omitted ``policy`` becomes the runtime's own default, read from the model.

        The equality is against ``CoalesceSettings.model_fields["policy"]``
        rather than the literal ``"require_all"`` deliberately: the value of
        normalising is that the two surfaces cannot disagree about what an
        omitted field means, so if the runtime ever changes its default this
        test tracks it instead of pinning a stale copy.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy=None,
        )

        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        assert coalesce.policy == CoalesceSettings.model_fields["policy"].default
        # Non-coalesce nodes keep None: ``policy`` is forbidden on them, and the
        # splice canonical-transform check reads it as a disqualifier.
        assert next(node for node in state.nodes if node.id == "fork_gate").policy is None

    def test_unset_coalesce_policy_stays_valid(self) -> None:
        """The runtime runs a policy-less coalesce as require_all, so Stage 1 must accept it.

        ``CoalesceSettings.policy`` DEFAULTS to ``"require_all"``, and the
        production loader parses a policy-less coalesce without complaint.
        Stage 1 nonetheless emitted ``coalesce_missing_policy``
        (elspeth-deb2f5ed93) — a validate-red/runtime-green divergence, the
        inverse of the shapes this surface usually carries, and the one
        violation of the placement rule that Stage 1 models the runtime's
        treatment rather than inventing one.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy=None,
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_invalid_coalesce_policy_still_rejected(self) -> None:
        """Retiring the missing-policy code must not weaken the closed-vocabulary guard.

        The two checks shared an ``if``/``elif`` chain, so deleting the first
        arm restructures the second. A committed value outside the runtime's
        vocabulary is still valid-but-not-runnable and must still be rejected.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy="require_all_branches",
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_policy_invalid"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"

    def test_unset_coalesce_policy_survives_yaml_generation(self) -> None:
        """The normalised value must be byte-visible in the exported YAML.

        This is the whole disclosure channel: no advisory replaces the retired
        error, so ``policy: require_all`` in the generated settings is what
        tells an author which arrival semantics they got. It also pins the
        normalisation's PLACEMENT — ``to_dict`` bypasses ``validate()``
        entirely, so a fix applied inside ``validate()`` would leave this red,
        exactly as the same seam did for ``merge``.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy=None,
        )

        node_dict = next(node for node in state.to_dict()["nodes"] if node["id"] == "merge_results")
        assert node_dict["policy"] == "require_all"
        assert "policy: require_all" in generate_yaml(state)

    def test_union_type_rule_applies_to_a_coalesce_with_merge_unset(self) -> None:
        """The type mirror must fire on the runtime's default merge, not only the declared one.

        Shipped by the very commit that closed the previous union divergence
        (elspeth-85f3cc3022): both union mirrors gated on ``merge == "union"``,
        so an LLM that simply omitted the optional field got ``is_valid=True``
        on a pipeline the runtime rejects. Omission is the CHEAPEST thing an
        authoring model does, which made the gap the likeliest path through
        the surface rather than an exotic one.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            merge=None,
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"
        assert entries[0].coalesce_union_type is not None
        assert entries[0].coalesce_union_type.field == "value"

    def test_mode_rule_applies_to_a_coalesce_with_merge_unset(self) -> None:
        """The mode mirror shares the gate, so it shares the repair."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            merge=None,
        )

        result = state.validate()

        assert any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_unset_coalesce_merge_stays_valid_when_branch_types_agree(self) -> None:
        """POSITIVE CONTROL — applying the union rules must not become rejecting the node.

        The runtime ACCEPTS an unset merge; it defaults it. Stage 1 must
        therefore run the union rules over the node, never reject it for
        having no explicit merge. Without this control a blanket rejection
        would satisfy every other test in this group.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge=None,
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_fork_gate_direct_sink_contract_checked(self) -> None:
        """Fork branches that terminate at sinks stay statically checkable."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                plugin="text",
                options={
                    "path": "/in.txt",
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={},
                fork_to=("main",),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "/out.csv",
                    "schema": {"mode": "fixed", "fields": ["text: str"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(EdgeSpec(id="e2", from_node="g1", to_node="main", edge_type="fork", label="main"))

        result = state.validate()

        assert not result.is_valid
        assert not any("fork" in w.message.lower() and "contract" in w.message.lower() for w in result.warnings)
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.from_id == "source"
        assert sink_contract.consumer_requires == ("text",)
        assert sink_contract.satisfied is False

    def test_fork_branch_name_cannot_overlap_coalesce_branch_and_sink(self) -> None:
        """Composer must reject branch names that runtime routes to coalesce before sink."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("main", "review"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_coalesce(
                "merge",
                "branches",
                "merged",
                branches=("main", "review"),
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("merged"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(EdgeSpec(id="e2", from_node="g1", to_node="main", edge_type="fork", label="main"))
        state = state.with_edge(EdgeSpec(id="e3", from_node="g1", to_node="merge", edge_type="fork", label="review"))

        result = state.validate()

        assert not result.is_valid
        assert any("Connection names overlap with sink names" in error.message and "main" in error.message for error in result.errors)

    def test_multi_hop_transform_no_schema_breaks_chain(self) -> None:
        """A schema-less transform breaks downstream guarantees across hops."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="source_to_ta",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "source_to_ta",
                "ta_out",
                plugin="passthrough",
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "ta_out",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "ta"))
        state = state.with_edge(self._make_edge("e2", "ta", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)
        tb_contract = next(ec for ec in result.edge_contracts if ec.to_id == "tb")
        assert tb_contract.from_id == "ta"
        assert tb_contract.satisfied is False

    def test_transform_then_gate_walk_back_terminates(self) -> None:
        """Walk-back stops at the first non-gate producer in the chain."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="ta_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "ta_in",
                "gate_in",
                plugin="passthrough",
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"high": "tb_in", "low": "sink"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "tb_in",
                "out",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out"))
        state = state.with_output(self._make_output("sink"))
        state = state.with_edge(self._make_edge("e1", "source", "ta"))
        state = state.with_edge(self._make_edge("e2", "ta", "g1"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)
        tb_contract = next(ec for ec in result.edge_contracts if ec.to_id == "tb")
        assert tb_contract.from_id == "ta"
        assert tb_contract.satisfied is False

    def test_multi_sink_gate_routing(self) -> None:
        """Route gates emit one satisfied sink contract per direct sink target."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "sink_a", "false": "sink_b"},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="sink_a",
                plugin="csv",
                options={
                    "path": "outputs/sink_a.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="sink_b",
                plugin="csv",
                options={
                    "path": "outputs/sink_b.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "sink_a", edge_type="route_true"))
        state = state.with_edge(self._make_edge("e3", "g1", "sink_b", edge_type="route_false"))

        result = state.validate()

        assert result.is_valid, result.errors
        sink_contracts = [ec for ec in result.edge_contracts if ec.to_id in {"output:sink_a", "output:sink_b"}]
        assert len(sink_contracts) == 2
        assert {ec.to_id for ec in sink_contracts} == {"output:sink_a", "output:sink_b"}
        assert {ec.from_id for ec in sink_contracts} == {"source"}
        assert all(ec.satisfied for ec in sink_contracts)

    def test_mixed_consumer_requirements_from_same_producer(self) -> None:
        """One upstream can satisfy one route and fail another."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "path_a", "false": "path_b"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "path_a",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "out_b",
                options={"required_input_fields": ["score"]},
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_output(self._make_output("out_b"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("score" in e.message for e in result.errors)
        ta_contract = next(ec for ec in result.edge_contracts if ec.to_id == "ta")
        assert ta_contract.satisfied is True
        tb_contract = next(ec for ec in result.edge_contracts if ec.to_id == "tb")
        assert tb_contract.satisfied is False
        assert "score" in tb_contract.missing_fields

    def test_aggregation_consumer_required_input_fields_fail(self) -> None:
        """Aggregation consumers honor required_input_fields contracts."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={
                    "value_field": "value",
                    "required_input_fields": ["value"],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        assert any("value" in e.message for e in result.errors)

    def test_aggregation_required_input_fields_rejected_even_when_upstream_satisfies_contract(self) -> None:
        """ADR-013 has no batch-aware pre-emission dispatch for required_input_fields."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["amount: float"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error="discard",
                options={
                    "value_field": "amount",
                    "required_input_fields": ["amount"],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output(name="main"))
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        messages = "\n".join(entry.message for entry in result.errors)
        assert "required_input_fields" in messages
        assert "batch-aware" in messages
        agg_contract = next(ec for ec in result.edge_contracts if ec.to_id == "agg1")
        assert agg_contract.satisfied is True

    def test_distribution_profile_unknown_value_type_warns_to_sample_or_use_top_k(self) -> None:
        """Observed upstream schema cannot prove value_field is numeric before execute."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="profile_in",
                options={
                    "schema": {
                        "mode": "observed",
                        "guaranteed_fields": ["community", "financial_barrier"],
                    }
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="profile_barriers",
                node_type="aggregation",
                plugin="batch_distribution_profile",
                input="profile_in",
                on_success="main",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "value_field": "financial_barrier",
                    "group_by": "community",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "profile_barriers"))
        state = state.with_edge(self._make_edge("e2", "profile_barriers", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        warnings = [entry for entry in result.warnings if entry.component == "node:profile_barriers"]
        assert any(
            warning.severity == "high"
            and "batch_distribution_profile.value_field.numeric" in warning.message
            and "batch_top_k" in warning.message
            for warning in warnings
        )

    def test_aggregation_nested_wrapper_required_input_fields_fail(self) -> None:
        """Aggregation wrapper-shaped options.required_input_fields is honored."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={
                    "options": {
                        "value_field": "value",
                        "required_input_fields": ["value"],
                        "schema": {"mode": "observed"},
                    }
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        assert any("value" in e.message for e in result.errors)
        agg_contract = next(ec for ec in result.edge_contracts if ec.to_id == "agg1")
        assert agg_contract.consumer_requires == ("value",)
        assert agg_contract.satisfied is False

    def test_aggregation_nested_wrapper_schema_required_fields_fail(self) -> None:
        """Aggregation wrapper-shaped options.schema.required_fields is honored."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={
                    "options": {
                        "value_field": "value",
                        "schema": {"mode": "observed", "required_fields": ["value"]},
                    }
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        assert any("value" in e.message for e in result.errors)
        agg_contract = next(ec for ec in result.edge_contracts if ec.to_id == "agg1")
        assert agg_contract.consumer_requires == ("value",)
        assert agg_contract.satisfied is False

    def test_aggregation_non_mapping_wrapper_options_surface_as_validation_error(self) -> None:
        """A non-Mapping ``options.options`` wrapper value surfaces as a high-severity
        ValidationEntry, not a silent fallback to the flat outer options.

        Pins the S-6 behavioral improvement: the inline duplication previously here
        silently fell through to the outer options when ``node.options["options"]``
        existed but was not a Mapping. The canonical helper
        ``get_aggregation_contract_options`` raises ValueError on that shape, and the
        ``_check_schema_contracts`` call site converts the error into a blocking
        ``ValidationEntry`` so misconfigured wrappers cannot bypass locked-input
        membership checks.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={"options": "not-a-mapping"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        wrapper_errors = [e for e in result.errors if e.component == "node:agg1" and "Invalid contract config" in e.message]
        assert wrapper_errors, (
            "Expected a high-severity 'Invalid contract config' error on node:agg1 "
            f"for the non-Mapping wrapper, got: {[(e.component, e.message) for e in result.errors]}"
        )
        assert any(e.severity == "high" for e in wrapper_errors)

    def test_coalesce_producer_emits_skip_warning(self) -> None:
        """A NON-UNION coalesce producer stays unresolved until runtime validation.

        ``_make_coalesce`` builds ``merge="nested"``, so this now pins the
        deliberate scope boundary of elspeth-ae83a6b60c rather than a blanket
        coalesce abstention: a UNION coalesce resolves through the guarantee
        walk (``TestUnionCoalesceGuaranteeExtras``), while nested keys the
        merged schema by branch name and select forwards one branch's raw
        schema — semantics the propagation vote does not mirror, so abstaining
        with this advisory is the correct answer for them. Do not "fix" this
        test by widening the union gate; that would invent top-level guarantees
        and reject pipelines the runtime runs.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="branch_a",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_coalesce(
                "after_merge",
                "branch_a",
                None,
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "after_merge",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "after_merge"))
        state = state.with_edge(self._make_edge("e2", "after_merge", "t1"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert any("coalesce node" in w.message.lower() and "runtime validator will check" in w.message.lower() for w in result.warnings)
        assert not any(ec.to_id == "t1" for ec in result.edge_contracts)

    # --- Guard tests ---

    def test_node_id_source_is_reserved(self) -> None:
        """A node cannot reuse the source sentinel id."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "source",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "source"))

        result = state.validate()

        assert not result.is_valid
        assert any("reserved" in e.message.lower() for e in result.errors)

    def test_node_id_source_namespace_prefix_is_reserved(self) -> None:
        """Nodes cannot collide with named-source producer ids such as source:orders."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "source:orders",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "source:orders"))

        result = state.validate()

        assert not result.is_valid
        assert any(error.component == "node:source:orders" and "source producer namespace" in error.message for error in result.errors)

    @pytest.mark.parametrize("source_name", ["Orders", "bad name", "continue", "__system", "x" * 39])
    def test_plural_source_names_follow_runtime_identifier_constraints(self, source_name: str) -> None:
        """Composer Stage 1 rejects names that runtime settings would reject later."""
        state = CompositionState(
            source=None,
            sources={source_name: self._make_source("main")},
            nodes=(),
            edges=(),
            outputs=(self._make_output("main"),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not result.is_valid
        assert any(error.component in {"source", f"source:{source_name}"} for error in result.errors)

    def test_bare_string_required_input_fields_emits_error(self) -> None:
        """Bare-string required_input_fields fails closed."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": "text"},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        assert not result.is_valid
        assert any("bare string" in e.message.lower() for e in result.errors)

    def test_duplicate_producer_connection_emits_error_and_skips_contracts(self) -> None:
        """Duplicate producers fail closed instead of overwriting the namespace."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"a": "dup", "b": "path_b"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "dup",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "dup",
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("duplicate producer" in e.message.lower() for e in result.errors)
        assert result.edge_contracts == ()

    def test_duplicate_consumer_connection_emits_error_and_skips_contracts(self) -> None:
        """Duplicate consumers fail closed instead of fabricating edge checks."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="shared",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "shared",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "shared",
                "out_b",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_output(self._make_output("out_b"))
        state = state.with_edge(self._make_edge("e1", "source", "ta"))
        state = state.with_edge(self._make_edge("e2", "source", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("duplicate consumer" in e.message.lower() for e in result.errors)
        assert result.edge_contracts == ()

    def test_connection_name_overlaps_sink_name_emits_error_and_skips_contracts(self) -> None:
        """Connection/sink namespace overlap aborts contract telemetry."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
            )
        )
        state = state.with_node(
            self._make_transform(
                "t2",
                "main",
                "out",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("out"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "t2"))

        result = state.validate()

        assert not result.is_valid
        assert any("disjoint" in e.message.lower() or "overlap" in e.message.lower() for e in result.errors)
        assert result.edge_contracts == ()

    # --- Data integrity ---

    def test_edge_contracts_populated_correctly(self) -> None:
        """ValidationSummary.edge_contracts carries the expected edge data."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert len(result.edge_contracts) >= 1
        contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert contract.from_id == "source"
        assert contract.producer_guarantees == ("text",)
        assert contract.consumer_requires == ("text",)
        assert contract.missing_fields == ()
        assert contract.satisfied is True

    def test_edge_contract_to_dict_serialization(self) -> None:
        """A real emitted EdgeContract serializes with API-facing keys."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        payload = contract.to_dict()
        assert payload["from"] == "source"
        assert payload["to"] == "t1"
        assert payload["producer_guarantees"] == ["text"]
        assert payload["consumer_requires"] == ["text"]
        assert payload["missing_fields"] == []
        assert payload["satisfied"] is True
        assert "from_id" not in payload
        assert "to_id" not in payload

    # --- Field-set membership tests (elspeth-3d25355784) ---
    #
    # The three S3 evaluation fixtures (msg{1,2,3}.json captured under
    # /tmp/elspeth_eval/2026-05-03/s3/) are the ground-truth reproducers for
    # the composer-time membership checks. Each surfaces a different rejection
    # shape that previously slipped past /validate (is_valid: true) and only
    # crashed at /execute with a structured engine error. The test cases below
    # mirror those YAMLs so the regression locks in the rejection at the
    # composer boundary.

    def _batch_stats_to_locked_sink_state(self) -> CompositionState:
        """v1 reproducer: ``batch_stats`` → locked-mode JSON sink."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "ticket_id: str",
                            "subject: str",
                            "body: str",
                            "customer_tier: str",
                            "amount: float",
                        ],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="results",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": "outputs/ticket_totals_by_tier.json",
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "format": "json",
                    "indent": 2,
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )
        return state

    def test_v1_locked_sink_rejects_upstream_batch_size_extra(self) -> None:
        """Sink ``mode: fixed`` rejects ``batch_size`` emitted by upstream batch_stats.

        Reproduces /tmp/elspeth_eval/2026-05-03/s3/msg1.json. The composer
        previously accepted this YAML; the engine then crashed at sink_write
        with PluginContractViolation (``Extra inputs are not permitted:
        batch_size``). The new field-set membership check rejects this at
        /validate with a message that names ``batch_size`` — the same field
        the engine names — and points the operator at both fixes.
        """
        state = self._batch_stats_to_locked_sink_state()

        result = state.validate()

        assert not result.is_valid, "Composer must reject locked sink that forbids producer-emitted extras."
        sink_extra_errors = [
            e for e in result.errors if e.component == "output:results" and "batch_size" in e.message and "input is locked" in e.message
        ]
        assert sink_extra_errors, f"Expected sink locked-input rejection naming batch_size, got: {[e.message for e in result.errors]}"
        msg = sink_extra_errors[0].message
        assert "Extra fields rejected by sink input contract: [batch_size]" in msg
        assert "mode: flexible" in msg  # operator-actionable: relax sink schema
        assert "field_mapper" in msg and "select_only: true" in msg  # operator-actionable: drop extras upstream

    def test_v2_field_mapper_select_only_with_inconsistent_declared_output(self) -> None:
        """Rule C: field_mapper declares an output field its mapping won't emit.

        Reproduces /tmp/elspeth_eval/2026-05-03/s3/msg2.json. The composer
        previously accepted this YAML; the engine crashed at the schema
        config mode contract with SchemaConfigModeViolation (``missing
        required fields ['batch_size']``). The runtime check expects the
        emitted row to satisfy the declared output schema, but with
        ``select_only: true`` the actual emit is exactly ``mapping.values()``
        — which excludes ``batch_size``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "ticket_id: str",
                            "subject: str",
                            "body: str",
                            "customer_tier: str",
                            "amount: float",
                        ],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="select_output_fields",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "select_output_fields",
                "select_output_fields",
                "results",
                plugin="field_mapper",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": [
                            "batch_size: int",
                            "count: int",
                            "customer_tier: str",
                            "sum: float",
                        ],
                        "required_fields": ["customer_tier", "count", "sum"],
                    },
                    "required_input_fields": ["customer_tier", "count", "sum"],
                    "mapping": {
                        "customer_tier": "customer_tier",
                        "count": "count",
                        "sum": "sum",
                    },
                    "select_only": True,
                    "strict": True,
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": "outputs/ticket_totals_by_tier.json",
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "format": "json",
                    "indent": 2,
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )

        result = state.validate()

        assert not result.is_valid, "Composer must reject field_mapper whose declared output won't be emitted."
        rule_c_errors = [
            e
            for e in result.errors
            if e.component == "node:select_output_fields" and "Transform contract violation" in e.message and "batch_size" in e.message
        ]
        assert rule_c_errors, f"Expected Rule C self-consistency rejection naming batch_size, got: {[e.message for e in result.errors]}"
        msg = rule_c_errors[0].message
        assert "select_only: true" in msg
        assert "Declared required output fields not produced by this transform: [batch_size]" in msg

    # ── Rule D: declared output collides with a definitely-arriving input field ──
    # elspeth-cfcd333f83. The runtime surface is TransformExecutor._run_preflight's
    # detect_field_collisions call, which raises PluginContractViolation on row 1.

    def _llm_options(self, response_field: str) -> dict[str, Any]:
        """Minimal constructible llm transform config for the collision probe."""
        return {
            "provider": "gateway",
            "model": "anthropic/claude-sonnet-4.6",
            "endpoint": "https://gateway.example.invalid/v1",
            "api_key": "${LLM_API_KEY}",
            "prompt_template": "Title-case this: {headline}",
            "response_field": response_field,
            "schema": {"mode": "observed"},
        }

    def _llm_rewrite_state(
        self,
        *,
        response_field: str,
        source_plugin: str = "text",
        source_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        """source -> llm -> sink, with the llm writing ``response_field``."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rewrite",
                plugin=source_plugin,
                options=source_options if source_options is not None else {"column": "headline", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rewrite",
                "rewrite",
                "main",
                plugin="llm",
                options=self._llm_options(response_field),
            )
        )
        return state.with_output(self._make_output("main"))

    def test_rule_d_llm_rewrite_in_place_collides_with_source_field(self) -> None:
        """Rule D: an llm rewriting its own input field in place is rejected.

        The reported defect: a text source emits ``headline`` and an llm
        transform sets ``response_field: headline`` to title-case it. Compose
        succeeded and /validate returned is_valid=true, then the run died on
        row 1 because TransformExecutor's collision preflight rejects a
        transform whose declared_output_fields overlap the arriving row.
        """
        result = self._llm_rewrite_state(response_field="headline").validate()

        assert not result.is_valid, "Composer must reject an llm that overwrites a field already on the row."
        rule_d_errors = [e for e in result.errors if e.component == "node:rewrite" and e.error_code == "transform_contract_violation"]
        assert rule_d_errors, f"Expected a Rule D rejection naming headline, got: {[e.message for e in result.errors]}"
        msg = rule_d_errors[0].message
        assert "[headline] already arrive(s) on its input row" in msg
        # Actionable in both repair directions, so the planner can fix it.
        assert "response_field" in msg
        assert "field_mapper" in msg
        # Only the colliding field is reported — the llm's other declared
        # outputs (headline_model / headline_usage) do not arrive on the row.
        contract = rule_d_errors[0].contract
        assert contract is not None
        assert contract.extra_fields == ("headline",)
        assert contract.producer == "rewrite"
        assert contract.consumer == "rewrite"

    def test_rule_d_negative_control_non_colliding_output_authors_cleanly(self) -> None:
        """Rule D stays silent when the declared output is a fresh field name.

        Same topology as the rejection above with only ``response_field``
        changed, so a failure here means Rule D fires on shape rather than on
        the collision itself.
        """
        result = self._llm_rewrite_state(response_field="headline_titlecased").validate()

        assert result.is_valid, result.errors

    def test_rule_d_abstains_when_arrival_is_not_definite(self) -> None:
        """Rule D abstains when the field is not PROVEN to arrive.

        A csv source with an observed schema declares no guaranteed fields, so
        ``_connection_definite_emits`` contributes nothing and the composer
        cannot prove ``headline`` reaches the node. These predicates are lower
        bounds: erroring on a merely-possible collision would be a false
        rejection, so the runtime preflight owns this case per-row.
        """
        result = self._llm_rewrite_state(
            response_field="headline",
            source_plugin="csv",
            source_options={"schema": {"mode": "observed"}},
        ).validate()

        assert result.is_valid, result.errors

    def test_rule_d_fires_when_only_one_row_union_arm_delivers_the_field(self) -> None:
        """Rule D rejects a collision carried by a SINGLE fan-in arm.

        ``_connection_definite_emits`` deliberately UNIONS row_union arm emit
        sets where the presence walk would intersect them, because the two
        directions have opposite safety polarities. A row_union republishes
        each arm's rows unchanged, so a field guaranteed by one arm really is
        present on that arm's rows; the executor's collision preflight runs
        per row and dies on them. Requiring the field on EVERY arm would miss
        this genuine failure, so union — not intersection — is correct here.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                plugin="text",
                options={"column": "headline", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("arm_a_in", "arm_b_in"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        # Only arm A mints `tag`; arm B carries the bare source row.
        state = state.with_node(self._make_transform("tagger", "arm_a_in", "arm_a_out", plugin="llm", options=self._llm_options("tag")))
        state = state.with_node(
            NodeSpec(
                id="union",
                node_type="row_union",
                plugin=None,
                input="arm_a_out",
                on_success="union_out",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                # Barrier branches are keyed by FORK BRANCH NAME; arm B reaches
                # the barrier untransformed, so its key and connection coincide.
                branches={"arm_a_in": "arm_a_out", "arm_b_in": "arm_b_in"},
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("retag", "union_out", "main", plugin="llm", options=self._llm_options("tag")))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        rule_d_errors = [e for e in result.errors if e.component == "node:retag" and e.error_code == "transform_contract_violation"]
        assert rule_d_errors, f"Expected Rule D to reject the single-arm collision, got: {[e.message for e in result.errors]}"
        assert rule_d_errors[0].contract is not None
        assert "tag" in rule_d_errors[0].contract.extra_fields
        # The collision must be the ONLY reason this pipeline is rejected —
        # otherwise a structural complaint could carry the test and the fan-in
        # semantics would go unverified.
        assert [e.error_code for e in result.errors] == ["transform_contract_violation"], [e.message for e in result.errors]

    def test_rule_d_ignores_transforms_declaring_no_output_fields(self) -> None:
        """A transform that declares no output fields is never a Rule D subject.

        ``value_transform`` deliberately keeps ``declared_output_fields`` empty
        because its targets may legitimately be overwrites — the same opt-out
        the executor's collision check honours. Rule D must inherit that
        abstention rather than re-deriving emission from the schema.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rewrite",
                plugin="text",
                options={"column": "headline", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rewrite",
                "rewrite",
                "main",
                options={"operations": [{"target": "headline", "expression": "row['headline'].title()"}]},
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_v3_field_mapper_locked_input_rejects_upstream_batch_size_extra(self) -> None:
        """Rule A: locked-mode field_mapper input rejects upstream batch_stats extra.

        Reproduces /tmp/elspeth_eval/2026-05-03/s3/msg3.json. The composer
        previously accepted this YAML; the engine crashed at input validation
        with PluginContractViolation (``Extra inputs are not permitted:
        batch_size``). The field_mapper's input Pydantic model gets
        ``extra="forbid"`` because its ``schema.mode`` is fixed; upstream
        batch_stats emits ``batch_size`` which is not in the declared
        ``fields``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "ticket_id: str",
                            "subject: str",
                            "body: str",
                            "customer_tier: str",
                            "amount: float",
                        ],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="select_output_fields",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "select_output_fields",
                "select_output_fields",
                "results",
                plugin="field_mapper",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                        "required_fields": ["customer_tier", "count", "sum"],
                    },
                    "required_input_fields": ["customer_tier", "count", "sum"],
                    "mapping": {
                        "customer_tier": "customer_tier",
                        "count": "count",
                        "sum": "sum",
                    },
                    "select_only": True,
                    "strict": True,
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": "outputs/ticket_totals_by_tier.json",
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "format": "json",
                    "indent": 2,
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )

        result = state.validate()

        assert not result.is_valid, "Composer must reject locked field_mapper input that forbids producer-emitted extras."
        consumer_extra_errors = [
            e
            for e in result.errors
            if e.component == "node:select_output_fields" and "input is locked" in e.message and "batch_size" in e.message
        ]
        assert consumer_extra_errors, (
            f"Expected consumer locked-input rejection naming batch_size, got: {[e.message for e in result.errors]}"
        )
        msg = consumer_extra_errors[0].message
        assert "Extra fields rejected by consumer input contract: [batch_size]" in msg
        assert "'aggregate_by_tier' -> 'select_output_fields'" in msg  # producer/consumer attribution
        assert "schema.mode: flexible" in msg  # operator-actionable: relax consumer schema
        assert "schema.fields" in msg  # operator-actionable: widen the field declaration
        assert "['batch_size']" in msg  # message names the specific field to add
        # When consumer IS field_mapper, the "insert a field_mapper" suggestion is degenerate.
        assert "insert a field_mapper" not in msg, "Rule A must not suggest inserting a field_mapper when the consumer is already one."

    def test_locked_input_check_does_not_fire_on_flexible_consumer(self) -> None:
        """Rule A negative: ``mode: flexible`` consumer accepts producer extras.

        Sanity guard against over-generalization: the same upstream
        (batch_stats with batch_size) feeding a ``mode: flexible`` consumer
        does not trigger the locked-input rejection. Only ``mode: fixed``
        produces ``extra="forbid"`` on the auto-generated input contract.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "amount: float"],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="select_output_fields",
                on_error="discard",
                options={
                    "schema": {"mode": "flexible", "fields": ["customer_tier: str", "amount: float"]},
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "select_output_fields",
                "select_output_fields",
                "main",
                plugin="field_mapper",
                options={
                    # Same shape as v3 except mode=flexible — extras allowed.
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "mapping": {
                        "customer_tier": "customer_tier",
                        "count": "count",
                        "sum": "sum",
                    },
                    "select_only": True,
                    "strict": True,
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not any("input is locked" in e.message and "batch_size" in e.message for e in result.errors), (
            f"Flexible consumer must not trigger locked-input rejection, got errors: {[e.message for e in result.errors]}"
        )

    # ── Projected declared_input_fields: a transform's own options name a
    # required input column (elspeth-ada5a60249). The runtime surface is
    # DeclaredRequiredFieldsContract.pre_emission_check, which raises before
    # process() runs, so every row fails.

    def _web_scrape_options(self, url_field: str) -> dict[str, Any]:
        """Minimal constructible web_scrape config for the declared-input probe."""
        return {
            "url_field": url_field,
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                # A non-reserved domain, so the composer's abuse_contact rule
                # stays silent and these tests observe only the contract check.
                "abuse_contact": "ops@somecompany.gov.au",
                "scraping_reason": "contract validation test",
                "allowed_hosts": ["127.0.0.0/8"],
            },
            "schema": {"mode": "observed"},
        }

    def _web_scrape_state(self, *, url_field: str, source_mode: str) -> CompositionState:
        """source -> web_scrape -> sink, with the scraper's url_field under test."""
        source_schema: dict[str, Any] = (
            {"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]} if source_mode == "fixed" else {"mode": "observed"}
        )
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="urls", plugin="csv", options={"schema": source_schema}))
        state = state.with_node(
            self._make_transform(
                "scraper",
                "urls",
                "main",
                plugin="web_scrape",
                options=self._web_scrape_options(url_field),
            )
        )
        return state.with_output(self._make_output("main"))

    def test_declared_input_field_missing_from_typed_producer_is_rejected(self) -> None:
        """The reported defect: a misnamed url_field composed clean and died on row 1.

        ``url_field`` never reaches ``required_input_fields`` or the ``schema:``
        block, so both raw config surfaces were blind; only the constructed
        plugin knows the column name.
        """
        result = self._web_scrape_state(url_field="page_url", source_mode="fixed").validate()

        assert not result.is_valid, "Composer must reject a url_field naming a column no producer emits."
        declared_errors = [
            e
            for e in result.errors
            if e.component == "node:scraper" and e.error_code == "schema_contract_violation" and "page_url" in e.message
        ]
        assert declared_errors, f"Expected a declared-input rejection naming page_url, got: {[e.message for e in result.errors]}"
        msg = declared_errors[0].message
        assert "Missing fields: [page_url]" in msg
        # The message must say WHERE the requirement came from, since the author
        # never wrote `required_input_fields`.
        assert "declared by its own options" in msg
        assert "url_field" in msg

    def test_declared_input_field_satisfied_by_producer_is_clean(self) -> None:
        """Negative control: the correctly wired chaosweb shape must stay valid."""
        result = self._web_scrape_state(url_field="url", source_mode="fixed").validate()

        assert result.is_valid, f"Correctly wired url_field must not be rejected, got: {[e.message for e in result.errors]}"

    def test_declared_input_field_against_observed_producer_abstains(self) -> None:
        """ABSTENTION: an observed producer proves nothing, so enforcement stays per-row.

        This is the case that separates the projected declaration from the raw
        ``required_input_fields`` surface, which fails closed here.
        """
        result = self._web_scrape_state(url_field="page_url", source_mode="observed").validate()

        assert result.is_valid, f"Observed producer must abstain, got: {[e.message for e in result.errors]}"

    def _blob_csv_expand_state(self, *, source_fields: list[str]) -> CompositionState:
        """blob_ref_field OMITTED — its default names 'blob_ref', so nothing in the options says so."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="manifest",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": source_fields}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "expand",
                "manifest",
                "main",
                plugin="blob_csv_expand",
                options={"columns": ["id", "text"], "schema": {"mode": "observed"}},
            )
        )
        return state.with_output(self._make_output("main"))

    def test_declared_input_field_from_option_default_is_rejected(self) -> None:
        """Omission shape: the author wrote no option at all, so only the probe can know.

        Pins that ``prepare_validation_probe_options`` preserves a
        default-derived declaration — nothing in the raw options names
        ``blob_ref``, so a probe that lost the default would leave this rule
        silently inert for every omission-shaped defect.
        """
        result = self._blob_csv_expand_state(source_fields=["manifest_index: int", "source_name: str"]).validate()

        assert not result.is_valid, "Composer must reject a default blob_ref_field no producer emits."
        declared_errors = [e for e in result.errors if e.component == "node:expand" and "blob_ref" in e.message]
        assert declared_errors, f"Expected a declared-input rejection naming blob_ref, got: {[e.message for e in result.errors]}"
        assert "declared by its own options" in declared_errors[0].message

    def test_declared_input_field_from_option_default_satisfied_is_clean(self) -> None:
        """Negative control: the canonical manifest shape guarantees blob_ref."""
        result = self._blob_csv_expand_state(source_fields=["manifest_index: int", "blob_ref: str"]).validate()

        assert result.is_valid, f"Canonical blob manifest must stay valid, got: {[e.message for e in result.errors]}"

    def test_declared_input_probe_closes_every_constructed_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The declared-input probe owns the validation-only transform it constructs."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )

        self._web_scrape_state(url_field="page_url", source_mode="fixed").validate()

        assert tracking.instances, "fixture did not exercise the declared-input probe site"
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)


class TestPassThroughComposerParity:
    """ADR-007 composer parity tests for known-pass-through plugins.

    The composer preview must mirror runtime propagation for pass-through
    plugins. Two behaviours are pinned:

    - Probe succeeds → the producer_guarantees on downstream edges include
      predecessor fields (not just the transform's own declared output).
    - Probe fails for a *known* pass-through plugin → fail-closed with
      high-severity warning, producer_guarantees=(), Stage 1 rejects the
      pipeline (mirroring runtime rejection).
    - Probe fails for a *non*-pass-through plugin → v2 behaviour preserved
      (medium-severity warning, return raw_guaranteed).
    """

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(self, on_success: str, plugin: str = "csv", options: dict[str, Any] | None = None) -> SourceSpec:
        opts = dict(options or {})
        if plugin == "csv":
            opts = {"path": "/data/input.csv", **opts}
        return SourceSpec(
            plugin=plugin,
            on_success=on_success,
            options=opts,
            on_validation_failure="discard",
        )

    def _make_transform(
        self,
        id: str,
        input: str,
        on_success: str,
        plugin: str,
        options: dict[str, Any] | None = None,
        on_error: str = "discard",
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin=plugin,
            input=input,
            on_success=on_success,
            on_error=on_error,
            options=dict(options or {}),
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_edge(self, id: str, from_id: str, to_id: str) -> EdgeSpec:
        return EdgeSpec(id=id, from_node=from_id, to_node=to_id, edge_type="on_success", label=None)

    def test_preview_inherits_upstream_guarantees_when_pass_through_has_no_output_schema_config(self) -> None:
        """Successful passthrough probes still propagate inherited guarantees.

        The built-in passthrough plugin does not populate
        ``_output_schema_config``. Composer preview must therefore treat its
        own declaration set as empty and still apply ADR-007 propagation,
        mirroring ``ExecutionGraph.get_effective_guaranteed_fields()``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="source",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["id: str", "body: str"],
                        "guaranteed_fields": ["id", "body"],
                    }
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "pt_node",
                "source",
                "main",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "pt_node"))
        state = state.with_edge(self._make_edge("e2", "pt_node", "main"))

        result = state.validate()

        assert result.is_valid
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert set(sink_contract.producer_guarantees) == {"id", "body"}
        assert sink_contract.consumer_requires == ("body",)
        assert sink_contract.satisfied is True

    def test_batch_replicate_propagates_upstream_guarantees_to_downstream_consumer(self) -> None:
        """Replicated rows retain the fields guaranteed by their source."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="replicate_in",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "observed",
                        "guaranteed_fields": ["color_name", "hex"],
                    }
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="replicate",
                node_type="aggregation",
                plugin="batch_replicate",
                input="replicate_in",
                on_success="score_in",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "include_copy_index": True,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 10},
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "score_variants",
                "score_in",
                "main",
                plugin="passthrough",
                options={
                    "schema": {"mode": "observed"},
                    "required_input_fields": ["color_name", "hex"],
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "replicate"))
        state = state.with_edge(self._make_edge("e2", "replicate", "score_variants"))
        state = state.with_edge(self._make_edge("e3", "score_variants", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        consumer_contract = next(ec for ec in result.edge_contracts if ec.to_id == "score_variants")
        assert set(consumer_contract.producer_guarantees) == {"color_name", "hex", "copy_index"}
        assert consumer_contract.consumer_requires == ("color_name", "hex")
        assert consumer_contract.satisfied is True

    def test_preview_inherits_upstream_guarantees_through_fork_gate_into_pass_through(self) -> None:
        """Pass-through preview must follow fork branches back to their producer."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["id: str", "body: str"],
                    }
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "overflow"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_transform(
                "pt_node",
                "path_a",
                "pt_out",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="rejoin",
                node_type="coalesce",
                plugin=None,
                input="pt_out",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"path_a": "pt_out"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="overflow",
                plugin="csv",
                options={
                    "path": "outputs/overflow.csv",
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "fork_gate"))
        state = state.with_edge(EdgeSpec(id="e2", from_node="fork_gate", to_node="pt_node", edge_type="fork", label="path_a"))
        state = state.with_edge(EdgeSpec(id="e3", from_node="fork_gate", to_node="overflow", edge_type="fork", label="overflow"))

        result = state.validate()

        # Engine-legal fork chains route branches through a coalesce (or a
        # sink-named branch), and the guarantee walk now RESOLVES a union
        # coalesce instead of deferring to the runtime validator
        # (elspeth-ae83a6b60c): its merged guarantee is the same one the DAG
        # builder stamps, so the contract row is computed rather than
        # fabricated and the skip warning would now be a false "not yet
        # checked" signal. This test therefore pins the end-to-end inheritance
        # it is named for — source guarantees, through the fork gate, through
        # the pass-through, through the merge, to the sink's requirement —
        # which the previous no-contract-row assertion could not see.
        assert result.is_valid, result.errors
        assert not any("coalesce" in w.message.lower() and "skipped" in w.message.lower() for w in result.warnings)
        contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert contract.from_id == "rejoin"
        assert set(contract.producer_guarantees) == {"id", "body"}
        assert contract.consumer_requires == ("body",)
        assert contract.satisfied is True

    def test_preview_fails_closed_when_known_pass_through_constructor_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Probe failure on a known pass-through plugin → Stage 1 rejects pipeline.

        Composer preview must surface high-severity warning and return an
        empty producer_guarantees set, matching the runtime rejection that
        would occur if the transform were constructed at DAG build time.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="pt_node",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": ["id: str", "body: str"], "guaranteed_fields": ["id", "body"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "pt_node",
                "source",
                "main",
                plugin="passthrough",  # Known pass-through plugin
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "pt_node"))
        state = state.with_edge(self._make_edge("e2", "pt_node", "main"))

        # Stub the plugin manager: get_transforms returns a minimal shim with
        # passthrough annotated True; create_transform raises for passthrough.
        class _StubPassThrough:
            name = "passthrough"
            passes_through_input = True
            forwards_input_fields = False
            removed_input_fields = frozenset()
            is_batch_aware = False  # Required by _known_batch_aware_transform_plugins()

        class _StubPluginManager(DelegatingPluginManagerDouble):
            def get_transforms(self) -> list[type]:
                return [_StubPassThrough]

            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise TemplateError("intentional probe failure")

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _StubPluginManager(),
        )

        result = state.validate()

        # Stage 1 rejects because producer guarantees are empty and sink requires 'body'.
        assert not result.is_valid
        high_warnings = [w for w in result.warnings if w.severity == "high"]
        probe_high = [w for w in high_warnings if "computed contract probe" in w.message.lower() and "pass-through" in w.message.lower()]
        assert probe_high, f"Expected a high-severity probe warning mentioning pass-through; got warnings={result.warnings!r}"
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.producer_guarantees == ()
        assert sink_contract.satisfied is False


class TestCompositionStateValidateEmitsSemanticContracts:
    def test_compact_wardline_yields_semantic_error_in_validate(self):
        from tests.unit.web.composer.test_semantic_validator import _wardline_state

        state = _wardline_state(text_separator=" ", scrape_format="text")
        result = state.validate()

        assert result.is_valid is False
        # Wardline-shape with compact text: at least one error tagged with
        # node:explode reflecting the semantic contract violation.
        explode_errors = [e for e in result.errors if e.component == "node:explode"]
        assert any("Semantic contract" in e.message or "line_explode" in e.message for e in explode_errors)

        # And a SemanticEdgeContract record on the summary.
        assert len(result.semantic_contracts) == 1
        assert result.semantic_contracts[0].outcome.value == "conflict"

    def test_passing_wardline_yields_satisfied_contract(self):
        from tests.unit.web.composer.test_semantic_validator import _wardline_state

        state = _wardline_state(text_separator="\n", scrape_format="text")
        result = state.validate()
        # Other validation may pass or fail; what we assert is that
        # the semantic contract is SATISFIED.
        assert any(c.outcome.value == "satisfied" for c in result.semantic_contracts)


class TestCompositionStateQueue:
    """Structural queue fan-in exposure (elspeth-a5b86149d4).

    A declared queue node legalises many-producer -> one-consumer interleave
    (mirroring the runtime `queues:` contract) without relaxing the ordinary
    single-producer / single-consumer rule anywhere else. Its canonical shape
    is id == input, plugin/routing absent, implicit output under its id, and
    description-only options.
    """

    def _queue(self, queue_id: str = "inbound", *, description: str | None = None, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": queue_id,
            "node_type": "queue",
            "plugin": None,
            "input": queue_id,
            "on_success": None,
            "on_error": None,
            "options": {} if description is None else {"description": description},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _source(self, on_success: str = "inbound") -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )

    def _transform(self, node_id: str = "normalize", *, input: str = "inbound", on_success: str = "combined") -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input,
            on_success=on_success,
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _sink(self, name: str = "combined") -> OutputSpec:
        return OutputSpec(name=name, plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard")

    def _state(self, *, sources: dict[str, SourceSpec], nodes: tuple[NodeSpec, ...], outputs: tuple[OutputSpec, ...]) -> CompositionState:
        return CompositionState(
            source=None,
            sources=sources,
            nodes=nodes,
            edges=(),
            outputs=outputs,
            metadata=PipelineMetadata(),
            version=1,
        )

    def _valid_state(self, **queue_overrides: Any) -> CompositionState:
        return self._state(
            sources={"orders": self._source(), "refunds": self._source()},
            nodes=(self._queue(**queue_overrides), self._transform()),
            outputs=(self._sink(),),
        )

    # --- Happy path + serialization ---

    def test_valid_two_source_queue_pipeline(self) -> None:
        result = self._valid_state().validate()
        assert result.is_valid, result.errors

    def test_queue_survives_serialization_round_trip(self) -> None:
        state = self._valid_state(description="Orders and refunds interleave here")
        restored = CompositionState.from_dict(state.to_dict())
        assert restored == state
        queue = next(n for n in restored.nodes if n.node_type == "queue")
        assert queue.id == "inbound" and queue.input == "inbound"
        assert queue.options == {"description": "Orders and refunds interleave here"}

    # --- Intrinsic contract (pure helper parity) ---

    def test_canonical_queue_has_no_intrinsic_error(self) -> None:
        assert queue_node_contract_error(self._queue()) is None
        assert queue_node_contract_error(self._queue(description="hi")) is None

    def test_queue_input_must_equal_id(self) -> None:
        assert "input must equal its id" in (queue_node_contract_error(self._queue(input="other")) or "")
        result = self._valid_state(input="other").validate()
        assert not result.is_valid

    def test_queue_rejects_non_canonical_fields(self) -> None:
        for field, value in (
            ("plugin", "csv"),
            ("on_success", "x"),
            ("on_error", "x"),
            ("condition", "True"),
            ("routes", {"a": "b"}),
            ("fork_to", ("a",)),
            ("policy", "require_all"),
            ("merge", "nested"),
            ("trigger", {"kind": "count"}),
            ("output_mode", "passthrough"),
            ("expected_output_count", 2),
            ("timeout_seconds", 5.0),
        ):
            error = queue_node_contract_error(self._queue(**{field: value}))
            assert error is not None and field in error, f"{field} not rejected: {error}"

    def test_queue_rejects_unknown_option_and_non_string_description(self) -> None:
        assert "unknown option" in (queue_node_contract_error(self._queue(options={"buffer": 10})) or "")
        assert "description must be a string" in (queue_node_contract_error(self._queue(options={"description": 5})) or "")

    # --- Structural topology ---

    def test_queue_requires_at_least_one_producer(self) -> None:
        # No source targets the queue's id -> unreachable / no producer.
        state = self._state(
            sources={"orders": self._source(on_success="elsewhere")},
            nodes=(self._queue(), self._transform(input="inbound", on_success="combined")),
            outputs=(self._sink(),),
        )
        assert not state.validate().is_valid

    def test_queue_requires_a_downstream_consumer(self) -> None:
        # Producers exist but nothing consumes the queue's output.
        state = self._state(
            sources={"orders": self._source(), "refunds": self._source()},
            nodes=(self._queue(),),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert not result.is_valid
        assert any("downstream consumer" in e.message for e in result.errors)

    def test_two_ordinary_consumers_of_a_queue_are_a_duplicate_consumer(self) -> None:
        state = self._state(
            sources={"orders": self._source(), "refunds": self._source()},
            nodes=(
                self._queue(),
                self._transform("c1", input="inbound", on_success="combined"),
                self._transform("c2", input="inbound", on_success="combined2"),
            ),
            outputs=(self._sink(), self._sink("combined2")),
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate consumer" in e.message for e in result.errors)

    def test_queue_id_may_not_collide_with_a_sink(self) -> None:
        state = self._state(
            sources={"orders": self._source(), "refunds": self._source()},
            nodes=(self._queue(), self._transform(input="inbound", on_success="combined")),
            outputs=(self._sink(), self._sink("inbound")),
        )
        assert not state.validate().is_valid

    def test_queue_id_may_not_collide_with_a_source_key(self) -> None:
        state = self._state(
            sources={"inbound": self._source(on_success="elsewhere"), "orders": self._source()},
            nodes=(self._queue(), self._transform(input="inbound", on_success="combined")),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert not result.is_valid

    def test_undeclared_fan_in_without_a_queue_still_reports_duplicate_producer(self) -> None:
        state = self._state(
            sources={"orders": self._source(), "refunds": self._source()},
            nodes=(self._transform(input="inbound", on_success="combined"),),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate producer" in e.message for e in result.errors)


class TestCompositionStateQueueGuaranteePropagation:
    """Stage-1 mirrors engine queue guarantee propagation (elspeth-3619b8774f).

    Since 83a53388a (elspeth-5a372d3267) the engine walker propagates
    effective guarantees through QUEUE nodes: intersection of arm votes when
    every arm participates, total abstention when any arm abstains. The
    composer preview must vote identically — checking a queue consumer's
    required fields against the fan-in intersection instead of abstaining
    with the medium 'Contract check skipped' warning — so validate() agrees
    with the runtime walker in both directions (green where the engine
    passes, red where it rejects).
    """

    def _queue(self, queue_id: str = "inbound") -> NodeSpec:
        return NodeSpec(
            id=queue_id,
            node_type="queue",
            plugin=None,
            input=queue_id,
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _source(self, *, guarantees: list[str] | None = None, on_success: str = "inbound") -> SourceSpec:
        schema: dict[str, Any] = {"mode": "observed"}
        if guarantees is not None:
            schema["guaranteed_fields"] = guarantees
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={"schema": schema},
            on_validation_failure="discard",
        )

    def _consumer(
        self,
        node_id: str = "consumer",
        *,
        input: str = "inbound",
        on_success: str = "combined",
        required: list[str] | None = None,
    ) -> NodeSpec:
        options: dict[str, Any] = {"schema": {"mode": "observed"}}
        if required is not None:
            options["required_input_fields"] = required
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input,
            on_success=on_success,
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _sink(self, name: str = "combined", *, required: list[str] | None = None) -> OutputSpec:
        schema: dict[str, Any] = {"mode": "observed"}
        if required is not None:
            schema["required_fields"] = required
        return OutputSpec(name=name, plugin="json", options={"schema": schema}, on_write_failure="discard")

    def _state(self, *, sources: dict[str, SourceSpec], nodes: tuple[NodeSpec, ...], outputs: tuple[OutputSpec, ...]) -> CompositionState:
        return CompositionState(
            source=None,
            sources=sources,
            nodes=nodes,
            edges=(),
            outputs=outputs,
            metadata=PipelineMetadata(),
            version=1,
        )

    def _queue_skip_warnings(self, result: Any) -> list[str]:
        return [w.message for w in result.warnings if "Contract check skipped" in w.message and "queue" in w.message]

    def test_queue_consumer_requiring_arm_guaranteed_field_validates_without_skip_warning(self) -> None:
        # Battery g08 shape (plugin-neutral): every arm guarantees the field
        # the consumer requires — the engine accepts, so Stage 1 must accept
        # WITHOUT the abstention warning.
        state = self._state(
            sources={
                "orders": self._source(guarantees=["llm_response"]),
                "refunds": self._source(guarantees=["llm_response"]),
            },
            nodes=(self._queue(), self._consumer(required=["llm_response"])),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert result.is_valid, [e.message for e in result.errors]
        assert self._queue_skip_warnings(result) == []

    def test_queue_consumer_requiring_unguaranteed_field_is_rejected(self) -> None:
        # Red-parity direction: the engine rejects this at graph build
        # ("guarantees: (none - dynamic schema)" pre-fix / missing-field
        # post-fix), so Stage 1 must reject it too instead of abstaining.
        state = self._state(
            sources={
                "orders": self._source(guarantees=["llm_response"]),
                "refunds": self._source(guarantees=["llm_response"]),
            },
            nodes=(self._queue(), self._consumer(required=["never_guaranteed"])),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert not result.is_valid
        assert any(e.error_code == "schema_contract_violation" for e in result.errors), [e.message for e in result.errors]

    def test_fan_in_intersects_arm_guarantees(self) -> None:
        # A field only ONE arm guarantees is not guaranteed on the queue's
        # interleaved stream (rows arrive from exactly one arm).
        state = self._state(
            sources={
                "orders": self._source(guarantees=["shared", "only_orders"]),
                "refunds": self._source(guarantees=["shared"]),
            },
            nodes=(self._queue(), self._consumer(required=["only_orders"])),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert not result.is_valid
        assert any(e.error_code == "schema_contract_violation" for e in result.errors)

        shared_state = self._state(
            sources={
                "orders": self._source(guarantees=["shared", "only_orders"]),
                "refunds": self._source(guarantees=["shared"]),
            },
            nodes=(self._queue(), self._consumer(required=["shared"])),
            outputs=(self._sink(),),
        )
        shared_result = shared_state.validate()
        assert shared_result.is_valid, [e.message for e in shared_result.errors]

    def test_abstaining_arm_collapses_queue_vote_and_keeps_skip_warning(self) -> None:
        # One dynamic (no-guarantee) arm collapses the whole vote to
        # abstention — mirroring the engine — and the abstention warning
        # stays: the runtime enforces the requirement per-row, and the
        # warning is the honest "not yet checked" signal.
        state = self._state(
            sources={
                "orders": self._source(guarantees=["llm_response"]),
                "refunds": self._source(),
            },
            nodes=(self._queue(), self._consumer(required=["llm_response"])),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert result.is_valid, [e.message for e in result.errors]
        assert self._queue_skip_warnings(result) != []

    def test_queue_guarantee_flows_through_pass_through_to_sink_check(self) -> None:
        # The _connection_propagation_vote path: a pass-through transform
        # downstream of the queue inherits the fan-in intersection, so the
        # sink's required-fields check resolves instead of abstaining.
        state = self._state(
            sources={
                "orders": self._source(guarantees=["llm_response"]),
                "refunds": self._source(guarantees=["llm_response"]),
            },
            nodes=(self._queue(), self._consumer()),
            outputs=(self._sink(required=["llm_response"]),),
        )
        result = state.validate()
        assert result.is_valid, [e.message for e in result.errors]

        missing_state = self._state(
            sources={
                "orders": self._source(guarantees=["llm_response"]),
                "refunds": self._source(guarantees=["llm_response"]),
            },
            nodes=(self._queue(), self._consumer()),
            outputs=(self._sink(required=["never_guaranteed"]),),
        )
        missing_result = missing_state.validate()
        assert not missing_result.is_valid
        assert any(e.error_code == "sink_contract_violation" for e in missing_result.errors), [e.message for e in missing_result.errors]

    def test_gate_arm_routing_back_into_queue_terminates(self) -> None:
        # Drafts are not DAG-checked at Stage 1: a gate consuming the queue
        # and routing one label back into it makes the fan-in walk cyclic.
        # The vote must terminate (conservative abstention on the revisit),
        # never recurse unboundedly.
        gate = NodeSpec(
            id="triage",
            node_type="gate",
            plugin=None,
            input="inbound",
            on_success=None,
            on_error=None,
            options={},
            condition="row.get('retry') == True",
            routes={"true": "inbound", "false": "combined"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._state(
            sources={"orders": self._source(guarantees=["llm_response"])},
            nodes=(self._queue(), gate),
            outputs=(self._sink(required=["llm_response"]),),
        )
        result = state.validate()  # must not raise RecursionError
        assert result is not None


def test_structural_node_shape_errors_carry_closed_error_codes() -> None:
    """Node-shape validation entries must carry closed error codes.

    The planner repair loop strips validation messages from candidate
    feedback (leak-safety), so ``error_code`` is the only actionable repair
    signal — and ``explain_validation_error`` can only explain what has a
    code. Live regression: fork/coalesce A/B proposals were repaired blind
    against entries whose error_code was None.
    """

    def _node(**overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "n",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "out",
            "on_error": "discard",
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard"),
        nodes=(
            _node(id="t_bad", on_success=None, on_error=None),
            _node(id="c_bad", node_type="coalesce", plugin=None),
            _node(
                id="c_vocab",
                node_type="coalesce",
                plugin=None,
                branches={"a": "a", "b": "b"},
                policy="require_all_branches",
                merge="union_fields",
            ),
            _node(id="g_bad", node_type="gate", plugin=None),
            _node(id="g_half", node_type="gate", plugin=None, condition="True", routes={"true": "out"}),
            _node(id="t_dangling", on_success="nowhere", on_error="missing_sink"),
            _node(
                id="t_novel_decision",
                options={
                    "interpretation_requirements": [
                        {
                            "id": "novel_decision_review",
                            "kind": "pipeline_decision",
                            "user_term": "ab_reconciliation_retention",
                            "status": "pending",
                            "draft": "Retain both variants in the reconciled row.",
                            "event_id": None,
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        }
                    ]
                },
            ),
        ),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="csv", options={}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )

    result = state.validate()
    codes = {(entry.component, entry.error_code) for entry in result.errors}
    for expected in (
        ("node:t_bad", "transform_missing_on_success"),
        ("node:t_bad", "transform_missing_on_error"),
        # ``branches`` has NO runtime default, so it stays required; ``policy``
        # is absent from this list because it HAS one (elspeth-deb2f5ed93).
        ("node:c_bad", "coalesce_missing_branches"),
        ("node:g_bad", "gate_missing_condition"),
        ("node:g_bad", "gate_missing_routes"),
        ("node:g_half", "gate_route_labels_mismatch"),
        ("node:t_dangling", "transform_on_success_dangling"),
        ("node:t_dangling", "transform_on_error_unknown_sink"),
        ("node:t_novel_decision", "pipeline_decision_unregistered"),
        ("node:c_vocab", "coalesce_policy_invalid"),
        ("node:c_vocab", "coalesce_merge_invalid"),
    ):
        assert expected in codes, f"missing {expected}; got {sorted(c for c in codes if c[1])}"


def test_gate_on_error_must_reference_declared_sink_or_discard() -> None:
    gate = NodeSpec(
        id="threshold",
        node_type="gate",
        plugin=None,
        input="rows",
        on_success=None,
        on_error="missing_error_sink",
        options={},
        condition="row['amount'] > 500",
        routes={"true": "high", "false": "standard"},
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard"),
        nodes=(gate,),
        edges=(),
        outputs=(
            OutputSpec(name="high", plugin="csv", options={}, on_write_failure="discard"),
            OutputSpec(name="standard", plugin="csv", options={}, on_write_failure="discard"),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )

    result = state.validate()

    assert ("node:threshold", "gate_on_error_unknown_sink") in {(entry.component, entry.error_code) for entry in result.errors}
    assert route_destination_facts(state)["node:threshold"] == {
        "dangling_on_error": "missing_error_sink",
        "declared_sinks": ["high", "standard"],
    }


def test_gate_fork_branches_must_reach_a_coalesce_branch_or_sink() -> None:
    """Mirror the engine's fork-branch destination rule at composition time.

    Live session d4ea3d8a: committed pipeline failed engine pre-run because
    the gate's fork_to names ('branch_a'/'branch_b') appeared nowhere in any
    coalesce branches keys or sink names — the model keyed coalesce branches
    by incoming connection instead of fork branch name. Composer validation
    accepted it: valid-but-not-runnable.
    """

    def _node(**overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "n",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "out",
            "on_error": "discard",
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard"),
        nodes=(
            _node(
                id="fork_rows",
                node_type="gate",
                plugin=None,
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=["branch_a", "branch_b"],
                on_success=None,
                on_error=None,
            ),
            _node(id="tone", input="branch_a", on_success="tone_out"),
            _node(id="usage", input="branch_b", on_success="usage_out"),
            _node(
                id="reconcile",
                node_type="coalesce",
                plugin=None,
                input="tone_out",
                on_success=None,
                on_error=None,
                branches={"tone_out": "tone_out", "usage_out": "usage_out"},
                policy="require_all",
                merge="union",
            ),
            _node(
                id="row_union",
                node_type="row_union",
                plugin=None,
                input="tone_out",
                on_success="union_out",
                on_error=None,
                branches={"branch_a": "tone_out", "other_branch": "usage_out"},
                policy=None,
                merge=None,
            ),
            _node(id="after_union", input="union_out", on_success="out"),
            _node(id="finalize", input="reconcile", on_success="out"),
        ),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="csv", options={}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )

    result = state.validate()
    entries = [(e.component, e.error_code) for e in result.errors]
    assert ("node:fork_rows", "fork_branch_no_destination") in entries, entries
    offending = [e for e in result.errors if e.error_code == "fork_branch_no_destination"]
    assert len(offending) == 1, offending
    assert "branch_b" in offending[0].message
    assert "fork branch 'branch_a'" not in offending[0].message
    assert "tone_out" in offending[0].message


class TestCompositionStateRowUnion:
    """Composer parity for the plugin-free correlated row_union barrier."""

    def _source(self, on_success: str = "fork_in", *, schema: dict[str, Any] | None = None) -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={"schema": schema or {"mode": "observed"}},
            on_validation_failure="discard",
        )

    def _gate(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "fork_rows",
            "node_type": "gate",
            "plugin": None,
            "input": "fork_in",
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": ("control_branch", "treatment_branch"),
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _transform(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options=options or {"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _llm(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        response_field: str,
    ) -> NodeSpec:
        """An llm arm emitting its guaranteed provenance trio.

        ``llm`` guarantees ``<response_field>`` plus the ``_usage``/``_model``
        side-fields (``LLM_GUARANTEED_SUFFIXES``). Those are row data, not
        audit-only provenance, so they reach a downstream consumer's input
        contract — the shape that produced elspeth-9d13900064.
        """
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="llm",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={
                "schema": {"mode": "observed"},
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "prompt_template": "Judge this row.",
                "api_key": "env:OPENROUTER_API_KEY",
                "response_field": response_field,
                "required_input_fields": [],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _aggregation(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        output_mode: str | None,
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="aggregation",
            plugin="batch_stats",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={
                "schema": {"mode": "observed"},
                "value_field": "value",
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={},
            output_mode=output_mode,
        )

    def _row_union(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "variant_union",
            "node_type": "row_union",
            "plugin": None,
            # Serialized adapter placeholder: the first branch connection.
            "input": "control_done",
            "on_success": "union_out",
            "on_error": None,
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": {
                "control_branch": "control_done",
                "treatment_branch": "treatment_done",
            },
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _output(self, name: str = "output", *, options: dict[str, Any] | None = None) -> OutputSpec:
        return OutputSpec(
            name=name,
            plugin="json",
            options=options or {"schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def _state(
        self,
        *,
        row_union: NodeSpec | None = None,
        gate: NodeSpec | None = None,
        arms: tuple[NodeSpec, ...] | None = None,
        extra_nodes: tuple[NodeSpec, ...] = (),
        tail_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        return CompositionState(
            source=self._source(),
            nodes=(
                gate or self._gate(),
                *(
                    arms
                    or (
                        self._transform("control", "control_branch", "control_done"),
                        self._transform("treatment", "treatment_branch", "treatment_done"),
                    )
                ),
                row_union or self._row_union(),
                self._transform("after_union", "union_out", "output", options=tail_options),
                *extra_nodes,
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_row_union_survives_serialization_round_trip(self) -> None:
        state = self._state(row_union=self._row_union(timeout_seconds=2.5))

        payload = state.to_dict()
        restored = CompositionState.from_dict(payload)

        assert payload["nodes"][3]["timeout_seconds"] == 2.5
        assert restored == state
        assert restored.nodes[3].timeout_seconds == 2.5

    def test_from_dict_normalizes_row_union_branch_list_to_identity_mapping(self) -> None:
        node = NodeSpec.from_dict(
            {
                "id": "variant_union",
                "node_type": "row_union",
                "plugin": None,
                "input": "control_branch",
                "on_success": "union_out",
                "on_error": None,
                "options": {},
                "branches": ["control_branch", "treatment_branch"],
            }
        )

        assert node.branches == {
            "control_branch": "control_branch",
            "treatment_branch": "treatment_branch",
        }

    def test_direct_row_union_branch_list_normalizes_before_round_trip(self) -> None:
        row_union = self._row_union(
            input="control_branch",
            branches=("control_branch", "treatment_branch"),
        )
        state = self._state(row_union=row_union)

        assert row_union.branches == {
            "control_branch": "control_branch",
            "treatment_branch": "treatment_branch",
        }
        assert CompositionState.from_dict(state.to_dict()) == state

    def test_from_dict_does_not_hide_duplicate_row_union_branch_aliases(self) -> None:
        row_union = NodeSpec.from_dict(
            {
                "id": "variant_union",
                "node_type": "row_union",
                "plugin": None,
                "input": "control_branch",
                "on_success": "union_out",
                "on_error": None,
                "options": {},
                "branches": ["control_branch", "treatment_branch", "control_branch"],
            }
        )

        result = self._state(row_union=row_union).validate()

        assert any(error.error_code == "row_union_branches_invalid" for error in result.errors)

    def test_valid_row_union_topology(self) -> None:
        result = self._state().validate()

        assert result.is_valid, result.errors

    @pytest.mark.parametrize("output_mode", [None, "transform"])
    def test_row_union_rejects_transform_mode_aggregation_inside_branch(self, output_mode: str | None) -> None:
        state = self._state()
        branch_aggregation = self._aggregation(
            "control",
            "control_branch",
            "control_done",
            output_mode=output_mode,
        )
        state = replace(
            state,
            nodes=tuple(branch_aggregation if node.id == "control" else node for node in state.nodes),
        )

        result = state.validate()

        error = next(error for error in result.errors if error.error_code == "row_union_branch_aggregation_invalid")
        assert error.component == "node:variant_union"
        assert "control" in error.message
        assert "row_id" in error.message
        assert "passthrough" in error.message

    def test_row_union_accepts_passthrough_aggregation_inside_branch(self) -> None:
        state = self._state()
        branch_aggregation = self._aggregation(
            "control",
            "control_branch",
            "control_done",
            output_mode="passthrough",
        )
        state = replace(
            state,
            nodes=tuple(branch_aggregation if node.id == "control" else node for node in state.nodes),
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_row_union_accepts_transform_mode_aggregation_before_fork(self) -> None:
        state = self._state()
        pre_fork_aggregation = self._aggregation(
            "pre_fork_batch",
            "pre_fork_in",
            "fork_in",
            output_mode="transform",
        )
        state = replace(
            state,
            sources={"source": self._source(on_success="pre_fork_in")},
            nodes=(pre_fork_aggregation, *state.nodes),
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_row_union_rejects_nested_fork_inside_branch(self) -> None:
        nested_gate = self._gate(
            id="nested_fork",
            input="control_branch",
            fork_to=("nested_a", "nested_b"),
        )
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._gate(),
                nested_gate,
                self._transform("control", "nested_a", "control_done"),
                self._transform("treatment", "treatment_branch", "treatment_done"),
                self._row_union(),
                self._transform("after_union", "union_out", "output"),
            ),
            edges=(),
            outputs=(self._output(), self._output("nested_b")),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        error = next(error for error in result.errors if error.error_code == "row_union_nested_fork_invalid")
        assert error.component == "node:variant_union"
        assert "nested_fork" in error.message
        assert "control_branch" in error.message

    def test_gate_fork_aliases_must_be_unique_before_row_union_origin_resolution(self) -> None:
        state = self._state(
            gate=self._gate(
                fork_to=("control_branch", "control_branch", "treatment_branch"),
            )
        )

        result = state.validate()

        error = next(error for error in result.errors if error.error_code == "gate_duplicate_fork_branch")
        assert error.component == "node:fork_rows"
        assert "control_branch" in error.message

    @pytest.mark.parametrize(
        "node_id",
        [
            "bad name",
            "a" * 39,
            "fork",
            "__private",
            " variant_union ",
        ],
    )
    def test_row_union_name_matches_runtime_identifier_contract(self, node_id: str) -> None:
        result = self._state(row_union=self._row_union(id=node_id)).validate()

        error = next(error for error in result.errors if error.error_code == "row_union_name_invalid")
        assert error.component == f"node:{node_id}"

    def test_malformed_row_union_branch_values_return_errors_without_sorting_type_error(self) -> None:
        payload = json.loads(json.dumps(self._state().to_dict()))
        row_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
        row_union["branches"] = {
            "control_branch": 123,
            "treatment_branch": "missing",
        }
        row_union["input"] = 123

        result = CompositionState.from_dict(payload).validate()

        assert not result.is_valid
        assert any(error.error_code == "row_union_branch_invalid" for error in result.errors)

    def test_row_union_rejects_branch_aliases_from_multiple_fork_gates(self) -> None:
        second_gate = self._gate(
            id="fork_treatment",
            input="second_fork_in",
            fork_to=("treatment_branch", "treatment_overflow"),
        )
        state = CompositionState(
            sources={
                "control_source": self._source(on_success="fork_in"),
                "treatment_source": self._source(on_success="second_fork_in"),
            },
            nodes=(
                self._gate(fork_to=("control_branch", "control_overflow")),
                second_gate,
                self._transform("control", "control_branch", "control_done"),
                self._transform("control_overflow", "control_overflow", "control_overflow_done"),
                self._transform("treatment", "treatment_branch", "treatment_done"),
                self._transform("treatment_overflow", "treatment_overflow", "treatment_overflow_done"),
                self._row_union(),
                self._transform("after_union", "union_out", "output"),
            ),
            edges=(),
            outputs=(
                self._output(),
                self._output("control_overflow_done"),
                self._output("treatment_overflow_done"),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        origin_error = next(error for error in result.errors if error.error_code == "row_union_branch_origin_invalid")
        assert "one common gate fork_to" in origin_error.message
        assert "fork_rows" in origin_error.message
        assert "fork_treatment" in origin_error.message
        # A step-8 topology finding, not the intrinsic node-shape code the
        # mutation preflight blocks on.
        assert "row_union_branch_invalid" not in {error.error_code for error in result.errors}

    def test_row_union_rejects_branch_connection_from_a_different_alias(self) -> None:
        row_union = self._row_union(
            input="treatment_done",
            branches={
                "control_branch": "treatment_done",
                "treatment_branch": "control_done",
            },
        )

        result = self._state(row_union=row_union).validate()

        mapping_error = next(error for error in result.errors if error.error_code == "row_union_branch_not_downstream")
        assert "control_branch" in mapping_error.message
        assert "treatment_done" in mapping_error.message
        assert "not downstream" in mapping_error.message
        # A step-8 topology finding, not the intrinsic node-shape code the
        # mutation preflight blocks on.
        assert "row_union_branch_invalid" not in {error.error_code for error in result.errors}

    def test_row_union_rejects_queue_branch_with_unrelated_producer(self) -> None:
        queue = NodeSpec(
            id="control_done",
            node_type="queue",
            plugin=None,
            input="control_done",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._state(extra_nodes=(queue,))
        state = replace(
            state,
            sources={
                "primary": self._source(),
                "contaminant": self._source(on_success="control_done"),
            },
        )

        result = state.validate()

        lineage_error = next(error for error in result.errors if error.error_code == "row_union_branch_not_downstream")
        assert "control_branch" in lineage_error.message
        assert "control_done" in lineage_error.message

    @pytest.mark.parametrize(
        ("control_fields", "treatment_fields", "is_compatible"),
        [
            (["id: str", "score: float"], ["id: str", "score: float"], True),
            (["id: str", "score: float"], ["id: str", "label: str"], False),
        ],
    )
    def test_row_union_requires_compatible_known_fixed_branch_schemas(
        self,
        control_fields: list[str],
        treatment_fields: list[str],
        is_compatible: bool,
    ) -> None:
        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={"schema": {"mode": "fixed", "fields": control_fields}},
            )
            if node.id == "control"
            else replace(
                node,
                options={"schema": {"mode": "fixed", "fields": treatment_fields}},
            )
            if node.id == "treatment"
            else node
            for node in state.nodes
        )

        result = replace(state, nodes=nodes).validate()
        schema_errors = [
            error
            for error in result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_schema_incompatible"
            and "incompatible" in error.message
        ]

        assert bool(schema_errors) is not is_compatible, result.errors
        if not is_compatible:
            detail = schema_errors[0].row_union_schema
            assert detail is not None
            assert detail.conflicting_fields == ("label", "score")
            assert tuple(branch.branch for branch in detail.branches) == (
                "control_branch",
                "treatment_branch",
            )

    def test_row_union_observed_branch_abstains_against_fixed_branch(self) -> None:
        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={"schema": {"mode": "fixed", "fields": ["id: str", "score: float"]}},
            )
            if node.id == "treatment"
            else node
            for node in state.nodes
        )

        result = replace(state, nodes=nodes).validate()

        assert not any(
            error.component == "node:variant_union"
            and error.error_code == "row_union_schema_incompatible"
            and "incompatible" in error.message
            for error in result.errors
        ), result.errors

    def test_row_union_accepts_disjoint_flexible_branch_declarations(self) -> None:
        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["score: float"] if node.id == "control" else ["label: str"],
                    }
                },
            )
            if node.id in {"control", "treatment"}
            else node
            for node in state.nodes
        )

        result = replace(state, nodes=nodes).validate()

        assert not any(error.error_code == "row_union_schema_incompatible" for error in result.errors), result.errors

    def test_row_union_flexible_shared_type_conflict_carries_repair_facts(self) -> None:
        from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
        from elspeth.web.composer.tools import ToolResult

        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["id: str"] if node.id == "control" else ["id: int"],
                    }
                },
            )
            if node.id in {"control", "treatment"}
            else node
            for node in state.nodes
        )
        candidate = replace(state, nodes=nodes)

        result = candidate.validate()

        entry = next(error for error in result.errors if error.error_code == "row_union_schema_incompatible")
        detail = entry.row_union_schema
        assert detail is not None
        assert detail.conflicting_fields == ("id",)
        assert [
            {
                "branch": branch.branch,
                "mode": branch.mode,
                "fields": tuple((field.name, field.field_type) for field in branch.fields),
            }
            for branch in detail.branches
        ] == [
            {
                "branch": "control_branch",
                "mode": "flexible",
                "fields": (("id", "str"),),
            },
            {
                "branch": "treatment_branch",
                "mode": "flexible",
                "fields": (("id", "int"),),
            },
        ]

        tool_result = ToolResult(
            success=False,
            updated_state=candidate,
            validation=result,
            affected_nodes=(),
        )
        projected = next(
            error
            for error in _allowlisted_candidate_feedback(tool_result)["validation"]["errors"]
            if error["error_code"] == "row_union_schema_incompatible"
        )
        assert "message" not in projected
        assert projected["row_union_schema"] == detail.to_dict()
        assert projected["suggested_fix"]

    def _coalesce(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "dup_merge",
            "node_type": "coalesce",
            "plugin": None,
            "input": "join",
            "on_success": "output",
            "on_error": None,
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            # Identity branches: direct gate->barrier COPY edges that claim no
            # ordinary connection consumer, so the duplicate-consumer check
            # cannot mask the barrier-ownership conflict under test.
            "branches": ("control_branch", "treatment_branch"),
            "policy": "require_all",
            "merge": "nested",
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def test_fork_branch_claimed_by_a_coalesce_and_a_row_union_is_rejected(self) -> None:
        """Composer/runtime parity for the engine's one-barrier-per-branch rule.

        The DAG builder raises ``GraphValidationError`` ("Each fork branch can
        only join at one barrier") when a coalesce and a row_union both declare
        the same branch, because the branch's arrival is delivered to exactly
        one barrier's pending map. validate() used to pass this composition, so
        generate_yaml handed the runtime a graph it refuses to build.
        """
        result = self._state(extra_nodes=(self._coalesce(),)).validate()

        codes = {error.error_code for error in result.errors}
        assert "fork_branch_multiple_barriers" in codes, result.errors
        # composer_mcp.server gates generate_yaml on is_valid, so a red
        # validate() is what stops the runtime-invalid YAML being exported.
        assert not result.is_valid
        conflict = next(error for error in result.errors if error.error_code == "fork_branch_multiple_barriers")
        assert "control_branch" in conflict.message
        assert "variant_union" in conflict.message
        assert "dup_merge" in conflict.message
        # A cross-node topology finding, not either barrier's intrinsic
        # node-shape code the mutation preflight blocks on.
        assert "row_union_branch_invalid" not in codes

    def test_fork_branch_claimed_by_two_coalesces_is_rejected(self) -> None:
        """The same engine rule covers coalesce/coalesce claims."""
        result = self._state(
            extra_nodes=(self._coalesce(), self._coalesce(id="second_merge")),
        ).validate()

        assert any(error.error_code == "fork_branch_multiple_barriers" for error in result.errors), result.errors

    def test_fork_branch_claimed_by_two_row_unions_is_rejected(self) -> None:
        """And row_union/row_union claims, which the engine rejects too."""
        second_union = self._row_union(id="second_union", on_success="second_union_out")
        result = self._state(
            extra_nodes=(second_union, self._transform("after_second", "second_union_out", "output")),
        ).validate()

        assert any(error.error_code == "fork_branch_multiple_barriers" for error in result.errors), result.errors

    def test_valid_topology_does_not_report_a_barrier_conflict(self) -> None:
        """A single barrier per branch stays clean — the rule is not a blanket ban."""
        result = self._state().validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "fork_branch_multiple_barriers" for error in result.errors)

    def test_row_union_output_feeds_ordinary_node_without_placeholder_consumer(self) -> None:
        state = self._state()

        result = state.validate()

        assert any(node.id == "after_union" and node.input == "union_out" for node in state.nodes)
        assert result.is_valid, result.errors
        assert not any(error.error_code == "duplicate_connection_consumer" for error in result.errors)

    def test_queue_output_feeds_row_union_branch_without_placeholder_consumer(self) -> None:
        queue = NodeSpec(
            id="control_done",
            node_type="queue",
            plugin=None,
            input="control_done",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._state(extra_nodes=(queue,))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "duplicate_connection_consumer" for error in result.errors)

    def test_identity_row_union_branch_does_not_consume_same_named_queue(self) -> None:
        queue = NodeSpec(
            id="control_branch",
            node_type="queue",
            plugin=None,
            input="control_branch",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        row_union = self._row_union(
            input="control_branch",
            branches={
                "control_branch": "control_branch",
                "treatment_branch": "treatment_branch",
            },
        )
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._gate(),
                queue,
                row_union,
                self._transform("after_union", "union_out", "output"),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert any(error.error_code == "queue_no_consumer" and error.component == "node:control_branch" for error in result.errors)

    def test_row_union_rejects_downstream_aggregation_with_early_trigger(self) -> None:
        aggregation = NodeSpec(
            id="after_union",
            node_type="aggregation",
            plugin="batch_stats",
            input="union_out",
            on_success="output",
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 2},
            output_mode="transform",
        )
        state = self._state()
        state = replace(
            state,
            nodes=tuple(aggregation if node.id == "after_union" else node for node in state.nodes),
        )

        result = state.validate()

        group_error = next(
            error
            for error in result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_downstream_group_invalid"
            and "indivisible" in error.message
        )
        assert "count/timeout/condition trigger" in group_error.message

    @pytest.mark.parametrize("barrier_type", ["coalesce", "row_union"])
    def test_row_union_rejects_downstream_correlated_barrier(self, barrier_type: str) -> None:
        post_union_gate = self._gate(
            id="post_union_fork",
            input="union_out",
            fork_to=("downstream_a", "downstream_b"),
        )
        if barrier_type == "coalesce":
            downstream_barrier = self._coalesce(
                id="downstream_barrier",
                input="downstream_a",
                branches=("downstream_a", "downstream_b"),
            )
            tail: tuple[NodeSpec, ...] = ()
        else:
            downstream_barrier = self._row_union(
                id="downstream_barrier",
                input="downstream_a",
                branches={
                    "downstream_a": "downstream_a",
                    "downstream_b": "downstream_b",
                },
                on_success="downstream_out",
            )
            tail = (self._transform("after_downstream", "downstream_out", "output"),)

        state = self._state()
        state = replace(
            state,
            nodes=(
                *(node for node in state.nodes if node.id != "after_union"),
                post_union_gate,
                downstream_barrier,
                *tail,
            ),
        )

        result = state.validate()

        group_error = next(
            error
            for error in result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_downstream_group_invalid"
            and "correlated barrier" in error.message
        )
        assert barrier_type in group_error.message
        assert "downstream_barrier" in group_error.message

    @pytest.mark.parametrize("branches", [None, (), ("only_branch",), {"only_branch": "control_done"}])
    def test_row_union_requires_at_least_two_branches(self, branches: object) -> None:
        result = self._state(row_union=self._row_union(branches=branches)).validate()

        assert any(error.error_code == "row_union_branches_invalid" for error in result.errors)

    @pytest.mark.parametrize("on_success", [None, "", "   "])
    def test_row_union_requires_non_empty_on_success(self, on_success: object) -> None:
        result = self._state(row_union=self._row_union(on_success=on_success)).validate()

        assert any(error.error_code == "row_union_on_success_invalid" for error in result.errors)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("plugin", "passthrough"),
            ("options", {"schema": {"mode": "observed"}}),
            ("on_error", "discard"),
            ("condition", "True"),
            ("routes", {"true": "union_out"}),
            ("fork_to", ("branch",)),
            ("policy", "require_all"),
            ("merge", "union"),
            ("trigger", {"kind": "count"}),
            ("output_mode", "passthrough"),
            ("expected_output_count", 2),
        ],
    )
    def test_row_union_rejects_fields_owned_by_other_node_kinds(self, field: str, value: object) -> None:
        result = self._state(row_union=self._row_union(**{field: value})).validate()

        assert any(error.error_code == "row_union_config_invalid" and field in error.message for error in result.errors), result.errors

    @pytest.mark.parametrize(
        "timeout_seconds",
        # The two oversized ints are unrepresentable as float; classifying them
        # INVALID must not regress the bool / NaN / inf / non-positive verdicts.
        [True, False, float("nan"), float("inf"), 0.0, -1.0, 10**400, -(10**400)],
    )
    def test_row_union_rejects_invalid_timeout(self, timeout_seconds: object) -> None:
        result = self._state(row_union=self._row_union(timeout_seconds=timeout_seconds)).validate()

        assert any(error.error_code == "row_union_timeout_invalid" for error in result.errors)

    def test_oversized_persisted_timeout_rejects_instead_of_overflowing(self) -> None:
        """An oversized int from a restored session must reject, not crash.

        ``timeout_seconds`` reaches ``NodeSpec.from_dict`` straight from the
        persisted session payload, bypassing the Pydantic
        ``_StrictTimeoutSeconds`` tool boundary. JSON has no integer ceiling,
        so ``10**400`` survives the round trip as an ``int`` that ``float()``
        cannot represent — ``math.isfinite`` used to raise ``OverflowError``
        out of ``validate()`` instead of producing a rejection.
        """
        payload = json.loads(
            json.dumps(
                {
                    "id": "variant_union",
                    "node_type": "row_union",
                    "plugin": None,
                    "input": "control_done",
                    "on_success": "union_out",
                    "on_error": None,
                    "options": {},
                    "branches": {"control_branch": "control_done", "treatment_branch": "treatment_done"},
                    "timeout_seconds": 10**400,
                }
            )
        )
        assert isinstance(payload["timeout_seconds"], int)

        result = self._state(row_union=NodeSpec.from_dict(payload)).validate()

        assert any(error.error_code == "row_union_timeout_invalid" for error in result.errors)

    def test_row_union_input_is_only_first_branch_placeholder(self) -> None:
        result = self._state(row_union=self._row_union(input="treatment_done")).validate()

        assert any(error.error_code == "row_union_input_mismatch" for error in result.errors)

    @pytest.mark.parametrize(
        "branches",
        [
            {"__control": "control_done", "treatment_branch": "treatment_done"},
            {"control_branch": "__control_done", "treatment_branch": "treatment_done"},
        ],
    )
    def test_row_union_branch_aliases_and_connections_obey_connection_name_rules(
        self,
        branches: dict[str, str],
    ) -> None:
        row_union = self._row_union(input=next(iter(branches.values())), branches=branches)
        result = self._state(row_union=row_union).validate()

        assert any(error.error_code == "row_union_branch_invalid" for error in result.errors)

    def test_row_union_requires_each_branch_alias_and_value_to_be_reachable(self) -> None:
        row_union = self._row_union(
            branches={
                "control_branch": "control_done",
                "unforked_branch": "missing_connection",
            }
        )
        result = self._state(row_union=row_union).validate()
        codes = {error.error_code for error in result.errors}

        assert "row_union_branch_alias_unreachable" in codes
        assert "row_union_branch_unreachable" in codes

    def test_row_union_claims_every_branch_value_as_a_consumer(self) -> None:
        competing = self._transform("competing", "treatment_done", "unused")
        state = self._state(
            extra_nodes=(competing,),
        )

        result = state.validate()

        assert any(error.error_code == "duplicate_connection_consumer" for error in result.errors)

    def test_row_union_on_success_must_feed_a_processing_node(self) -> None:
        row_union = self._row_union(on_success="output")
        result = self._state(row_union=row_union).validate()

        assert any(error.error_code == "row_union_on_success_must_be_connection" for error in result.errors)

    def test_row_union_with_participating_branches_propagates_guarantees(self) -> None:
        """elspeth-41bcaa882e: a participating union is checked, not skipped.

        Historically the walk-back abstained at every row_union with a
        medium "Contract check skipped" warning and emitted no EdgeContract,
        which let the composer author union-consumer requirements the engine
        then deterministically rejected at /validate. When every branch
        participates, the contract check now proceeds against the union's
        branch-intersection guarantee.
        """
        state = self._state(
            tail_options={
                "required_input_fields": ["id"],
                "schema": {"mode": "observed"},
            }
        )
        state = replace(
            state,
            sources={
                "source": self._source(
                    schema={
                        "mode": "fixed",
                        "fields": ["id: str"],
                        "guaranteed_fields": ["id"],
                    }
                )
            },
        )

        result = state.validate()

        assert result.is_valid, result.errors
        contract = next(contract for contract in result.edge_contracts if contract.to_id == "after_union")
        assert contract.from_id == "variant_union"
        assert contract.satisfied
        assert not any("row_union" in warning.message and "observed schema" in warning.message for warning in result.warnings)

    def _union_arm_queue(self, connection_name: str) -> NodeSpec:
        """An in-place queue on a branch connection: an arm Composer cannot resolve."""
        return NodeSpec(
            id=connection_name,
            node_type="queue",
            plugin=None,
            input=connection_name,
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _locked_tail_options(self, fields: list[str]) -> dict[str, Any]:
        return {"schema": {"mode": "fixed", "fields": fields}}

    def _llm_arms(self, *, control_field: str = "verdict", treatment_field: str = "verdict") -> tuple[NodeSpec, ...]:
        return (
            self._llm("control", "control_branch", "control_done", response_field=control_field),
            self._llm("treatment", "treatment_branch", "treatment_done", response_field=treatment_field),
        )

    def test_row_union_arm_emits_reach_a_locked_input_consumer(self) -> None:
        """elspeth-9d13900064: Rule A must not fail open across a row_union.

        The presence direction abstains at a row_union (an arm's guarantees
        cannot be promoted to the union's), but the extras direction is the
        opposite polarity: a field guaranteed by an arm WILL arrive on that
        arm's rows, so a fixed-mode consumer forbidding it is a definite
        runtime PluginContractViolation, not a maybe.
        """
        state = self._state(
            arms=self._llm_arms(),
            tail_options=self._locked_tail_options(["verdict: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:after_union"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.consumer == "after_union"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_row_union_extras_reach_a_fixed_mode_field_mapper_consumer(self) -> None:
        """The ticket's literal graph: llm x2 -> row_union -> fixed field_mapper.

        A field_mapper consumer also selects the plugin-specific repair
        wording, which the boundary path must carry like the resolved-producer
        path does.
        """
        field_mapper = NodeSpec(
            id="after_union",
            node_type="transform",
            plugin="field_mapper",
            input="union_out",
            on_success="output",
            on_error="discard",
            options={
                "schema": {"mode": "fixed", "fields": ["verdict: str"]},
                "mapping": {"verdict": "verdict"},
                "select_only": True,
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            source=self._source(),
            nodes=(self._gate(), *self._llm_arms(), self._row_union(), field_mapper),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")
        assert "consumer's schema.fields" in entry.message

    def test_row_union_arm_emits_union_rather_than_intersect(self) -> None:
        """A field guaranteed by ONE arm only is still a definite extra.

        Intersection math (the presence-direction merge) would clear this
        graph: the arms share no guaranteed field. Rows from the treatment arm
        still carry its trio, so the union is the sound set here.
        """
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            tail_options=self._locked_tail_options(["verdict: str", "verdict_usage: any", "verdict_model: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("tone", "tone_model", "tone_usage")

    def test_row_union_locked_input_accepting_every_arm_emit_is_clean(self) -> None:
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            tail_options=self._locked_tail_options(
                [
                    "verdict: str",
                    "verdict_usage: any",
                    "verdict_model: str",
                    "tone: str",
                    "tone_usage: any",
                    "tone_model: str",
                ]
            ),
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_row_union_unresolvable_arm_does_not_invent_locked_input_extras(self) -> None:
        """An arm the EMIT walker cannot resolve contributes no fields, and no error.

        The guarantee-direction vote sees through the in-place queue (fan-in
        intersection, elspeth-3619b8774f), so the union participates and the
        contract check runs — no skip warning. The emit direction stays
        conservative: the queued arm contributes nothing, so no extras are
        invented for it.
        """
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            extra_nodes=(self._union_arm_queue("treatment_done"),),
            tail_options=self._locked_tail_options(["verdict: str", "verdict_usage: any", "verdict_model: str"]),
        )

        result = state.validate()

        assert not any(error.error_code == "locked_input_extras" for error in result.errors), result.errors

    def test_row_union_known_arm_extras_survive_an_unresolvable_sibling(self) -> None:
        """Partial knowledge still errors on what IS known.

        The queued treatment arm is opaque to the EMIT walker, but the
        control arm's guarantees are proven — so its extras are reported.
        """
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            extra_nodes=(self._union_arm_queue("treatment_done"),),
            tail_options=self._locked_tail_options(["verdict: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_row_union_arm_emits_reach_a_locked_sink_through_a_gate(self) -> None:
        """Rule B shares Rule A's walker, so it shares the row_union hole.

        A row_union cannot feed a sink directly (its on_success must be a
        processing connection), so the sink boundary is only reachable through
        an intervening routing gate.
        """
        release_gate = self._gate(
            id="release",
            input="union_out",
            routes={"true": "output", "false": "output"},
            fork_to=None,
        )
        state = CompositionState(
            source=self._source(),
            nodes=(self._gate(), *self._llm_arms(), self._row_union(), release_gate),
            edges=(),
            outputs=(self._output(options={"schema": {"mode": "fixed", "fields": ["verdict: str"]}}),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.component == "output:output"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.consumer == "output:output"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_resolvable_pass_through_node_between_union_and_locked_consumer_still_errors(self) -> None:
        """elspeth-902fc354b2 gap 1: a resolvable intermediate node must not bypass Rule A.

        With a pass-through relay between the row_union and the locked
        consumer, the presence walk RESOLVES (to the relay), so the row_union
        boundary path never runs. The relay declares
        ``passes_through_input=True`` — a runtime-verified ADR-008 contract —
        so every arm emit definitely survives it and must still reach the
        Rule A comparison at the locked consumer.
        """
        relay = self._transform("relay", "union_out", "relay_done")
        locked_tail = self._transform(
            "after_union",
            "relay_done",
            "output",
            options=self._locked_tail_options(["verdict: str"]),
        )
        state = CompositionState(
            source=self._source(),
            nodes=(self._gate(), *self._llm_arms(), self._row_union(), relay, locked_tail),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:after_union"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "relay"
        assert detail.consumer == "after_union"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_row_union_arm_pass_through_carries_source_guarantees_to_locked_consumer(self) -> None:
        """elspeth-902fc354b2 gap 2 through a union: arms pass through source fields.

        g08's live rejection included ``complaint_text`` — an ordinary
        pass-through field, not an llm side-field. An llm arm declares
        ``passes_through_input=True``, so a source-guaranteed field definitely
        arrives on every union row and a locked consumer forbidding it is a
        definite runtime PluginContractViolation.
        """
        state = self._state(
            arms=self._llm_arms(),
            tail_options=self._locked_tail_options(["verdict: str", "verdict_model: str", "verdict_usage: any"]),
        )
        state = replace(
            state,
            sources={
                "source": self._source(
                    schema={
                        "mode": "fixed",
                        "fields": ["complaint_text: str"],
                        "guaranteed_fields": ["complaint_text"],
                    }
                )
            },
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.extra_fields == ("complaint_text",)

    def test_union_consumer_requirement_satisfied_by_every_branch_validates(self) -> None:
        """elspeth-41bcaa882e (battery-2026-08-06 g08): the barrier is transparent.

        Both arms are pass-through, so a source-guaranteed field arrives on
        every released row. A consumer downstream of the union requiring it
        must validate — the walker abstaining at the row_union previously
        reported "guarantees: (none)" and rejected the runnable pipeline,
        mirroring the engine's "(none - dynamic schema)" rejection.
        """
        state = self._state(
            tail_options={
                "schema": {"mode": "observed"},
                "required_input_fields": ["amount"],
            },
        )
        state = replace(
            state,
            sources={"source": self._source(schema={"mode": "observed", "guaranteed_fields": ["id", "amount"]})},
        )

        result = state.validate()

        assert result.is_valid, [e.message for e in result.errors]

    def test_union_consumer_requiring_branch_only_field_still_errors(self) -> None:
        """Fail-closed twin: a field only ONE branch guarantees is not union-guaranteed.

        Control rows never carry the treatment arm's extra field, and every
        released group contains a control row, so the intersection must drop
        it and the consumer requirement must still reject.
        """
        arms = (
            self._transform("control", "control_branch", "control_done"),
            self._transform(
                "treatment",
                "treatment_branch",
                "treatment_done",
                options={"schema": {"mode": "observed", "guaranteed_fields": ["treatment_tag"]}},
            ),
        )
        state = self._state(
            arms=arms,
            tail_options={
                "schema": {"mode": "observed"},
                "required_input_fields": ["treatment_tag"],
            },
        )
        state = replace(
            state,
            sources={"source": self._source(schema={"mode": "observed", "guaranteed_fields": ["id", "amount"]})},
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "schema_contract_violation")
        detail = entry.contract
        assert detail is not None
        assert detail.consumer == "after_union"
        assert detail.missing_fields == ("treatment_tag",)


class TestUnionCoalesceGuaranteeExtras:
    """Rule A/B must see a union coalesce's MERGED guarantees (elspeth-ae83a6b60c).

    The composer half of agreement Shape 19. Stage 1 abstained at EVERY
    coalesce, at three separate sites, so a union coalesce's merged guarantee
    set was invisible to the extras rules while the runtime rejected the
    identical pipeline at build time
    (``validate_typed_producer_guaranteed_extras``). Composer green, ``elspeth
    run`` red — and because Stage 1 emitted no error, the authoring loop had
    nothing to repair against.

    Deliberately the coalesce mirror of ``TestCompositionStateRowUnion``'s
    locked-input tests, down to the llm-arm fixtures: the row_union half of
    the same walk was closed first (elspeth-9d13900064 / elspeth-41bcaa882e),
    so the two classes diverging is itself a signal.

    The merged set is NOT computed here. It comes from the composer's existing
    coalesce accumulation in ``_producer_entry_propagation_vote``, which calls
    the runtime's own ``merge_guaranteed_fields`` — the same function the DAG
    builder stamps a coalesce's guarantees with. Both surfaces therefore read
    one implementation, and the tests below pin the mirror rather than a
    re-derivation of it.
    """

    def _source(self, *, schema: dict[str, Any] | None = None) -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success="fork_in",
            options={"schema": schema or {"mode": "observed"}},
            on_validation_failure="discard",
        )

    def _fork_gate(self) -> NodeSpec:
        return NodeSpec(
            id="fork_rows",
            node_type="gate",
            plugin=None,
            input="fork_in",
            on_success=None,
            on_error=None,
            options={},
            condition="True",
            routes={"true": "fork", "false": "fork"},
            fork_to=("control_branch", "treatment_branch"),
            branches=None,
            policy=None,
            merge=None,
        )

    def _llm(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        response_field: str = "verdict",
    ) -> NodeSpec:
        """An llm arm guaranteeing its provenance trio, as in the row_union class.

        ``llm`` guarantees ``<response_field>`` plus the ``_usage``/``_model``
        side-fields, which are row data rather than audit-only provenance and
        so reach a downstream consumer's input contract. Using the same arm
        plugin as the row_union tests keeps the two guarantee sets comparable.
        """
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="llm",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={
                "schema": {"mode": "observed"},
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "prompt_template": "Judge this row.",
                "api_key": "env:OPENROUTER_API_KEY",
                "response_field": response_field,
                "required_input_fields": [],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _passthrough(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options=options or {"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _coalesce(self, **overrides: Any) -> NodeSpec:
        """A TERMINAL union coalesce: no ``on_success``, consumed by NAME.

        Terminal is not incidental. ``coalesce_on_success_must_be_sink``
        rejects a coalesce routed into a transform, so the runtime-legal shape
        for a coalesce feeding a non-sink consumer is a terminal barrier whose
        id IS the connection the consumer reads (the same shape
        ``test_coalesce_producer_emits_skip_warning`` uses). Wiring
        ``on_success`` at a transform instead would trip that unrelated rule
        and the probe would prove nothing.
        """
        defaults: dict[str, Any] = {
            "id": "variant_merge",
            "node_type": "coalesce",
            "plugin": None,
            # Serialized adapter placeholder: the first branch connection.
            "input": "control_done",
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": {
                "control_branch": "control_done",
                "treatment_branch": "treatment_done",
            },
            "policy": "require_all",
            "merge": "union",
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _output(self, *, options: dict[str, Any] | None = None) -> OutputSpec:
        return OutputSpec(
            name="output",
            plugin="json",
            options=options or {"schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def _state(
        self,
        *,
        coalesce: NodeSpec | None = None,
        arms: tuple[NodeSpec, ...] | None = None,
        tail: NodeSpec | None = None,
        extra_nodes: tuple[NodeSpec, ...] = (),
        output_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        return CompositionState(
            source=self._source(),
            nodes=(
                self._fork_gate(),
                *(
                    arms
                    or (
                        self._llm("control", "control_branch", "control_done"),
                        self._llm("treatment", "treatment_branch", "treatment_done"),
                    )
                ),
                coalesce or self._coalesce(),
                *((tail,) if tail is not None else (self._passthrough("after_merge", "variant_merge", "output"),)),
                *extra_nodes,
            ),
            edges=(),
            outputs=(self._output(options=output_options),),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _locked(self, fields: list[str]) -> dict[str, Any]:
        return {"schema": {"mode": "fixed", "fields": fields}}

    def test_terminal_union_coalesce_into_locked_transform_reports_locked_input_extras(self) -> None:
        """Rule A through the walk-back escape and the emit profile (sites 1 + 2).

        Both llm arms guarantee the same trio, so the require_all union merge
        is that trio, and the locked consumer admits only the response field.
        The two extras are the ones the runtime names in its own
        ``EdgeContractError`` on the equivalent graph.

        Reverting the walk-back escape alone restores the unconditional
        coalesce abstention and this goes green; reverting the emit-profile
        branch alone ALSO goes green, because the walk-back then resolves the
        coalesce but ``_effective_producer_vote`` answers with the coalesce
        node's own (empty) declared set and Rule A finds no extras. Neither
        site is sufficient by itself.
        """
        state = self._state(
            tail=self._passthrough(
                "after_merge",
                "variant_merge",
                "output",
                options=self._locked(["verdict: str"]),
            )
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:after_merge"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_merge"
        assert detail.consumer == "after_merge"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")
        # The abstention advisory is the pre-fix signal; a resolved coalesce
        # must stop emitting it, or the authoring loop is told the edge was
        # deferred while an error names it.
        assert not [
            warning for warning in result.warnings if "Contract check skipped" in warning.message and "coalesce" in warning.message
        ], result.warnings

    def test_definite_emits_traverse_union_coalesce_behind_pass_through(self) -> None:
        """Rule B through ``_connection_definite_emits`` (site 3) in isolation.

        The locked SINK's direct producer is the pass-through relay, so the
        walk-back and the emit profile never see the coalesce at all — only
        the definite-arrivals walk crosses it, because the relay declares
        ``passes_through_input=True`` with an extras-allowing contract and so
        propagates upstream arrivals. Reverting site 3 alone leaves the relay
        contributing its own emits only, and this goes green.

        The relay must declare its OWN ``guaranteed_fields``, which is what
        isolates site 3. Without them ``_producer_emit_profile`` has no
        computed set to prefer and falls back to the relay's inherited vote —
        which already resolves the coalesce through the propagation vote's
        long-standing coalesce branch — so the test would pass pre-fix and pin
        nothing. ``flexible`` keeps the relay extras-allowing, so it
        propagates rather than firewalling arrivals off.
        """
        state = self._state(
            tail=self._passthrough(
                "pt_mid",
                "variant_merge",
                "output",
                options={"schema": {"mode": "flexible", "fields": ["verdict: str"], "guaranteed_fields": ["verdict"]}},
            ),
            output_options=self._locked(["verdict: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.component == "output:output"
        detail = entry.contract
        assert detail is not None
        assert detail.consumer == "output:output"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_best_effort_union_coalesce_intersects_branch_guarantees(self) -> None:
        """The require_all discriminator, both halves, on ONE pipeline shape.

        ``merge_guaranteed_fields`` unions branch guarantees under
        ``require_all`` (every branch always arrives, so any branch's
        guarantee survives) and INTERSECTS otherwise (a branch may be lost).
        The arms here guarantee disjoint trios, so the intersection is empty:
        under ``best_effort`` there is no field the coalesce can promise, and
        reporting extras would be a FALSE RED against a runtime that also
        intersects.

        Mutating the composer branch to always union fails the best_effort
        half; to always intersect fails the require_all half.
        """
        arms = (
            self._llm("control", "control_branch", "control_done", response_field="verdict"),
            self._llm("treatment", "treatment_branch", "treatment_done", response_field="tone"),
        )
        tail = self._passthrough(
            "after_merge",
            "variant_merge",
            "output",
            options=self._locked(["verdict: str", "verdict_model: str", "verdict_usage: any"]),
        )

        require_all = self._state(arms=arms, coalesce=self._coalesce(policy="require_all"), tail=tail).validate()

        entry = next(error for error in require_all.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("tone", "tone_model", "tone_usage")

        best_effort = self._state(arms=arms, coalesce=self._coalesce(policy="best_effort"), tail=tail).validate()

        assert not [error for error in best_effort.errors if error.error_code == "locked_input_extras"], best_effort.errors

    @pytest.mark.parametrize("merge", ["nested", "select"])
    def test_non_union_coalesce_keeps_the_skip_warning(self, merge: str) -> None:
        """The ``merge == "union"`` scope gate, in the shape that would trip it.

        Only ``union`` merges branch guarantees into top-level fields, so only
        union is the population where a coalesce's guarantees can exceed its
        declared ones. ``nested`` keys the merged schema BY BRANCH NAME and
        ``select`` forwards one branch's raw schema; the propagation vote
        mirrors neither, so extending these rules to them would invent
        guarantees and false-red. Deleting the gate makes this pipeline gain
        the union's top-level trio and report extras the runtime does not.

        The abstention plus its advisory is the correct answer here — the
        runtime validator remains authoritative for these merges.
        """
        overrides: dict[str, Any] = {"merge": merge}
        if merge == "select":
            overrides["options"] = {"select_branch": "control_branch"}
        state = self._state(
            coalesce=self._coalesce(**overrides),
            tail=self._passthrough(
                "after_merge",
                "variant_merge",
                "output",
                options=self._locked(["verdict: str"]),
            ),
        )

        result = state.validate()

        assert not [error for error in result.errors if error.error_code == "locked_input_extras"], result.errors
        assert [warning for warning in result.warnings if "Contract check skipped" in warning.message and "coalesce" in warning.message], (
            result.warnings
        )

    def test_unparseable_branch_options_abstain_instead_of_crashing_validate(self) -> None:
        """A branch parsed for the FIRST time here is Tier-3 input, not our bug.

        The Rule A/B call sites deliberately let a ValueError from the emit
        profile crash, because THEIR producer was already parsed earlier in the
        same iteration — a fault there would be a non-determinism bug in our own
        code and masking it would hide it. A coalesce BRANCH is the other case:
        the seam walks branch nodes the consumer's own iteration never touched,
        so a malformed schema block there is ordinary recoverable external input
        and must abstain, exactly as ``_arm_emit_profile`` does.

        The node ORDER is load-bearing and this test is worthless without it.
        ``treatment`` is placed AFTER the locked consumer, so the walk reaches
        its malformed options before that node's own iteration has parsed them.
        With the branch first, the ValueError never fires inside the seam and
        the test would pass vacuously — which is why the skip warning is
        asserted positively rather than merely asserting no crash: the warning
        only appears when the seam actually abstained.

        The malformed value must break the PRODUCER-side parse the seam walks:
        a non-mapping ``schema`` block, which ``parse_raw_schema_config``
        rejects with "schema config must be a mapping". A malformed
        ``required_input_fields`` does NOT work here — that is a consumer-side
        parse the guarantee vote never reads, so the seam resolves normally and
        the extras error still fires. The branch's own node iteration still
        reports the malformed block against the right owner
        (``contract_config_invalid``), which is why abstaining here loses no
        diagnosis.
        """
        malformed_branch = self._passthrough(
            "treatment",
            "treatment_branch",
            "treatment_done",
            options={"schema": "observed"},
        )
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._fork_gate(),
                self._llm("control", "control_branch", "control_done"),
                self._coalesce(),
                self._passthrough("after_merge", "variant_merge", "output", options=self._locked(["verdict: str"])),
                # AFTER the consumer: see the docstring's order note.
                malformed_branch,
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not [error for error in result.errors if error.error_code == "locked_input_extras"], result.errors
        assert [warning for warning in result.warnings if "Contract check skipped" in warning.message and "coalesce" in warning.message], (
            result.warnings
        )

    @pytest.mark.parametrize("barrier_type", ["coalesce", "row_union"])
    def test_draft_cycle_through_a_fan_in_barrier_returns_a_verdict(self, barrier_type: str) -> None:
        """A cyclic draft must produce a verdict, never ``RecursionError``.

        Drafts are not DAG-checked at Stage 1, so a half-wired composition can
        route a barrier's own output back into one of its branches. The
        guarantee vote's coalesce and row_union arms recursed on branch
        connections with no visited-node guard — only queues had one — and
        resolving a union coalesce at the walk-back widened the trigger
        surface: every locked consumer behind a coalesce now votes. Unbounded
        recursion here is a /validate 500, not a rejection.

        Both barrier kinds are covered because the guard is one shared
        ``visited_fan_in_ids`` set: the coalesce case is the one this fix made
        reachable, the row_union case was already reachable through
        pass-through inheritance, and a guard that covered only the newly
        reachable half would leave its sibling recursing.

        ``spin`` must be a pass-through: only a pass-through transform's vote
        walks to its own input, so only that closes the cycle through the
        guarantee channel. Reverting the guard raises ``RecursionError`` from
        BOTH parameters — the check that they reach the vote at all rather than
        being turned back by an earlier pass.
        """
        arms = (
            self._llm("control", "control_branch", "control_done"),
            self._passthrough("treatment", "treatment_branch", "treatment_mid"),
        )
        # ``spin`` reads the barrier's output and republishes it as the branch
        # connection the barrier itself consumes — the cycle.
        spin = self._passthrough("spin", "variant_merge", "treatment_done", options=self._locked(["verdict: str"]))
        if barrier_type == "coalesce":
            state = self._state(arms=arms, tail=spin)
        else:
            state = self._state(
                arms=arms,
                coalesce=NodeSpec(
                    id="variant_merge",
                    node_type="row_union",
                    plugin=None,
                    input="control_done",
                    on_success="union_out",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches={
                        "control_branch": "control_done",
                        "treatment_branch": "treatment_done",
                    },
                    policy=None,
                    merge=None,
                ),
                tail=self._passthrough("spin", "union_out", "treatment_done", options=self._locked(["verdict: str"])),
            )

        result = state.validate()

        assert result is not None
        assert isinstance(result.is_valid, bool)


class TestPassThroughArrivalExtras:
    """Rule A/B must compare DEFINITE ARRIVALS, not the nearest producer's own emits.

    elspeth-902fc354b2 (battery round 3, g08): the runtime predicate these
    rules mirror — the consumer's generated input model with
    ``extra='forbid'`` — validates the ENTIRE arriving row, which includes
    every field passed through ``passes_through_input=True`` transforms
    (a runtime-verified ADR-008 declaration) from arbitrarily far upstream.
    A producer's own predicted emit set is therefore only a fragment of what
    arrives; the walker must union in upstream definite arrivals wherever the
    pass-through declaration proves they survive.
    """

    def _source(self) -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success="rows",
            options={
                "schema": {
                    "mode": "fixed",
                    "fields": ["complaint_id: str", "complaint_text: str"],
                    "guaranteed_fields": ["complaint_id", "complaint_text"],
                }
            },
            on_validation_failure="discard",
        )

    def _llm(self, *, on_success: str = "summarized", schema: dict[str, Any] | None = None) -> NodeSpec:
        return NodeSpec(
            id="summarize",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success=on_success,
            on_error="discard",
            options={
                "schema": schema or {"mode": "observed"},
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "prompt_template": "Summarize this row.",
                "api_key": "env:OPENROUTER_API_KEY",
                "response_field": "one_sentence_summary",
                "required_input_fields": [],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _passthrough(self, node_id: str, input_connection: str, on_success: str, *, options: dict[str, Any] | None = None) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options=options or {"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _output(self, *, options: dict[str, Any] | None = None) -> OutputSpec:
        return OutputSpec(
            name="output",
            plugin="json",
            options=options or {"schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def test_pass_through_source_fields_reach_a_locked_consumer(self) -> None:
        """The g08 linear shape: source fields survive the llm and hit the locked tail."""
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._llm(),
                self._passthrough(
                    "final_cleanup",
                    "summarized",
                    "output",
                    options={"schema": {"mode": "fixed", "fields": ["one_sentence_summary: str"]}},
                ),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:final_cleanup"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "summarize"
        assert detail.extra_fields == (
            "complaint_id",
            "complaint_text",
            "one_sentence_summary_model",
            "one_sentence_summary_usage",
        )

    def test_pass_through_source_fields_reach_a_locked_sink(self) -> None:
        """Rule B shares the arrival math: pass-through fields hit a locked sink too."""
        state = CompositionState(
            source=self._source(),
            nodes=(self._llm(on_success="output"),),
            edges=(),
            outputs=(self._output(options={"schema": {"mode": "fixed", "fields": ["one_sentence_summary: str"]}}),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.component == "output:output"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "summarize"
        assert detail.extra_fields == (
            "complaint_id",
            "complaint_text",
            "one_sentence_summary_model",
            "one_sentence_summary_usage",
        )

    def test_non_pass_through_intermediate_stops_upstream_arrivals(self) -> None:
        """A select_only field_mapper is NOT pass-through: arrivals stop at its emit set.

        ``passes_through_input=False`` means pass-through of any given
        upstream field is not definite, so contributing only the mapper's own
        computed emit set is the correct lower bound — no invented extras at
        the tail.
        """
        mapper = NodeSpec(
            id="select",
            node_type="transform",
            plugin="field_mapper",
            input="summarized",
            on_success="selected",
            on_error="discard",
            options={
                "schema": {"mode": "flexible", "fields": ["summary_out: str"]},
                "mapping": {"one_sentence_summary": "summary_out"},
                "select_only": True,
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._llm(),
                mapper,
                self._passthrough(
                    "final_cleanup",
                    "selected",
                    "output",
                    options={"schema": {"mode": "fixed", "fields": ["summary_out: str"]}},
                ),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not any(error.error_code == "locked_input_extras" and error.component == "node:final_cleanup" for error in result.errors), (
            result.errors
        )

    def test_fixed_output_pass_through_transform_stops_upstream_arrivals(self) -> None:
        """A ``mode: fixed`` output contract is an extras firewall at that transform.

        Runtime enforces the relay's own fixed output with extra='forbid':
        rows either match the declared set exactly or the run fails AT THE
        RELAY — extras can never travel past it. The defect is reported at
        the relay's own locked input, not invented at the downstream tail.
        """
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._passthrough(
                    "relay",
                    "rows",
                    "relayed",
                    options={"schema": {"mode": "fixed", "fields": ["complaint_id: str"]}},
                ),
                self._passthrough(
                    "final_cleanup",
                    "relayed",
                    "output",
                    options={"schema": {"mode": "fixed", "fields": ["complaint_id: str"]}},
                ),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not any(error.error_code == "locked_input_extras" and error.component == "node:final_cleanup" for error in result.errors), (
            result.errors
        )
        relay_entry = next(
            error for error in result.errors if error.error_code == "locked_input_extras" and error.component == "node:relay"
        )
        detail = relay_entry.contract
        assert detail is not None
        assert detail.extra_fields == ("complaint_text",)

    def _rename_mapper(self, input_connection: str, on_success: str) -> NodeSpec:
        """A reductive fixed-output mapper: renames complaint_text -> body.

        The declared fixed schema lists the ARRIVING fields (with guarantees,
        so the plugin can compute); the mapping drops ``complaint_text`` by
        renaming it. The plugin computes its own emit set (``complaint_id``,
        ``body``) — the declared-required union must never override that
        computation.
        """
        return NodeSpec(
            id="rename",
            node_type="transform",
            plugin="field_mapper",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={
                "schema": {
                    "mode": "fixed",
                    "fields": ["complaint_id: str", "complaint_text: str"],
                    "guaranteed_fields": ["complaint_id", "complaint_text"],
                },
                "mapping": {"complaint_text": "body"},
                "select_only": False,
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def test_reductive_fixed_mapper_does_not_predict_dropped_fields_at_locked_sink(self) -> None:
        """A fixed-mode reductive producer's PLUGIN-COMPUTED emit set is authoritative.

        Regression for the mode:fixed pin introduced with elspeth-902fc354b2:
        pinning to ``get_effective_guaranteed_fields()`` unions the declared
        required fields — for a reductive transform, exactly the fields it
        drops — into the Rule B prediction, rejecting a pipeline the runtime
        executes clean (field_mapper deletes the renamed source key).
        """
        state = CompositionState(
            source=self._source(),
            nodes=(self._rename_mapper("rows", "output"),),
            edges=(),
            outputs=(self._output(options={"schema": {"mode": "fixed", "fields": ["complaint_id: str", "body: str"]}}),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not any(error.error_code == "sink_locked_extras" for error in result.errors), result.errors

    def test_reductive_fixed_mapper_does_not_predict_dropped_fields_at_locked_consumer(self) -> None:
        """Rule A shares the emit-profile math: no invented extras at a locked node input."""
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._rename_mapper("rows", "renamed"),
                self._passthrough(
                    "final_cleanup",
                    "renamed",
                    "output",
                    options={"schema": {"mode": "fixed", "fields": ["complaint_id: str", "body: str"]}},
                ),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not any(error.error_code == "locked_input_extras" and error.component == "node:final_cleanup" for error in result.errors), (
            result.errors
        )

    def _fixed_llm_schema(self) -> dict[str, Any]:
        """The llm's own arriving fields, declared fixed — an ADDITIVE firewall.

        Mirror image of ``_rename_mapper``: the same ``mode: fixed`` contract,
        but on a producer that declares ``passes_through_input=True``, so the
        declared fields are ones it FORWARDS rather than ones it drops.
        """
        return {"mode": "fixed", "fields": ["complaint_id: str", "complaint_text: str"]}

    def _locked_summary_trio(self) -> dict[str, Any]:
        return {
            "schema": {
                "mode": "fixed",
                "fields": [
                    "one_sentence_summary: str",
                    "one_sentence_summary_model: str",
                    "one_sentence_summary_usage: any",
                ],
            }
        }

    def test_additive_fixed_pass_through_predicts_forwarded_fields_at_a_locked_sink(self) -> None:
        """An ADDITIVE fixed-output producer still emits the fields it forwards.

        elspeth-9a8367078f, the mirror of the reductive pair above. ``llm``
        declares ``passes_through_input=True`` — runtime-enforced by the
        executor's pass-through cross-check (ADR-008), which fails the run if
        an input field is dropped — so ``complaint_id``/``complaint_text``
        reach every emitted row. Its computed ``guaranteed_fields`` names only
        the summary trio it ADDS, so predicting that alone let this pipeline
        compose valid and die on row 1 against the locked sink's
        ``extra='forbid'`` model: the compose-valid/run-dies direction.
        """
        state = CompositionState(
            source=self._source(),
            nodes=(self._llm(on_success="output", schema=self._fixed_llm_schema()),),
            edges=(),
            outputs=(self._output(options=self._locked_summary_trio()),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.component == "output:output"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "summarize"
        assert detail.extra_fields == ("complaint_id", "complaint_text")

    def test_additive_fixed_pass_through_predicts_forwarded_fields_at_a_locked_consumer(self) -> None:
        """Rule A shares the emit-profile math, so it shares the additive blind spot."""
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._llm(schema=self._fixed_llm_schema()),
                self._passthrough("final_cleanup", "summarized", "output", options=self._locked_summary_trio()),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(
            error for error in result.errors if error.error_code == "locked_input_extras" and error.component == "node:final_cleanup"
        )
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "summarize"
        assert detail.extra_fields == ("complaint_id", "complaint_text")


class TestExtrasFirewallDirection:
    """The composer rejects the shape the DAG's un-gated walk accepts — via Rule A, not the twin.

    ``walk_effective_guarantee_vote`` unions guarantees through a
    ``passes_through_input`` node without consulting its extras firewall, so a
    ``mode: fixed`` llm still carries ``a`` downstream and
    ``validate_transform_declared_input_fields`` accepts a web_scrape that needs
    it (pinned in
    ``tests/unit/core/dag/test_transform_declared_input_fields.py::TestExtrasFirewallDirection``).

    The composer's own declared-input block shares that un-gated union and
    likewise raises nothing here. What rejects is Rule A at the llm's locked
    input, whose emit profile stops propagation at a non-extras-allowing
    contract. Both assertions below are load-bearing: the positive one pins that
    the divergence stays DAG-accept/composer-reject, and the negative one pins
    WHICH check owns the rejection, so gating the declared-input block registers
    here as a change rather than passing silently (elspeth-9c5ff8fa7d).
    """

    def _state(self) -> CompositionState:
        """source {a,url} -> llm (locked to [url], pass-through) -> web_scrape needing 'a'."""
        source = SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"schema": {"mode": "fixed", "fields": ["a: str", "url: str"], "guaranteed_fields": ["a", "url"]}},
            on_validation_failure="discard",
        )
        llm = NodeSpec(
            id="summarize",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="scraped",
            on_error="discard",
            options={
                "schema": {"mode": "fixed", "fields": ["url: str"]},
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "prompt_template": "Summarize this row.",
                "api_key": "env:OPENROUTER_API_KEY",
                "response_field": "summary",
                "required_input_fields": [],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        scraper = NodeSpec(
            id="scraper",
            node_type="transform",
            plugin="web_scrape",
            input="scraped",
            on_success="output",
            on_error="discard",
            options={
                "url_field": "a",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "http": {
                    "abuse_contact": "ops@dta.gov.au",
                    "scraping_reason": "contract validation test",
                    "allowed_hosts": ["127.0.0.0/8"],
                },
                "schema": {"mode": "observed"},
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        return CompositionState(
            source=source,
            nodes=(llm, scraper),
            edges=(),
            outputs=(
                OutputSpec(
                    name="output",
                    plugin="json",
                    options={"schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_rule_a_rejects_at_the_firewall_node(self) -> None:
        """The safe-direction pin: the row carrying 'a' dies at the llm, and composing says so."""
        result = self._state().validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:summarize"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "source"
        assert detail.extra_fields == ("a",)

    def test_declared_input_block_does_not_report_the_downstream_consumer(self) -> None:
        """Discriminator: the composer's declared-input twin shares the walk's un-gated union."""
        result = self._state().validate()

        assert [error.component for error in result.errors if error.error_code == "schema_contract_violation"] == []


class TestForwardingTransformExtrasReachTheLockedSink:
    """Composer half of elspeth-15c72686f2 — the surface the defect was reported on.

    The battery observed this through ``/api/sessions/{id}/validate`` and the
    persisted composition state, BOTH reporting ``is_valid: true`` with zero
    errors, so a DAG-only fix would have left the reporting surface green. The
    truncation was ``_producer_emit_profile`` answering
    ``propagates_upstream=False`` for a transform that forwards the whole row
    minus the column it consumed — it can never declare ``passes_through_input``,
    so the walk stopped and the upstream llm's ``_usage`` / ``_model`` never
    reached Rule B.
    """

    _LLM_SOURCE_OPTIONS: ClassVar[dict[str, Any]] = {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "test-api-key",
        "prompt_template": "Write an announcement.",
        "response_field": "announcement",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }

    def _explode(self, input_connection: str) -> NodeSpec:
        return NodeSpec(
            id="exploded",
            node_type="transform",
            plugin="line_explode",
            input=input_connection,
            on_success="sentence_rows",
            on_error="discard",
            options={
                "source_field": "announcement",
                "output_field": "sentence",
                "include_index": False,
                "schema": {"mode": "observed"},
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _locked_sink(self, fields: list[str]) -> OutputSpec:
        return OutputSpec(
            name="sentence_rows",
            plugin="text",
            options={
                "path": "outputs/announcement_sentences.txt",
                "field": "sentence",
                "schema": {"mode": "fixed", "fields": fields},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
            on_write_failure="discard",
        )

    def _llm_source_state(self, sink: OutputSpec) -> CompositionState:
        return CompositionState(
            source=SourceSpec(
                plugin="llm",
                options=dict(self._LLM_SOURCE_OPTIONS),
                on_success="brief",
                on_validation_failure="discard",
            ),
            nodes=(self._explode("brief"),),
            edges=(),
            outputs=(sink,),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_llm_source_metadata_reaches_the_locked_sink_through_line_explode(self) -> None:
        """The literal g11-s2 graph, on the gate that reported it clean."""
        result = self._llm_source_state(self._locked_sink(["sentence: str"])).validate()

        assert not result.is_valid
        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "exploded"
        assert detail.extra_fields == ("announcement_model", "announcement_usage")

    def test_consumed_source_field_is_not_named_as_an_extra(self) -> None:
        """``announcement`` is what line_explode CONSUMES, so it never arrives.

        Naming it would send the authoring loop after a field the transform
        already removed — the removal set exists to prevent exactly that.
        """
        result = self._llm_source_state(self._locked_sink(["sentence: str"])).validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.contract is not None
        assert "announcement" not in entry.contract.extra_fields

    def test_llm_transform_upstream_reaches_the_same_dead_end(self) -> None:
        """Shape B: the defect never needed ``source:llm`` to be authorable.

        The ticket argued no earlier battery round could have found this because
        ``source:llm`` was outside the plugin allowlist. An llm TRANSFORM in
        front of the same exploder — the far commoner authored shape — was
        always reachable and equally green.
        """
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                options={
                    "path": "data/in.csv",
                    "schema": {"mode": "fixed", "fields": ["topic: str"]},
                    "on_validation_failure": "discard",
                },
                on_success="rows",
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="writer",
                    node_type="transform",
                    plugin="llm",
                    input="rows",
                    on_success="written",
                    on_error="discard",
                    options={
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": "test-api-key",
                        "prompt_template": "Write an announcement about {{ row.topic }}.",
                        "response_field": "announcement",
                        "required_input_fields": ["topic"],
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                self._explode("written"),
            ),
            edges=(),
            outputs=(self._locked_sink(["sentence: str"]),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.contract is not None
        # The csv column rides through BOTH hops, so the walk composes across
        # the pass-through llm and the forwarding exploder rather than stopping
        # at the nearest producer.
        assert entry.contract.extra_fields == ("announcement_model", "announcement_usage", "topic")

    def test_sink_declaring_the_metadata_optional_is_clean(self) -> None:
        """Rejection is on ARRIVAL, not on having an llm upstream.

        The sink stays locked (``mode: fixed``); declaring the metadata
        OPTIONAL admits it without requiring line_explode to guarantee it. Two
        of the three g11 samples authored a tolerant sink and ran correctly —
        those must keep validating.
        """
        result = self._llm_source_state(
            self._locked_sink(["sentence: str", "announcement_usage: any?", "announcement_model: str?"])
        ).validate()

        assert [error.error_code for error in result.errors if error.error_code == "sink_locked_extras"] == []

    def test_locked_transform_input_downstream_of_the_forwarder_trips_rule_a(self) -> None:
        """Rule A (node-level) shares the edited union site with Rule B and must fire too.

        The sink tests above exercise Rule B; this pins the locked TRANSFORM
        consumer — a fixed-schema value_transform fed by the exploder — so the
        node-level arm of the forwarding propagation cannot silently regress
        while the sink arm stays green.
        """
        state = CompositionState(
            source=SourceSpec(
                plugin="llm",
                options=dict(self._LLM_SOURCE_OPTIONS),
                on_success="brief",
                on_validation_failure="discard",
            ),
            nodes=(
                self._explode("brief"),
                NodeSpec(
                    id="shout",
                    node_type="transform",
                    plugin="value_transform",
                    input="sentence_rows",
                    on_success="out_conn",
                    on_error="discard",
                    options={
                        "operations": [{"field": "sentence", "operation": "uppercase"}],
                        "schema": {"mode": "fixed", "fields": ["sentence: str"]},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="out_conn",
                    plugin="json",
                    options={"path": "outputs/out.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:shout"
        assert entry.contract is not None
        assert entry.contract.extra_fields == ("announcement_model", "announcement_usage")


class TestStepDescriptions:
    """Contract for the optional composer-authored per-step ``description``.

    Three invariants (elspeth-051eadb901):
      * every spec kind round-trips the field through to_dict/from_dict;
      * a dict persisted BEFORE the field existed deserialises to None; and
      * to_dict omits the key when None, so pre-existing serialised states —
        and therefore their composition_content_hash values — are unchanged.
    """

    def _node(self, description: str | None = None) -> NodeSpec:
        return NodeSpec(
            id="summarize",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="summarized",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            description=description,
        )

    def test_source_round_trips_description(self) -> None:
        source = SourceSpec(
            plugin="csv",
            on_success="rows",
            options={},
            on_validation_failure="discard",
            description="Read the three project-brief pages.",
        )
        d = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {},
            "on_validation_failure": "discard",
            "description": "Read the three project-brief pages.",
        }
        assert SourceSpec.from_dict(d) == source

    def test_node_round_trips_description(self) -> None:
        node = self._node("Have an LLM write a short summary of each page.")
        restored = NodeSpec.from_dict(
            {
                "id": "summarize",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "summarized",
                "on_error": "discard",
                "options": {},
                "description": "Have an LLM write a short summary of each page.",
            }
        )
        assert restored == node

    def test_output_round_trips_description(self) -> None:
        output = OutputSpec(
            name="results",
            plugin="json",
            options={},
            on_write_failure="discard",
            description="Write url and summary to a JSON file.",
        )
        d = {
            "name": "results",
            "plugin": "json",
            "options": {},
            "on_write_failure": "discard",
            "description": "Write url and summary to a JSON file.",
        }
        assert OutputSpec.from_dict(d) == output

    def test_legacy_dicts_without_the_key_deserialise_to_none(self) -> None:
        source = SourceSpec.from_dict({"plugin": "csv", "on_success": "rows", "options": {}, "on_validation_failure": "discard"})
        node = NodeSpec.from_dict(
            {
                "id": "summarize",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "summarized",
                "on_error": "discard",
                "options": {},
            }
        )
        output = OutputSpec.from_dict({"name": "results", "plugin": "json", "options": {}, "on_write_failure": "discard"})
        assert source.description is None
        assert node.description is None
        assert output.description is None

    def test_state_to_dict_omits_the_key_when_none_and_carries_it_when_set(self) -> None:
        undescribed = CompositionState(
            sources={"source": SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard")},
            nodes=(self._node(None),),
            edges=(),
            outputs=(OutputSpec(name="summarized", plugin="json", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )
        d = undescribed.to_dict()
        assert "description" not in d["sources"]["source"]
        assert "description" not in d["nodes"][0]
        assert "description" not in d["outputs"][0]

        described = CompositionState(
            sources={
                "source": SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    options={},
                    on_validation_failure="discard",
                    description="Read the pages.",
                )
            },
            nodes=(self._node("Summarise each page."),),
            edges=(),
            outputs=(
                OutputSpec(
                    name="summarized",
                    plugin="json",
                    options={},
                    on_write_failure="discard",
                    description="Write the results.",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        restored = CompositionState.from_dict(described.to_dict())
        assert restored.sources["source"].description == "Read the pages."
        assert restored.nodes[0].description == "Summarise each page."
        assert restored.outputs[0].description == "Write the results."
