"""Tests for CompositionState and supporting data models."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, ClassVar

import pytest

from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationEntry,
    ValidationSummary,
    _closer_backward_reach_connections,
    _fork_branch_reaches_sink_before_closer,
    _runtime_nodes_downstream_of_connection,
    queue_node_contract_error,
)


@pytest.mark.parametrize("auth", [{}, {"credential": "literal"}, {"credential": None}, {"credential": {"secret_ref": "WEB_TOKEN"}}])
def test_web_scrape_auth_is_unavailable_during_authoring(auth: dict[str, object]) -> None:
    state = CompositionState(
        sources={"default": SourceSpec(plugin="csv", on_success="scrape", options={}, on_validation_failure="discard")},
        nodes=(
            NodeSpec.from_dict(
                {
                    "id": "scrape",
                    "node_type": "transform",
                    "plugin": "web_scrape",
                    "input": "scrape",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {"auth": auth},
                }
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    errors = [error for error in state.validate().errors if error.error_code == "web_scrape_auth_unavailable"]
    assert len(errors) == 1
    assert errors[0].component == "node:scrape"
    assert errors[0].severity == "high"
    assert "response evidence" in errors[0].message
    assert "literal" not in errors[0].message


@pytest.mark.parametrize(("plugin", "options"), [("web_scrape", {}), ("web_scrape", {"auth": None}), ("passthrough", {"auth": {}})])
def test_auth_refusal_does_not_reject_unauthenticated_or_other_plugins(plugin: str, options: dict[str, object]) -> None:
    state = CompositionState(
        sources={"default": SourceSpec(plugin="csv", on_success="scrape", options={}, on_validation_failure="discard")},
        nodes=(
            NodeSpec.from_dict(
                {
                    "id": "scrape",
                    "node_type": "transform",
                    "plugin": plugin,
                    "input": "scrape",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": options,
                }
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    assert not any(error.error_code == "web_scrape_auth_unavailable" for error in state.validate().errors)


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


class TestImplicitPublisherWalksReachThroughAggregations:
    """The three remaining graph walks derive the success channel, not restate it.

    All three stopped dead at an aggregation that omits ``on_success``. That
    aggregation publishes under its own id (``core/dag/builder.py`` registers
    ``agg_settings.name``), so "publishes nothing" was wrong and everything
    beyond it was invisible to the walk.

    These are not cosmetic conversions — the walks feed real checks:
    ``_runtime_nodes_downstream_of_connection`` backs
    ``row_union_downstream_group_invalid``, and the other two back bound-region
    fork-branch anchoring.

    Tested at the function level deliberately. Each blind spot is a property of
    one walk, and building a fully-valid pipeline that isolates one walk's
    contribution to a caller's verdict takes a fixture so elaborate that it
    pins the fixture rather than the walk — a first attempt here produced a
    graph whose error came from a different arm entirely.

    Each case carries a QUEUE control, because deriving the channel means these
    walks now see a queue's own id where they did not before. That is inert:
    every one of them is entered only when the node's input already intersects
    the reachable set, and a queue's sole input IS its own id
    (``queue_node_contract_error`` enforces it), so re-adding it is a no-op.
    The controls hold that reasoning to account.
    """

    @staticmethod
    def _node(**overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "x",
            "node_type": "transform",
            "plugin": None,
            "input": "in",
            "on_success": None,
            "on_error": None,
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

    def _transform(self, node_id: str, input_connection: str, on_success: str) -> NodeSpec:
        return self._node(id=node_id, plugin="passthrough", input=input_connection, on_success=on_success)

    def _implicit_aggregation(self, node_id: str, input_connection: str) -> NodeSpec:
        """An aggregation that omits on_success — it publishes under its own id."""
        return self._node(
            id=node_id,
            node_type="aggregation",
            plugin="batch_stats",
            input=input_connection,
            on_success=None,
            on_error="discard",
            trigger={"count": 2},
        )

    def test_downstream_walk_continues_past_an_implicit_aggregation(self) -> None:
        """Site A. Backs ``row_union_downstream_group_invalid``."""
        nodes = (
            self._implicit_aggregation("agg", "start"),
            self._transform("after", "agg", "done"),
        )
        reached = [node.id for node in _runtime_nodes_downstream_of_connection("start", nodes)]
        assert reached == ["agg", "after"], (
            f"The walk stopped at the aggregation and never saw 'after'. Got {reached}. "
            "An aggregation omitting on_success publishes under its own id."
        )

    def test_downstream_walk_is_unchanged_for_queues(self) -> None:
        nodes = (
            self._node(id="q", node_type="queue", input="q"),
            self._transform("feed", "rows", "q"),
            self._transform("drain", "q", "done"),
        )
        reached = [node.id for node in _runtime_nodes_downstream_of_connection("rows", nodes)]
        assert reached == ["feed", "drain"], reached

    def test_backward_reach_continues_past_an_implicit_aggregation(self) -> None:
        """Site B. Widens the fork-branch seed roster for bound regions."""
        closer = self._node(
            id="co",
            node_type="coalesce",
            input="l_done",
            on_success="out",
            branches={"l": "l_done", "r": "r_done"},
            policy="require_all",
            merge="union",
        )
        nodes = (
            self._implicit_aggregation("agg", "seed"),
            self._transform("mid", "agg", "l_done"),
            self._transform("right", "r_in", "r_done"),
            closer,
        )
        reach = _closer_backward_reach_connections(nodes, closer)
        assert "seed" in reach, (
            f"Backward reach stopped at the aggregation, so its own input 'seed' was never reached. Got {sorted(reach)}."
        )

    def test_backward_reach_is_unchanged_for_queues(self) -> None:
        closer = self._node(
            id="co",
            node_type="coalesce",
            input="l_done",
            on_success="out",
            branches={"l": "l_done"},
            policy="require_all",
            merge="union",
        )
        nodes = (self._node(id="q", node_type="queue", input="q"), self._transform("drain", "q", "l_done"), closer)
        reach = _closer_backward_reach_connections(nodes, closer)
        assert "q" in reach and "l_done" in reach, sorted(reach)

    def test_fork_branch_sink_walk_continues_past_an_implicit_aggregation(self) -> None:
        """Site C. A sink reached THROUGH an in-region aggregation was invisible."""
        nodes = (
            self._implicit_aggregation("agg", "branch_l"),
            self._transform("leak", "agg", "main"),
            self._node(
                id="closer",
                node_type="coalesce",
                input="branch_l",
                on_success="out",
                branches={"l": "branch_l"},
                policy="require_all",
                merge="union",
            ),
        )
        hit = _fork_branch_reaches_sink_before_closer({"branch_l"}, "closer", nodes, frozenset({"main"}))
        assert hit == "main", f"The in-region sink leak was invisible because the walk stopped at the aggregation. Got {hit!r}."

    def test_fork_branch_sink_walk_reports_no_leak_when_there_is_none(self) -> None:
        """Control: the walk must still abstain when nothing reaches a sink."""
        nodes = (
            self._implicit_aggregation("agg", "branch_l"),
            self._transform("inner", "agg", "closer_in"),
            self._node(
                id="closer",
                node_type="coalesce",
                input="closer_in",
                on_success="out",
                branches={"l": "closer_in"},
                policy="require_all",
                merge="union",
            ),
        )
        assert _fork_branch_reaches_sink_before_closer({"branch_l"}, "closer", nodes, frozenset({"main"})) is None


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
            "abuse-contact-unset@elspeth.example.gov.au",
            "ops@somecompany.gov.au",
            "abuse@example.somecompany.gov.au",  # 'example' as a label, not the reserved TLD
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


class TestPromptTemplateUndeclaredRowFields:
    """A single-prompt llm node may declare one row field and reference another.

    Both authoring layers accepted that before elspeth-a9ba80cb0b: the edge
    contract is satisfied by the DECLARATION, so a producer guaranteeing the
    declared field passes, and every row then raised ``UndefinedError`` at
    render. The composer must carry this rule itself — its plugin probes do
    construct the node and do see the plugin's rejection, then swallow it
    through ``_is_config_probe_exception`` so a draft never crashes validation.
    """

    def _state(self, prompt_template: str, **options: object) -> CompositionState:
        node = NodeSpec(
            id="classify",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="classified",
            on_error="discard",
            options={"prompt_template": prompt_template, "model": "test-model", **options},
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

    def _errors(self, state: CompositionState) -> list[ValidationEntry]:
        return [e for e in state.validate().errors if e.error_code == "prompt_template_undeclared_row_fields"]

    def test_rejects_reference_outside_the_declaration(self) -> None:
        errors = self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=["case_study_1"]))
        assert errors, "expected prompt_template_undeclared_row_fields"
        entry = errors[0]
        assert entry.component == "node:classify"
        assert entry.severity == "high"
        assert "'case_study'" in entry.message
        assert "'case_study_1'" in entry.message

    def test_accepts_a_declared_reference(self) -> None:
        assert not self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=["case_study"]))

    def test_accepts_a_declaration_wider_than_the_template(self) -> None:
        assert not self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=["case_study", "audit_id"]))

    def test_empty_declaration_is_the_documented_opt_out(self) -> None:
        assert not self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=[]))

    def test_absent_declaration_is_the_sibling_validators_business(self) -> None:
        assert not self._errors(self._state("Rate: {{ row.case_study }}"))

    def test_multi_query_nodes_are_out_of_scope(self) -> None:
        """Multi-query binds ``row`` per query from ``input_fields``; its own rule owns that."""
        state = self._state(
            "Rate: {{ row.case_study }}",
            required_input_fields=["case_study_1"],
            queries={"q1": {"input_fields": {"case_study": "case_study_1"}}},
        )
        assert not self._errors(state)

    def test_original_header_literal_is_covered_by_its_canonical_key(self) -> None:
        """``row["Original Header"]`` is not a declarable name at all.

        It can therefore only be an ``original_name``, and the row key it
        resolves to is its canonical form — the one sound bridge, and it runs
        only in this direction.
        """
        assert not self._errors(self._state('Rate: {{ row["Original Header"] }}', required_input_fields=["original_header"]))

    def test_uncovered_original_header_literal_is_reported_with_its_declarable_form(self) -> None:
        errors = self._errors(self._state('Rate: {{ row["Original Header"] }}', required_input_fields=["something_else"]))
        assert errors
        assert "declare as 'original_header'" in errors[0].message

    def test_header_spelling_of_a_declared_field_is_accepted(self) -> None:
        """``{{ row.Name }}`` under a declared ``name`` reads it by its header spelling (ADR-051 (b), S-02).

        The composer shares ``undeclared_row_fields`` with the plugin layer, so
        it admits exactly what ``LLMConfig`` admits; a row whose header is
        spelled otherwise fails that row at render.
        """
        assert not self._errors(self._state("Hello {{ row.Name }}", required_input_fields=["name"]))

    def test_spelling_of_an_undeclared_field_is_reported(self) -> None:
        errors = self._errors(self._state("Hello {{ row.Title }}", required_input_fields=["name"]))
        assert errors
        assert "'Title'" in errors[0].message

    def test_interpretation_placeholder_does_not_silence_the_rule(self) -> None:
        """Unmasked, ``{{interpretation:...}}`` is a TemplateSyntaxError that would
        abstain BOTH limbs on every interpretation-carrying node."""
        errors = self._errors(
            self._state("Rate how {{interpretation:cool}} {{ row.case_study }} is.", required_input_fields=["case_study_1"])
        )
        assert errors
        assert "'case_study'" in errors[0].message

    def test_both_limbs_can_fire_on_one_node(self) -> None:
        """A bare name and an undeclared row field are separate defects with separate codes."""
        codes = {
            e.error_code for e in self._state("{{ text }} {{ row.case_study }}", required_input_fields=["case_study_1"]).validate().errors
        }
        assert "prompt_template_unbound_variables" in codes
        assert "prompt_template_undeclared_row_fields" in codes

    def test_message_routes_to_its_own_catalogue_entry(self) -> None:
        """``_VALIDATION_ERROR_PATTERNS`` matches in LIST ORDER against raw text.

        The two prompt_template codes are neighbours in that list and the
        unbound-variables pattern also matches the phrase "prompt render
        context does not define", so a message reusing that natural phrasing
        would be served the wrong repair — advice telling the planner to
        "rewrite each name as row.<field>" when the reference already IS
        row.<field>, which sends it in a circle.
        """
        from elspeth.web.composer.state import (
            _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX,
        )
        from elspeth.web.composer.tools.generation import _VALIDATION_ERROR_PATTERNS

        def first_match_fix(message: str) -> str | None:
            for pattern, _explanation, fix in _VALIDATION_ERROR_PATTERNS:
                if re.search(pattern, message):
                    return fix
            return None

        errors = self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=["case_study_1"]))
        assert errors
        assert first_match_fix(errors[0].message) is _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX

    def test_named_repair_clears_the_error(self) -> None:
        """Pin TRUTH, not existence: applying the advice must actually clear it.

        Both named remedies are exercised — declaring the field the template
        reads, and rewriting the reference to the field already declared.
        """
        assert not self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=["case_study"]))
        assert not self._errors(self._state("Rate: {{ row.case_study_1 }}", required_input_fields=["case_study_1"]))

    def test_a_malformed_declaration_is_the_type_rule_business_not_a_silent_bypass(self) -> None:
        """A declaration the composer cannot read must not read as "no declaration".

        ``declared_names`` filters non-strings, so ``[5, 6]`` collapses to empty
        and this rule stays silent — correct only because ``contract_config_invalid``
        owns the type rejection. Measured, not assumed; a mixed list still yields
        the shortfall for the names that ARE readable.
        """
        for declared in ([5, 6], "case_study_1", {}):
            codes = {e.error_code for e in self._state("Rate: {{ row.case_study }}", required_input_fields=declared).validate().errors}
            assert "contract_config_invalid" in codes, f"{declared!r} is not rejected by the type rule"

        assert self._errors(self._state("Rate: {{ row.case_study }}", required_input_fields=["case_study_1", 5]))

    def test_advice_leads_with_rewrite_and_qualifies_declaring(self) -> None:
        """Declaring a read name the producer does not guarantee is refused when
        the pipeline is validated (``schema_contract_violation``), so a repair
        that leads with it trades this error for another: the ordering of the
        two remedies is load-bearing."""
        from elspeth.web.composer.state import _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX as fix

        assert fix.index("Rewrite each reference") < fix.index("Add a name to options.required_input_fields")
        assert "ONLY if the upstream producer guarantees that exact name" in fix
        assert "refused when the pipeline is validated" in fix
        assert "patch_node_options replaces the option's value, it does not append" in fix

    def test_both_authoring_surfaces_carry_the_same_substantive_advice(self) -> None:
        """The plugin message and the composer message reach authors on disjoint
        paths and cannot be byte-identical — one names YAML, the other names
        composer tools. Their CLAIMS must still agree, or the same defect gets
        opposite guidance depending on how it was found (elspeth-920bd88299)."""
        from elspeth.plugins.transforms.llm.base import _UNDECLARED_ROW_FIELDS_REMEDY as plugin_fix
        from elspeth.web.composer.state import _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX as composer_fix

        for text in (plugin_fix, composer_fix):
            assert text.index("Rewrite each reference") < text.index("Add a name to options.required_input_fields")
            assert "ONLY if the upstream producer guarantees that exact name" in text
            assert "refused when the pipeline is validated" in text
            assert "withdraws the contract for every field" in text


class TestLLMPromptRolesRequired:
    """Every composer-authored ``llm`` node carries BOTH prompt roles.

    Session 60ab6a67 (2026-09-20): the user supplied a user prompt per A/B arm
    and the planner shipped both nodes with ``prompt_template`` only. The
    skill told it to fill the missing role; the plugin schema it read last
    called the field "Optional system prompt", and nothing at Stage 1 pushed
    back, so the omission reached the approval card unseen. A rejection the
    planner must repair is the enforcement; the server never writes the text.
    """

    def _state_with_llm(self, options: dict[str, Any]) -> CompositionState:
        node = NodeSpec(
            id="classify",
            node_type="transform",
            plugin="llm",
            input="rows",
            on_success="classified",
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

    def _errors(self, state: CompositionState, code: str) -> list[ValidationEntry]:
        return [e for e in state.validate().errors if e.error_code == code]

    def test_user_prompt_without_system_prompt_is_rejected(self) -> None:
        """The session-60ab6a67 shape: a supplied user prompt, no system role."""
        state = self._state_with_llm(
            {"profile": "sonnet", "prompt_template": "Classify: {{ row.text }}", "required_input_fields": ["text"]}
        )
        errors = self._errors(state, "llm_system_prompt_missing")
        assert len(errors) == 1
        entry = errors[0]
        assert entry.component == "node:classify"
        assert entry.severity == "high"
        assert "options.system_prompt" in entry.message
        assert not self._errors(state, "llm_user_prompt_missing")

    @pytest.mark.parametrize("blank", [None, "", "   \n\t"])
    def test_blank_system_prompt_is_not_a_system_prompt(self, blank: str | None) -> None:
        """``transform.py`` sends the system message only ``if self.system_prompt``."""
        state = self._state_with_llm({"prompt_template": "Classify: {{ row.text }}", "system_prompt": blank})
        assert len(self._errors(state, "llm_system_prompt_missing")) == 1

    def test_both_roles_present_is_accepted(self) -> None:
        state = self._state_with_llm(
            {"prompt_template": "Classify: {{ row.text }}", "system_prompt": "You classify support tickets by sentiment."}
        )
        assert not self._errors(state, "llm_system_prompt_missing")
        assert not self._errors(state, "llm_user_prompt_missing")

    def test_system_prompt_without_user_prompt_is_rejected(self) -> None:
        state = self._state_with_llm({"system_prompt": "You classify support tickets by sentiment."})
        errors = self._errors(state, "llm_user_prompt_missing")
        assert len(errors) == 1
        assert errors[0].component == "node:classify"
        assert errors[0].severity == "high"
        assert "options.prompt_template" in errors[0].message
        assert not self._errors(state, "llm_system_prompt_missing")

    def test_node_with_neither_role_reports_both(self) -> None:
        state = self._state_with_llm({"profile": "sonnet"})
        assert len(self._errors(state, "llm_system_prompt_missing")) == 1
        assert len(self._errors(state, "llm_user_prompt_missing")) == 1

    def test_multi_query_node_needs_the_shared_system_prompt(self) -> None:
        """``system_prompt`` is shared by every query, so one rule covers both modes."""
        state = self._state_with_llm({"queries": {"tone": {"template": "Tone of {{ row.text }}?", "input_fields": {"text": "text"}}}})
        assert len(self._errors(state, "llm_system_prompt_missing")) == 1
        assert not self._errors(state, "llm_user_prompt_missing")

    @pytest.mark.parametrize(
        "queries",
        [
            pytest.param(
                {
                    "tone": {"template": "Tone of {{ row.text }}?", "input_fields": {"text": "text"}},
                    "urgency": {"template": "Urgency of {{ row.text }}?", "input_fields": {"text": "text"}},
                },
                id="mapping-form",
            ),
            pytest.param(
                [
                    {"name": "tone", "template": "Tone of {{ row.text }}?", "input_fields": {"text": "text"}},
                    {"name": "urgency", "template": "Urgency of {{ row.text }}?", "input_fields": {"text": "text"}},
                ],
                id="list-form",
            ),
        ],
    )
    def test_multi_query_one_system_prompt_many_user_prompts_is_the_supported_shape(self, queries: Any) -> None:
        """One shared ``system_prompt`` plus a user prompt per query needs no node-level ``prompt_template``."""
        state = self._state_with_llm({"system_prompt": "You assess support tickets.", "queries": queries})
        assert not self._errors(state, "llm_system_prompt_missing")
        assert not self._errors(state, "llm_user_prompt_missing")

    def test_multi_query_without_any_user_template_names_the_query(self) -> None:
        """A query with no ``template`` falls back to the node-level ``prompt_template``; with neither it has no user prompt."""
        state = self._state_with_llm(
            {
                "system_prompt": "You assess support tickets.",
                "queries": {
                    "tone": {"template": "Tone of {{ row.text }}?", "input_fields": {"text": "text"}},
                    "urgency": {"input_fields": {"text": "text"}},
                },
            }
        )
        errors = self._errors(state, "llm_user_prompt_missing")
        assert len(errors) == 1
        assert "'urgency'" in errors[0].message
        assert "'tone'" not in errors[0].message

    def test_multi_query_falls_back_to_the_node_level_template(self) -> None:
        state = self._state_with_llm(
            {
                "system_prompt": "You assess support tickets.",
                "prompt_template": "Assess {{ row.text }}",
                "queries": {"urgency": {"input_fields": {"text": "text"}}},
            }
        )
        assert not self._errors(state, "llm_user_prompt_missing")

    def test_user_uploaded_blob_marker_counts_as_supplied(self) -> None:
        """ADR-034 lets a user-uploaded prompt artifact back either role as an inline marker mapping.

        Whether the marker is well formed and user-authored is owned by the
        inline-blob rules; this rule only asks whether the role was supplied.
        """
        marker = {"inline_content": {"blob_id": "00000000-0000-0000-0000-000000000001"}}
        state = self._state_with_llm({"prompt_template": marker, "system_prompt": marker})
        assert not self._errors(state, "llm_system_prompt_missing")
        assert not self._errors(state, "llm_user_prompt_missing")

    def test_non_llm_transform_is_not_subject_to_the_rule(self) -> None:
        node = NodeSpec(
            id="tidy",
            node_type="transform",
            plugin="field_mapper",
            input="rows",
            on_success="tidied",
            on_error="discard",
            options={"mapping": {"id": "id"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(source=None, nodes=(node,), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        codes = {e.error_code for e in state.validate().errors}
        assert "llm_system_prompt_missing" not in codes
        assert "llm_user_prompt_missing" not in codes


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

    def test_shared_node_template_column_check_does_not_scale_with_query_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Queries without an override share the node-level prompt_template; the
        required-input column and generated-input checks must analyse it a
        constant number of times, not once per query (#230: O(queries x
        template) on a composer edit)."""
        from elspeth.web.composer import state as state_module

        template = "Assess {{ row.input_1 }} against {{ row.source_row.column_0 }}"
        shared_template_calls = 0

        def counting(original: Any) -> Any:
            def wrapper(candidate: str, *args: Any, **kwargs: Any) -> Any:
                nonlocal shared_template_calls
                if template in candidate:
                    shared_template_calls += 1
                return original(candidate, *args, **kwargs)

            return wrapper

        monkeypatch.setattr(state_module, "_parse_template_names", counting(state_module._parse_template_names))
        monkeypatch.setattr(state_module, "multi_query_source_row_columns", counting(state_module.multi_query_source_row_columns))

        def calls_for(query_count: int) -> int:
            nonlocal shared_template_calls
            shared_template_calls = 0
            node = replace(
                self._state(template, {}).nodes[0],
                options={
                    "prompt_template": template,
                    "model": "test-model",
                    "schema": {"mode": "observed"},
                    "required_input_fields": [f"column_{index}" for index in range(query_count)],
                    "queries": [{"name": f"query_{index}", "input_fields": {"input_1": f"column_{index}"}} for index in range(query_count)],
                },
            )
            assert state_module._validate_multi_query_required_input_columns(node) == ()
            assert state_module._validate_multi_query_generated_input_requirements(node) == ()
            return shared_template_calls

        assert calls_for(1) == calls_for(50)

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

    @pytest.mark.parametrize("required", [["name"], ["Name"]], ids=["canonical", "header_spelling_candidate"])
    def test_an_abstaining_queue_is_reported_once_per_consumer(self, required: list[str]) -> None:
        # The presence rule and the field-name spelling rule both walk the
        # consumer's connection back through the queue. Each skipped check is
        # one warning: the spelling pass must not repeat what the presence
        # rule already reported, whether or not its declarations can be
        # header spellings (measured: 2 identical warnings for both shapes
        # before the spelling pass kept its walk's reports apart).
        state = self._state(
            sources={"orders": self._source(), "refunds": self._source()},
            nodes=(self._queue(), self._consumer(required=required)),
            outputs=(self._sink(),),
        )
        result = state.validate()
        assert len(self._queue_skip_warnings(result)) == 1

    def test_shared_queue_guarantee_subgraphs_are_evaluated_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A layered queue diamond stays linear in authored graph size."""
        from elspeth.web.composer._producer_resolver import ProducerResolver

        depth = 8
        nodes: list[NodeSpec] = [self._queue("queue_0_a"), self._queue("queue_0_b")]
        for layer in range(1, depth + 1):
            previous = layer - 1
            destinations = (f"queue_{layer}_a", f"queue_{layer}_b")
            for branch in ("a", "b"):
                nodes.append(
                    NodeSpec(
                        id=f"gate_{layer}_{branch}",
                        node_type="gate",
                        plugin=None,
                        input=f"queue_{previous}_{branch}",
                        on_success=None,
                        on_error=None,
                        options={},
                        condition="True",
                        routes={"true": destinations[0], "false": destinations[1]},
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            nodes.extend((self._queue(destinations[0]), self._queue(destinations[1])))
        nodes.extend(
            (
                self._consumer(input=f"queue_{depth}_a", required=["shared"]),
                self._consumer("drain", input=f"queue_{depth}_b", on_success="drained"),
            )
        )

        calls = 0
        original = ProducerResolver.queue_predecessors

        def counting_queue_predecessors(resolver: ProducerResolver, queue_id: str):
            nonlocal calls
            calls += 1
            return original(resolver, queue_id)

        monkeypatch.setattr(ProducerResolver, "queue_predecessors", counting_queue_predecessors)
        state = self._state(
            sources={
                "left": self._source(guarantees=["shared"], on_success="queue_0_a"),
                "right": self._source(guarantees=["shared"], on_success="queue_0_b"),
            },
            nodes=tuple(nodes),
            outputs=(self._sink(), self._sink("drained")),
        )

        result = state.validate()

        assert result.is_valid, [error.message for error in result.errors]
        assert calls <= (2 * depth) + 2

    def test_gate_only_cycle_abstains_instead_of_recursing(self) -> None:
        def gate(node_id: str, input_connection: str, output_connection: str) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="gate",
                plugin=None,
                input=input_connection,
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": output_connection, "false": output_connection},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )

        state = self._state(
            sources={"source": self._source(guarantees=["shared"], on_success="unused")},
            nodes=(
                gate("gate_a", "loop_b", "loop_a"),
                gate("gate_b", "loop_a", "loop_b"),
                self._consumer(input="loop_b", required=["shared"]),
            ),
            outputs=(self._sink(),),
        )

        result = state.validate()

        assert result.is_valid is False
        assert result.errors

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


@pytest.mark.parametrize("on_error", ["missing_error_sink", "merge"])
def test_aggregation_on_error_must_reference_declared_sink(on_error: str) -> None:
    """An aggregation's dangling on_error is an unknown sink like a transform's.

    No rule-9 closer relax for an aggregation: a closer-shaped value ('merge'
    names a coalesce here) is still an unknown sink, because rule 6 keeps
    aggregations out of every bound region (elspeth-d2e3f29d10 parity with
    the DAG builder).
    """
    aggregation = NodeSpec(
        id="stats",
        node_type="aggregation",
        plugin="batch_stats",
        input="rows",
        on_success="high",
        on_error=on_error,
        options={"schema": {"mode": "observed"}, "value_field": "amount"},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        trigger={"count": 2},
        output_mode="transform",
    )
    merge = NodeSpec(
        id="merge",
        node_type="coalesce",
        plugin=None,
        input="merge_in",
        on_success="high",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches={"a": "a_in", "b": "b_in"},
        policy="require_all",
        merge="union",
    )
    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard"),
        nodes=(aggregation, merge),
        edges=(),
        outputs=(OutputSpec(name="high", plugin="csv", options={}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )

    result = state.validate()

    assert ("node:stats", "aggregation_on_error_unknown_sink") in {(entry.component, entry.error_code) for entry in result.errors}


def test_gate_fork_branches_must_reach_a_coalesce_branch_or_sink() -> None:
    """Mirror the engine's fork-branch destination rule at composition time.

    Live session d4ea3d8a: committed pipeline failed engine pre-run because
    the gate's fork_to names ('branch_a'/'branch_b') appeared nowhere in any
    coalesce branches keys or sink names — the model keyed coalesce branches
    by incoming connection instead of fork branch name. Composer validation
    accepted it: valid-but-not-runnable.

    Since the E2 destination-rule fix (a fork branch may feed an ordinary
    downstream consumer), 'branch_b' — consumed by the 'usage' transform —
    is a LEGAL destination, so this topology's defect is now mixed closure
    (spec §7 rule 2: 'branch_a' row_union-bound, 'branch_b' unbound), not a
    missing destination. The genuinely-orphaned-branch case lives in
    test_state_bound_regions.py::test_orphan_fork_branch_still_has_no_destination.
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
    assert ("node:fork_rows", "fork_mixed_closure_invalid") in entries, entries
    # E2: a consumer-fed branch is a legal destination — the old code must NOT fire here.
    assert not any(e.error_code == "fork_branch_no_destination" for e in result.errors), entries
    offending = [e for e in result.errors if e.error_code == "fork_mixed_closure_invalid"]
    assert len(offending) == 1, offending
    assert "branch_b" in offending[0].message
    assert "['branch_a']" in offending[0].message  # bound side named as the barrier-closing set
    assert "mixed closure" in offending[0].message


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

    def test_every_runtime_merge_strategy_is_adjudicated(self) -> None:
        """No merge strategy may be silently unaccounted for.

        The strategy list is READ FROM the runtime model
        (``CoalesceSettings.model_fields["merge"]``'s Literal), never restated
        here — the same rule the fix itself follows. Each strategy must be in
        exactly one of two populations:

        * MIRRORED — Composer derives its guarantees from
          ``merge_coalesce_schema`` and Rule A/B adjudicate the coalesce;
        * UNMIRRORABLE — Composer abstains with the "runtime validator will
          check this edge" advisory, and authoring rejects the node outright so
          the abstention can never gate an acceptance.

        A new strategy added to the runtime Literal lands in NEITHER and fails
        here, which is the point: the nested defect existed because a strategy
        sat in the abstaining population by default with no one adjudicating
        it. Silence is the failure mode this test removes.
        """
        from typing import get_args

        from elspeth.core.config import CoalesceSettings
        from elspeth.web.composer.state import _MIRRORED_COALESCE_MERGES

        runtime_strategies = frozenset(get_args(CoalesceSettings.model_fields["merge"].annotation))
        unmirrorable = frozenset({"select"})

        assert runtime_strategies >= _MIRRORED_COALESCE_MERGES, (
            "Composer claims to mirror a merge strategy the runtime does not define",
            sorted(_MIRRORED_COALESCE_MERGES - runtime_strategies),
        )
        unaccounted = runtime_strategies - _MIRRORED_COALESCE_MERGES - unmirrorable
        assert not unaccounted, (
            f"Merge strategy {sorted(unaccounted)} is adjudicated by neither population. "
            "Either mirror it (derive its guarantees from merge_coalesce_schema, and pin the "
            "engine's verdict on the same pipeline) or declare it unmirrorable (name the input "
            "the composer NodeSpec cannot carry, and reject it at authoring).",
        )
        assert not (_MIRRORED_COALESCE_MERGES & unmirrorable)

    def test_unmirrorable_merge_is_rejected_at_authoring(self) -> None:
        """The abstention for ``select`` is safe ONLY because authoring refuses it.

        If a future change legalises ``select`` without giving Composer a
        ``select_branch`` to mirror with, this seam silently reverts to the
        nested defect: an abstention that lets a pipeline the runtime rejects
        validate green. This test is the tripwire for that day.
        """
        state = self._state(
            coalesce=self._coalesce(merge="select", options={"select_branch": "control_branch"}),
            tail=self._passthrough("after_merge", "variant_merge", "output", options={"schema": {"mode": "observed"}}),
        )

        result = state.validate()

        assert "coalesce_merge_select_unsupported" in [error.error_code for error in result.errors], result.errors

    @pytest.mark.parametrize("merge", ["select"])
    def test_unmirrorable_coalesce_keeps_the_skip_warning(self, merge: str) -> None:
        """The ``_MIRRORED_COALESCE_MERGES`` scope gate, in the shape that trips it.

        ``select`` forwards ONE branch's raw schema keyed by a ``select_branch``
        a composer ``NodeSpec`` cannot carry, so Composer has nothing to mirror
        and abstaining with its advisory is the correct answer — the runtime
        validator remains authoritative for that merge.

        Parametrized over a single value deliberately: ``nested`` USED to sit
        in this population on the argument that "the propagation vote mirrors
        neither". That argument was empirically false — see
        ``test_nested_coalesce_reports_the_branch_name_extras`` for the engine
        rejection this abstention was hiding — so nested moved into the
        mirrored set and only ``select`` remains. Adding a merge back here
        needs the same evidence: an engine build that AGREES with the
        abstention. ``test_every_runtime_merge_strategy_is_adjudicated`` is the
        gate that makes that decision mandatory rather than optional.
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

    def test_nested_coalesce_reports_the_branch_name_extras(self) -> None:
        """A nested merge emits BRANCH NAMES, and a locked tail must see them.

        The engine builds this exact pipeline and rejects it with
        ``EdgeContractError``: "Consumer (passthrough) input is locked (mode:
        fixed) and accepts: ['verdict']. Producer (coalesce:merge_results)
        guarantees fields: ['branch_a', 'branch_b']". Stage 1 abstained here
        instead, so the authoring loop got a green preview and a runtime that
        would not run — the elspeth-ae83a6b60c shape that survived for nested
        after the union half closed.

        The extras set is NOT re-derived here. The vote dispatches on the merge
        strategy exactly as ``core/dag/coalesce_merge.merge_coalesce_schema``
        does and calls that same function, so the branch-name set is the
        runtime's own.
        """
        state = self._state(
            coalesce=self._coalesce(merge="nested", policy="require_all"),
            tail=self._passthrough(
                "after_merge",
                "variant_merge",
                "output",
                options=self._locked(["verdict: str"]),
            ),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("control_branch", "treatment_branch")
        assert not [
            warning for warning in result.warnings if "Contract check skipped" in warning.message and "coalesce" in warning.message
        ], result.warnings

    def test_definite_emits_traverse_nested_coalesce_behind_pass_through(self) -> None:
        """The nested twin of the site-3 test above — the only topology that
        arm reaches.

        With an extras-allowing relay between the coalesce and the locked sink,
        the walk-back and the emit profile never see the coalesce; only
        ``_connection_definite_emits`` crosses it. Widening the seam without
        this leg would leave the third of three sites unverified for nested.

        Engine parity, measured on the equivalent settings graph: it rejects
        the same two-hop pipeline with ``EdgeContractError`` naming
        ``['branch_a', 'branch_b']`` as the producer's guaranteed fields, and
        BUILDS it under a lossy policy — the pairing
        ``test_nested_coalesce_under_a_lossy_policy_promises_nothing`` holds
        for the one-hop shape.
        """
        state = self._state(
            coalesce=self._coalesce(merge="nested", policy="require_all"),
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
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("control_branch", "treatment_branch")

    def test_nested_coalesce_under_a_lossy_policy_promises_nothing(self) -> None:
        """A branch may be lost, so no branch name is guaranteed — and the
        engine agrees: the same pipeline under ``policy="first"`` BUILDS.

        The lower-bound discipline of the extras direction: reporting branch
        names here would be a false red against a runtime that marks every
        nested field optional (``merge_coalesce_schema``'s
        ``optional = not require_all``).
        """
        state = self._state(
            coalesce=self._coalesce(merge="nested", policy="first"),
            tail=self._passthrough(
                "after_merge",
                "variant_merge",
                "output",
                options=self._locked(["verdict: str"]),
            ),
        )

        result = state.validate()

        assert not [error for error in result.errors if error.error_code == "locked_input_extras"], result.errors

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


class TestOpenProducerProvesNoAbsence:
    """Composer mirror of the runtime ``TestOpenProducerProvesNoAbsence``.

    ``declared_missing`` is a set DIFFERENCE, so it proves ABSENCE and needs an
    UPPER bound on what a producer emits. ``producer_guaranteed`` is a LOWER
    one. An ``observed`` source naming ``guaranteed_fields: [id]`` participates
    in the vote AND still admits every other column, so subtracting its
    guarantee reported a miss for the pass-through columns its rows genuinely
    carry — a false REJECT of a pipeline the engine builds and runs
    (elspeth-9c5ff8fa7d).

    Both tests are load-bearing and must move together: the first pins that an
    OPEN producer no longer manufactures a rejection, the second that a CLOSED
    producer which provably omits the column still does. Gating the block on
    closedness alone would pass the first; deleting the check entirely would
    pass the first and FAIL the second.
    """

    def _state(self, source_options: dict[str, Any]) -> CompositionState:
        """source -> web_scrape whose ``url_field`` names a column the source never declares."""
        source = SourceSpec(
            plugin="csv",
            on_success="rows",
            options=source_options,
            on_validation_failure="discard",
        )
        scraper = NodeSpec(
            id="scraper",
            node_type="transform",
            plugin="web_scrape",
            input="rows",
            on_success="output",
            on_error="discard",
            options={
                "url_field": "colour",
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
            nodes=(scraper,),
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

    def _declared_input_errors(self, state: CompositionState) -> list[str]:
        return [
            error.message
            for error in state.validate().errors
            if error.error_code == "schema_contract_violation" and "declared by its own options" in error.message
        ]

    def test_an_open_producer_naming_a_guarantee_proves_no_absence(self) -> None:
        """The false red: observed + guaranteed_fields participates but admits extras."""
        state = self._state({"schema": {"mode": "observed", "guaranteed_fields": ["id"]}})

        assert self._declared_input_errors(state) == []

    def test_a_closed_producer_that_omits_the_column_still_rejects(self) -> None:
        """The gate that must NOT be lost: a fixed schema forbids extras, so absence is provable."""
        state = self._state({"schema": {"mode": "fixed", "fields": ["id: str"], "guaranteed_fields": ["id"]}})

        messages = self._declared_input_errors(state)
        assert len(messages) == 1, messages
        assert "colour" in messages[0]


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


class TestRowUnionDownstreamBanDoesNotWalkASentinel:
    """The ban must skip when there is no connection, not walk ``""``.

    ``row_union_downstream_group_invalid`` forbids a correlated barrier directly
    downstream of a row_union. It resolved its connection as
    ``node.on_success or ""`` — and an empty connection name is NOT a no-op to
    ``_runtime_nodes_downstream_of_connection``: ``NodeSpec`` accepts
    ``input=""``, so the walk matched any empty-input node and this ban accused
    it of being downstream of the row_union (elspeth-6b48bda677).

    The state IS already invalid in that case — ``row_union_on_success_invalid``
    reports it in the same pass, because that guard COLLECTS rather than
    short-circuiting — which is exactly why the ban still runs and why the
    sentinel mattered. A second error computed from a state already known bad
    named two nodes that had no relationship at all.

    Deliberately NOT fixed by calling ``published_success_connection`` like the
    rest of its family: row_union is absent from
    ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` BECAUSE it requires ``on_success``,
    so the authority returns ``None`` here and calling it would change nothing.
    """

    @staticmethod
    def _state(ru_on_success: str | None, ghost_input: str) -> CompositionState:
        def node(**kw: Any) -> NodeSpec:
            base: dict[str, Any] = {
                "id": None,
                "node_type": "transform",
                "plugin": None,
                "input": None,
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": None,
                "routes": None,
                "fork_to": None,
                "branches": None,
                "policy": None,
                "merge": None,
            }
            base.update(kw)
            return NodeSpec(**base)

        state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        state = state.with_source(
            SourceSpec(
                plugin="csv", on_success="rows", options={"path": "in.csv", "schema": {"mode": "observed"}}, on_validation_failure="discard"
            )
        )
        state = state.with_node(
            node(id="fan", node_type="gate", input="rows", condition="'all'", routes={"all": "fork"}, fork_to=("a", "b"))
        )
        state = state.with_node(
            node(id="pa", plugin="passthrough", input="a", on_success="a_out", on_error="discard", options={"schema": {"mode": "observed"}})
        )
        state = state.with_node(
            node(id="pb", plugin="passthrough", input="b", on_success="b_out", on_error="discard", options={"schema": {"mode": "observed"}})
        )
        state = state.with_node(
            node(
                id="ru",
                node_type="row_union",
                input="a_out",
                branches={"a": "a_out", "b": "b_out"},
                policy="require_all",
                on_success=ru_on_success,
            )
        )
        state = state.with_node(
            node(
                id="ghost_co",
                node_type="coalesce",
                input=ghost_input,
                branches={"x": ghost_input, "y": ghost_input},
                policy="require_all",
                merge="union",
            )
        )
        return state.with_output(
            OutputSpec(name="main", plugin="csv", options={"path": "o.csv", "schema": {"mode": "observed"}}, on_write_failure="discard")
        )

    @staticmethod
    def _codes(state: CompositionState) -> set[str]:
        return {entry.error_code for entry in state.validate().errors}

    def test_a_genuinely_downstream_barrier_is_still_rejected(self) -> None:
        """The ban's real job must survive the fix."""
        assert "row_union_downstream_group_invalid" in self._codes(self._state("ru_out", "ru_out"))

    def test_an_unrelated_empty_input_node_is_not_accused(self) -> None:
        """THE regression. ``ghost_co`` is not downstream of ``ru`` by any edge.

        Mutating the resolution back to ``node.on_success or ""`` reds exactly
        this test — a two-case pin (fires with on_success, silent without) passes
        against the buggy code, which is why the false positive survived the
        first diagnosis.
        """
        assert "row_union_downstream_group_invalid" not in self._codes(self._state(None, ""))

    def test_the_real_error_is_still_reported(self) -> None:
        """Skipping the ban must not hide why the state is invalid."""
        assert "row_union_on_success_invalid" in self._codes(self._state(None, ""))


# --- tier-rem/web-composer: pinning tests -----------------------------------


def _tier_rem_node(**overrides: Any) -> NodeSpec:
    defaults: dict[str, Any] = {
        "id": "n1",
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "in",
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


def test_coalesce_non_string_branch_connection_is_rejected_at_composition_time() -> None:
    """A persisted payload can hand NodeSpec.from_dict a non-string coalesce
    branch connection; without an intrinsic type check it validated green here
    and died at the runtime's CoalesceSettings load (valid-but-not-runnable)."""
    state = CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    state = state.with_source(SourceSpec(plugin="csv", on_success="g_in", options={}, on_validation_failure="discard"))
    state = state.with_node(
        _tier_rem_node(
            id="g",
            node_type="gate",
            plugin=None,
            input="g_in",
            on_success=None,
            on_error=None,
            condition="True",
            routes={"true": "fork", "false": "fork"},
            fork_to=("x",),
        )
    )
    state = state.with_node(_tier_rem_node(id="tx", input="x", on_success="cx"))
    state = state.with_node(
        _tier_rem_node(
            id="c",
            node_type="coalesce",
            plugin=None,
            input="cx",
            on_success="main",
            on_error=None,
            branches={"x": 7},
            policy="require_all",
            merge="union",
        )
    )
    state = state.with_output(OutputSpec(name="main", plugin="csv", options={}, on_write_failure="discard"))

    result = state.validate()

    messages = [e.message for e in result.errors if e.error_code == "coalesce_branches_invalid"]
    assert any("must be a string (got int)" in message for message in messages), messages


def test_coalesce_non_string_branch_name_is_rejected_at_composition_time() -> None:
    """The branch NAME operand of the same intrinsic type check.

    ``_routing_label_errors`` skips a pair whose branch name is not a string,
    so the rejection must come from the intrinsic coalesce shape check, which
    runs ``type(value) is not str`` over the branch name and the branch
    connection in one loop. This test plants the name operand; its sibling
    above plants the connection operand.
    """
    state = CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    state = state.with_source(SourceSpec(plugin="csv", on_success="g_in", options={}, on_validation_failure="discard"))
    state = state.with_node(
        _tier_rem_node(
            id="g",
            node_type="gate",
            plugin=None,
            input="g_in",
            on_success=None,
            on_error=None,
            condition="True",
            routes={"true": "fork", "false": "fork"},
            fork_to=("x",),
        )
    )
    state = state.with_node(_tier_rem_node(id="tx", input="x", on_success="cx"))
    state = state.with_node(
        _tier_rem_node(
            id="c",
            node_type="coalesce",
            plugin=None,
            input="cx",
            on_success="main",
            on_error=None,
            branches={7: "cx"},
            policy="require_all",
            merge="union",
        )
    )
    state = state.with_output(OutputSpec(name="main", plugin="csv", options={}, on_write_failure="discard"))

    result = state.validate()

    assert not result.is_valid
    messages = [e.message for e in result.errors if e.error_code == "coalesce_branches_invalid"]
    assert any("branch name must be a string (got int)" in message for message in messages), messages


def test_template_syntax_rejection_is_owned_by_plugin_config_not_advisory_rules() -> None:
    """The composer's template advisory rules abstain on unparseable templates;
    the raising rejection is owned by LLMConfig (pydantic ValidationError at
    plugin-config admission), so the abstention loses no integrity signal."""
    from pydantic import ValidationError

    from elspeth.contracts.schema import SchemaConfig
    from elspeth.plugins.transforms.llm.base import LLMConfig
    from elspeth.plugins.transforms.llm.multi_query import QueryDefinition
    from elspeth.web.composer.state import (
        _parse_template_names,
        _validate_prompt_template_variable_bindings,
    )

    bad_template = "{% if %}"
    parsed, syntax_error = _parse_template_names(bad_template)
    assert parsed is None
    assert syntax_error is not None and "Expected an expression" in syntax_error
    node = _tier_rem_node(id="llm1", plugin="llm", options={"prompt_template": bad_template})
    assert _validate_prompt_template_variable_bindings(node) == ()

    with pytest.raises(ValidationError) as node_error:
        LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template=bad_template,
            schema_config=SchemaConfig(mode="observed", fields=None),
        )
    assert any(error["loc"] == ("prompt_template",) for error in node_error.value.errors())

    with pytest.raises(ValidationError) as query_error:
        QueryDefinition(input_fields={"text": "text"}, template=bad_template)
    assert any(error["loc"] == ("template",) for error in query_error.value.errors())
