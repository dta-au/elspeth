"""Edge/route reconciliation contract — Lane W2 (elspeth-67b44040ee).

Visual edges (EdgeSpec) and NodeSpec scalar routing fields are two views of
one runtime routing decision. This module pins the reconciliation contract:

- elspeth-372e18e365: a scalar route and its visual edge commit atomically
  from one model — no mutation order can persist a graph/runtime mismatch.
- elspeth-2c18c2127e: every visual node/edge combination that cannot lower
  to a valid runtime route is rejected before persistence.
- elspeth-eb4127fb49: Stage-1 validation enforces the same route contract on
  every entry path, including states deserialized or bulk-imported with
  edges the mutation tools never saw.
"""

from __future__ import annotations

from typing import Any

from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from tests.unit.web.composer.test_tools import (
    _empty_state,
    _mock_catalog,
    execute_tool,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _transform_args(node_id: str, *, input_: str = "in", on_success: str = "out") -> dict[str, Any]:
    return {
        "id": node_id,
        "node_type": "transform",
        "plugin": "passthrough",
        "input": input_,
        "on_success": on_success,
        "options": {"schema": {"mode": "observed"}},
    }


def _output_args(name: str) -> dict[str, Any]:
    return {
        "sink_name": name,
        "plugin": "csv",
        "options": {"path": f"/data/outputs/test-session/{name}.csv", "schema": {"mode": "observed"}},
        "on_write_failure": "discard",
    }


def _edge_args(edge_id: str, from_node: str, to_node: str, edge_type: str) -> dict[str, Any]:
    return {"id": edge_id, "from_node": from_node, "to_node": to_node, "edge_type": edge_type, "label": None}


def _state_with(*tool_calls: tuple[str, dict[str, Any]]) -> CompositionState:
    """Apply tool calls in order to an empty state; every call must succeed."""
    catalog = _mock_catalog()
    state = _empty_state()
    for tool_name, args in tool_calls:
        result = execute_tool(tool_name, args, state, catalog)
        assert result.success is True, f"fixture setup failed at {tool_name}: {result.data}"
        state = result.updated_state
    return state


def _node(state: CompositionState, node_id: str) -> NodeSpec:
    return next(node for node in state.nodes if node.id == node_id)


def _edge(state: CompositionState, edge_id: str) -> EdgeSpec:
    return next(edge for edge in state.edges if edge.id == edge_id)


def _node_spec(
    node_id: str,
    node_type: str,
    *,
    plugin: str | None = None,
    input_: str = "in",
    on_success: str | None = None,
    on_error: str | None = None,
    condition: str | None = None,
    routes: dict[str, str] | None = None,
    fork_to: tuple[str, ...] | None = None,
    branches: dict[str, str] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,  # type: ignore[arg-type]
        plugin=plugin,
        input=input_,
        on_success=on_success,
        on_error=on_error,
        options={},
        condition=condition,
        routes=routes,
        fork_to=fork_to,
        branches=branches,
        policy=None,
        merge=None,
    )


def _raw_state(
    *,
    nodes: tuple[NodeSpec, ...] = (),
    edges: tuple[EdgeSpec, ...] = (),
    outputs: tuple[OutputSpec, ...] = (),
    source_on_success: str = "in",
) -> CompositionState:
    """Build a state directly, bypassing the mutation tools.

    Models the import/deserialization entry paths, which construct
    CompositionState without ever running the tool-level admission checks.
    """
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success=source_on_success,
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=edges,
        outputs=outputs,
        metadata=PipelineMetadata(),
        version=1,
    )


def _sink(name: str) -> OutputSpec:
    return OutputSpec(name=name, plugin="csv", options={}, on_write_failure="discard")


def _error_codes(state: CompositionState) -> list[str | None]:
    return [entry.error_code for entry in state.validate().errors]


def _errors_for(state: CompositionState, code: str) -> list[str]:
    return [entry.message for entry in state.validate().errors if entry.error_code == code]


# ---------------------------------------------------------------------------
# elspeth-372e18e365 — atomic scalar-route/visual-edge commits
# ---------------------------------------------------------------------------


class TestSinkEdgeMirrorAtomicity:
    def _base(self) -> CompositionState:
        return _state_with(
            ("upsert_node", _transform_args("t1", on_success="mid")),
            ("upsert_node", _transform_args("t2", input_="mid", on_success="out")),
            ("set_output", _output_args("err_a")),
            ("set_output", _output_args("err_b")),
        )

    def test_retargeting_edge_to_new_from_node_clears_stale_mirror(self) -> None:
        """Re-pointing an existing edge id at a new source node must clear the
        route the old node carried for that edge."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error"), state, catalog)
        assert r1.success is True
        assert _node(r1.updated_state, "t1").on_error == "err_a"

        r2 = execute_tool("upsert_edge", _edge_args("e1", "t2", "err_a", "on_error"), r1.updated_state, catalog)
        assert r2.success is True
        assert _node(r2.updated_state, "t2").on_error == "err_a"
        # The old carrier must not keep routing to err_a: no edge expresses it.
        assert _node(r2.updated_state, "t1").on_error != "err_a"

    def test_retargeting_edge_to_new_edge_type_clears_old_slot(self) -> None:
        """Changing an edge's type must clear the slot the old type wrote."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t2", "err_a", "on_success"), state, catalog)
        assert r1.success is True
        assert _node(r1.updated_state, "t2").on_success == "err_a"

        r2 = execute_tool("upsert_edge", _edge_args("e1", "t2", "err_a", "on_error"), r1.updated_state, catalog)
        assert r2.success is True
        assert _node(r2.updated_state, "t2").on_error == "err_a"
        assert _node(r2.updated_state, "t2").on_success != "err_a"

    def test_retargeting_edge_to_new_sink_moves_mirror(self) -> None:
        """Same slot, new sink target: the scalar follows the edge."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error"), state, catalog)
        r2 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_b", "on_error"), r1.updated_state, catalog)
        assert r2.success is True
        assert _node(r2.updated_state, "t1").on_error == "err_b"

    def test_second_edge_for_same_route_slot_is_rejected(self) -> None:
        """One routing slot, one edge: a second edge id claiming the same
        (from_node, edge_type) slot is ambiguous and must be rejected."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error"), state, catalog)
        assert r1.success is True

        r2 = execute_tool("upsert_edge", _edge_args("e2", "t1", "err_a", "on_error"), r1.updated_state, catalog)
        assert r2.success is False
        assert r2.updated_state is r1.updated_state
        assert "e1" in str(r2.data)

    def test_remove_edge_on_legacy_duplicate_state_keeps_live_mirror(self) -> None:
        """A persisted state may carry semantic duplicates from before the
        admission rule. Removing one duplicate must not clear a route that a
        surviving edge still expresses."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error"), state, catalog)
        duplicated = r1.updated_state.with_edge(EdgeSpec(id="e2", from_node="t1", to_node="err_a", edge_type="on_error", label=None))

        removed = execute_tool("remove_edge", {"id": "e1"}, duplicated, catalog)
        assert removed.success is True
        assert _node(removed.updated_state, "t1").on_error == "err_a"

    def test_upsert_node_scalar_change_retargets_mirror_edge(self) -> None:
        """Changing a node's sink route via upsert_node must retarget the
        visual edge that expressed the old route."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error"), state, catalog)
        assert r1.success is True

        args = _transform_args("t1", on_success="mid")
        args["on_error"] = "err_b"
        r2 = execute_tool("upsert_node", args, r1.updated_state, catalog)
        assert r2.success is True
        assert _node(r2.updated_state, "t1").on_error == "err_b"
        assert _edge(r2.updated_state, "e1").to_node == "err_b"

    def test_upsert_node_clearing_sink_route_removes_mirror_edge(self) -> None:
        """Reverting a node's error route to discard must drop the stale
        visual edge instead of leaving it pointing at the old sink."""
        catalog = _mock_catalog()
        state = self._base()
        r1 = execute_tool("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error"), state, catalog)
        assert r1.success is True

        args = _transform_args("t1", on_success="mid")
        args["on_error"] = "discard"
        r2 = execute_tool("upsert_node", args, r1.updated_state, catalog)
        assert r2.success is True
        assert _node(r2.updated_state, "t1").on_error == "discard"
        assert not any(edge.id == "e1" for edge in r2.updated_state.edges)


# ---------------------------------------------------------------------------
# elspeth-2c18c2127e — non-lowerable node/edge combinations are rejected
# ---------------------------------------------------------------------------


class TestEdgeLowerabilityAdmission:
    def _catalog(self) -> Any:
        return _mock_catalog()

    def _queue_state(self) -> CompositionState:
        return _state_with(
            (
                "upsert_node",
                {
                    "id": "inbound",
                    "node_type": "queue",
                    "plugin": None,
                    "input": "inbound",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                },
            ),
            ("set_output", _output_args("out_sink")),
        )

    def test_queue_on_success_edge_to_sink_rejected(self) -> None:
        state = self._queue_state()
        result = execute_tool("upsert_edge", _edge_args("e1", "inbound", "out_sink", "on_success"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def test_queue_on_error_edge_to_sink_rejected(self) -> None:
        state = self._queue_state()
        result = execute_tool("upsert_edge", _edge_args("e1", "inbound", "out_sink", "on_error"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def test_coalesce_on_error_edge_to_sink_rejected(self) -> None:
        """CoalesceSettings has no on_error field: the route cannot lower, so
        the edge must be rejected instead of silently diverging from YAML."""
        state = _state_with(
            (
                "upsert_node",
                {
                    "id": "joined",
                    "node_type": "coalesce",
                    "plugin": None,
                    "input": "branch_a_done",
                    "branches": {"a": "branch_a_done", "b": "branch_b_done"},
                    "policy": "require_all",
                    "merge": "union",
                },
            ),
            ("set_output", _output_args("err_sink")),
        )
        result = execute_tool("upsert_edge", _edge_args("e1", "joined", "err_sink", "on_error"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def test_transform_on_error_edge_to_node_rejected(self) -> None:
        """Transform on_error must lower to a sink; an on_error edge into a
        processing node has no runtime meaning."""
        state = _state_with(
            ("upsert_node", _transform_args("t1", on_success="mid")),
            ("upsert_node", _transform_args("t2", input_="mid", on_success="out")),
        )
        result = execute_tool("upsert_edge", _edge_args("e1", "t1", "t2", "on_error"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def test_route_edge_from_non_gate_to_node_rejected(self) -> None:
        state = _state_with(
            ("upsert_node", _transform_args("t1", on_success="mid")),
            ("upsert_node", _transform_args("t2", input_="mid", on_success="out")),
        )
        result = execute_tool("upsert_edge", _edge_args("e1", "t1", "t2", "route_true"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def test_fork_edge_from_non_gate_to_node_rejected(self) -> None:
        state = _state_with(
            ("upsert_node", _transform_args("t1", on_success="mid")),
            ("upsert_node", _transform_args("t2", input_="mid", on_success="out")),
        )
        result = execute_tool("upsert_edge", _edge_args("e1", "t1", "t2", "fork"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def _gate_state(self) -> CompositionState:
        return _state_with(
            (
                "upsert_node",
                {
                    "id": "g1",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "in",
                    "on_success": None,
                    "options": {},
                    "condition": "row['x'] > 0",
                    "routes": {"true": "keep", "false": "discard"},
                },
            ),
            ("upsert_node", _transform_args("t2", input_="keep", on_success="out")),
            ("set_output", _output_args("out_sink")),
        )

    def test_gate_on_success_edge_to_sink_rejected(self) -> None:
        """A gate has no on_success scalar: a sink-targeting on_success edge
        cannot lower. Sink routing from gates uses route/fork edges."""
        state = self._gate_state()
        result = execute_tool("upsert_edge", _edge_args("e1", "g1", "out_sink", "on_success"), state, self._catalog())
        assert result.success is False
        assert result.updated_state is state

    def test_gate_on_success_edge_to_node_stays_advisory(self) -> None:
        """Labeled routes into processing nodes have no dedicated EdgeType;
        the advisory on_success picture must stay drawable."""
        state = self._gate_state()
        result = execute_tool("upsert_edge", _edge_args("e1", "g1", "t2", "on_success"), state, self._catalog())
        assert result.success is True
        gate = _node(result.updated_state, "g1")
        assert gate.on_success is None

    def test_source_non_on_success_edge_rejected_for_node_targets(self) -> None:
        state = _state_with(
            (
                "set_source",
                {
                    "plugin": "csv",
                    "on_success": "main",
                    "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
            ),
            ("upsert_node", _transform_args("t1", input_="main", on_success="out")),
        )
        result = execute_tool("upsert_edge", _edge_args("e1", "source", "t1", "route_true"), state, self._catalog())
        assert result.success is False


# ---------------------------------------------------------------------------
# elspeth-eb4127fb49 — Stage-1 enforces the same contract on raw states
# ---------------------------------------------------------------------------


class TestStageOneEdgeRouteConsistency:
    def test_sink_edge_scalar_mismatch_is_error(self) -> None:
        """An edge claiming a sink route the scalar does not carry is a lie in
        the operator-facing graph; Stage 1 must reject it on every entry path."""
        state = _raw_state(
            nodes=(_node_spec("t1", "transform", plugin="passthrough", input_="in", on_success="out", on_error="err_b"),),
            edges=(EdgeSpec(id="e1", from_node="t1", to_node="err_a", edge_type="on_error", label=None),),
            outputs=(_sink("err_a"), _sink("err_b"), _sink("out")),
        )
        assert _errors_for(state, "edge_route_mismatch")

    def test_duplicate_slot_edges_are_error(self) -> None:
        state = _raw_state(
            nodes=(_node_spec("t1", "transform", plugin="passthrough", input_="in", on_success="out", on_error="err_a"),),
            edges=(
                EdgeSpec(id="e1", from_node="t1", to_node="err_a", edge_type="on_error", label=None),
                EdgeSpec(id="e2", from_node="t1", to_node="err_a", edge_type="on_error", label=None),
            ),
            outputs=(_sink("err_a"), _sink("out")),
        )
        assert _errors_for(state, "edge_route_conflict")

    def test_non_lowerable_edge_is_error(self) -> None:
        state = _raw_state(
            nodes=(
                _node_spec(
                    "joined",
                    "coalesce",
                    input_="branch_a_done",
                    branches={"a": "branch_a_done", "b": "branch_b_done"},
                ),
            ),
            edges=(EdgeSpec(id="e1", from_node="joined", to_node="err_a", edge_type="on_error", label=None),),
            outputs=(_sink("err_a"),),
        )
        assert _errors_for(state, "edge_not_lowerable")


class TestStageOneGateStructuralParity:
    """Mirror GateSettings structural rules the runtime enforces at build."""

    def _gate_state(
        self,
        *,
        condition: str = "row['category']",
        routes: dict[str, str] | None,
        fork_to: tuple[str, ...] | None = None,
        outputs: tuple[OutputSpec, ...] = (),
    ) -> CompositionState:
        return _raw_state(
            nodes=(
                _node_spec(
                    "g1",
                    "gate",
                    input_="in",
                    condition=condition,
                    routes=routes,
                    fork_to=fork_to,
                ),
            ),
            outputs=outputs,
        )

    def test_empty_routes_mapping_is_error(self) -> None:
        """GateSettings requires at least one route; routes={} must not pass
        Stage 1 when the runtime deterministically rejects it."""
        state = self._gate_state(routes={})
        summary = state.validate()
        assert any(entry.error_code in ("gate_missing_routes", "gate_routes_empty") for entry in summary.errors), [
            e.error_code for e in summary.errors
        ]

    def test_fork_route_without_fork_to_is_error(self) -> None:
        state = self._gate_state(routes={"all": "fork"})
        assert _errors_for(state, "gate_fork_route_without_fork_to")

    def test_fork_to_without_fork_route_is_error(self) -> None:
        state = self._gate_state(
            routes={"all": "somewhere"},
            fork_to=("path_a", "path_b"),
            outputs=(_sink("somewhere"),),
        )
        assert _errors_for(state, "gate_fork_to_without_fork_route")

    def test_ghost_route_destination_is_error(self) -> None:
        """A gate route naming neither a sink nor a consumed connection nor a
        reserved keyword dead-ends every row it receives."""
        state = self._gate_state(routes={"match": "ghost"}, outputs=(_sink("real_sink"),))
        assert _errors_for(state, "gate_route_target_unknown")


class TestStageOneRuntimeFatalPromotions:
    """Deterministic runtime build failures must be Stage-1 errors, not
    warnings an entry path is free to ignore."""

    def test_aggregation_on_error_unknown_sink_is_error(self) -> None:
        state = _raw_state(
            nodes=(
                NodeSpec(
                    id="agg1",
                    node_type="aggregation",
                    plugin="batch_stats",
                    input="in",
                    on_success="out",
                    on_error="ghost",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(_sink("out_sink"),),
        )
        assert _errors_for(state, "aggregation_on_error_unknown_sink")

    def test_failsink_nonexistent_target_is_error(self) -> None:
        state = _raw_state(
            outputs=(OutputSpec(name="main", plugin="csv", options={}, on_write_failure="ghost"),),
            source_on_success="main",
        )
        assert _errors_for(state, "failsink_unknown_output")

    def test_failsink_self_reference_is_error(self) -> None:
        state = _raw_state(
            outputs=(OutputSpec(name="main", plugin="csv", options={}, on_write_failure="main"),),
            source_on_success="main",
        )
        assert _errors_for(state, "failsink_self_reference")

    def test_failsink_chain_is_error(self) -> None:
        state = _raw_state(
            outputs=(
                OutputSpec(name="main", plugin="csv", options={}, on_write_failure="backup"),
                OutputSpec(name="backup", plugin="csv", options={}, on_write_failure="tertiary"),
                OutputSpec(name="tertiary", plugin="csv", options={}, on_write_failure="discard"),
            ),
            source_on_success="main",
        )
        assert _errors_for(state, "failsink_chain")

    def test_failsink_ineligible_plugin_is_error(self) -> None:
        state = _raw_state(
            outputs=(
                OutputSpec(name="main", plugin="csv", options={}, on_write_failure="db_backup"),
                OutputSpec(name="db_backup", plugin="database", options={}, on_write_failure="discard"),
            ),
            source_on_success="main",
        )
        assert _errors_for(state, "failsink_ineligible_plugin")

    def test_lowering_matrix_is_pinned_exhaustively(self) -> None:
        """Pin the full (from kind, edge type, target kind) lowering matrix.

        One shared predicate serves upsert_edge admission and Stage-1
        validation; this table is the contract. True = lowers (or is
        advisory-legal), False = rejected.
        """
        from elspeth.web.composer.state import edge_lowering_error

        matrix: dict[tuple[str, str, str], bool] = {
            # (from_kind, edge_type, to_kind): allowed
            ("source", "on_success", "node"): True,
            ("source", "on_success", "output"): True,
            ("source", "on_error", "node"): False,
            ("source", "on_error", "output"): False,
            ("source", "route_true", "output"): False,
            ("source", "fork", "output"): False,
            ("transform", "on_success", "node"): True,
            ("transform", "on_success", "output"): True,
            ("transform", "on_error", "node"): False,
            ("transform", "on_error", "output"): True,
            ("transform", "route_true", "node"): False,
            ("transform", "route_true", "output"): False,
            ("transform", "route_false", "output"): False,
            ("transform", "fork", "node"): False,
            ("transform", "fork", "output"): False,
            ("aggregation", "on_success", "output"): True,
            ("aggregation", "on_error", "output"): True,
            ("aggregation", "on_error", "node"): False,
            ("aggregation", "route_true", "output"): False,
            ("aggregation", "fork", "output"): False,
            ("collector", "on_success", "node"): True,
            ("collector", "on_success", "output"): True,
            ("collector", "on_error", "output"): True,
            ("collector", "on_error", "node"): False,
            ("collector", "route_true", "output"): False,
            ("collector", "fork", "output"): False,
            ("gate", "on_success", "node"): True,  # advisory labeled-route picture
            ("gate", "on_success", "output"): False,
            ("gate", "on_error", "node"): False,  # node-level policy, never an edge
            ("gate", "on_error", "output"): False,
            ("gate", "route_true", "node"): True,
            ("gate", "route_true", "output"): True,
            ("gate", "route_false", "node"): True,
            ("gate", "route_false", "output"): True,
            ("gate", "fork", "node"): True,
            ("gate", "fork", "output"): True,
            ("coalesce", "on_success", "node"): True,
            ("coalesce", "on_success", "output"): True,
            ("coalesce", "on_error", "node"): False,
            ("coalesce", "on_error", "output"): False,
            ("coalesce", "route_true", "output"): False,
            ("coalesce", "fork", "output"): False,
            ("row_union", "on_success", "node"): True,
            ("row_union", "on_success", "output"): False,
            ("row_union", "on_error", "output"): False,
            ("row_union", "route_true", "output"): False,
            ("row_union", "fork", "output"): False,
            ("queue", "on_success", "node"): True,
            ("queue", "on_success", "output"): False,
            ("queue", "on_error", "node"): False,
            ("queue", "on_error", "output"): False,
            ("queue", "route_true", "output"): False,
            ("queue", "fork", "output"): False,
        }
        for (from_kind, edge_type, to_kind), allowed in matrix.items():
            edge = EdgeSpec(id="e", from_node="f", to_node="t", edge_type=edge_type, label=None)  # type: ignore[arg-type]
            verdict = edge_lowering_error(
                edge,
                from_kind=from_kind,  # type: ignore[arg-type]
                to_kind=("output" if to_kind == "output" else "transform"),
            )
            assert (verdict is None) == allowed, f"{from_kind} --{edge_type}--> {to_kind}: {verdict}"

    def test_mutation_orders_never_leave_a_lying_sink_edge(self) -> None:
        """Every interleaving of edge/node route mutations leaves the sink
        edges in agreement with the scalars (the elspeth-372e18e365 property).
        """
        from itertools import permutations

        catalog = _mock_catalog()
        base = _state_with(
            ("upsert_node", _transform_args("t1", on_success="mid")),
            ("upsert_node", _transform_args("t2", input_="mid", on_success="out")),
            ("set_output", _output_args("err_a")),
            ("set_output", _output_args("err_b")),
        )
        mutations: list[tuple[str, dict[str, Any]]] = [
            ("upsert_edge", _edge_args("e1", "t1", "err_a", "on_error")),
            ("upsert_edge", _edge_args("e1", "t1", "err_b", "on_error")),
            ("upsert_node", {**_transform_args("t1", on_success="mid"), "on_error": "err_a"}),
            ("remove_edge", {"id": "e1"}),
        ]
        for order in permutations(range(len(mutations))):
            state = base
            for index in order:
                tool_name, args = mutations[index]
                result = execute_tool(tool_name, args, state, catalog)
                if not result.success:
                    continue  # rejected mutations must not mutate state
                state = result.updated_state
                mismatches = [
                    entry for entry in state.validate().errors if entry.error_code in ("edge_route_mismatch", "edge_route_conflict")
                ]
                assert not mismatches, f"order {order} step {index}: {[m.message for m in mismatches]}"

    def test_quarantine_nonexistent_target_is_error(self) -> None:
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="main",
                options={"schema": {"mode": "observed"}},
                on_validation_failure="ghost",
            ),
            nodes=(),
            edges=(),
            outputs=(OutputSpec(name="main", plugin="csv", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )
        assert _errors_for(state, "quarantine_unknown_output")
