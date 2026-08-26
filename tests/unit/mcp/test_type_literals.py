"""Regression tests for finite MCP TypedDict string fields."""

from __future__ import annotations

from typing import Literal, Required, TypeAliasType, get_args, get_origin, get_type_hints

from elspeth.contracts.enums import NodeType
from elspeth.mcp import types
from elspeth.mcp.server import _TOOLS

# Node kinds the runtime can emit that MCP deliberately does NOT describe.
#
# EMPTY, and that is a REPORT rather than a claim of completeness: `collector`
# and `row_union` are in NodeType and absent from `NodeTypeValue`, and which of
# the two resolutions applies — widen the MCP literal, or record a documented
# exclusion here — is not this file's call. It turns on whether the MCP
# analyzers actually dispatch on those kinds or fall through, which is being
# inventoried separately (elspeth-71858d1244).
#
# Whatever lands here must carry its reason. Note the set is SUBTRACTED from the
# runtime enum below rather than being a hand-listed expectation, so an entry
# that stops being true fails this test instead of silently narrowing it.
_NODE_TYPES_OUTSIDE_MCP_SCOPE: frozenset[str] = frozenset()


def _literal_values(hint: object) -> set[str]:
    if get_origin(hint) is Required:
        hint = get_args(hint)[0]
    if isinstance(hint, TypeAliasType):
        hint = hint.__value__
    assert get_origin(hint) is Literal
    return set(get_args(hint))


def test_run_record_status_uses_current_run_status_literal() -> None:
    hints = get_type_hints(types.RunRecord)

    assert _literal_values(hints["status"]) == {
        "running",
        "completed",
        "completed_with_failures",
        "failed",
        "empty",
        "interrupted",
    }


def test_operation_record_uses_operation_literals() -> None:
    hints = get_type_hints(types.OperationRecord)

    assert _literal_values(hints["operation_type"]) == {
        "source_load",
        "sink_write",
        "runtime_preflight",
    }
    assert _literal_values(hints["status"]) == {"open", "completed", "failed", "pending"}


def test_operation_type_filter_schema_matches_operation_literals() -> None:
    """MCP schema must accept every operation type the runtime writes."""
    hints = get_type_hints(types.OperationRecord)
    operation_type_values = _literal_values(hints["operation_type"])
    schema_values = set(_TOOLS["list_operations"].schema_properties["operation_type"]["enum"])

    assert schema_values == operation_type_values


def test_node_state_and_dag_literals_match_current_contracts() -> None:
    node_state_hints = get_type_hints(types.NodeStateRecord)
    dag_edge_hints = get_type_hints(types.DAGEdge)

    assert _literal_values(node_state_hints["status"]) == {"open", "pending", "completed", "failed"}
    assert _literal_values(dag_edge_hints["mode"]) == {"move", "copy", "divert"}
    assert _literal_values(dag_edge_hints["flow_type"]) == {"normal", "divert"}


def test_dag_node_type_covers_the_runtime_node_vocabulary() -> None:
    """MCP must describe every node kind the runtime can put in a graph.

    DERIVED, not restated. This assertion previously hand-listed seven members
    against a hand-written ``NodeTypeValue`` literal — both sides written by the
    same hand, so the test was green by construction and could not report the
    drift it existed to catch (elspeth-71858d1244). ``NodeType`` is the runtime
    authority: it is the enum the DAG builder stamps on every node, the value
    persisted to ``nodes.node_type``, and the enum the MCP analyzers themselves
    compare against (``reports.py`` tests ``n.node_type == NodeType.SINK``). So
    the expectation is computed from it.

    The sibling ``test_operation_type_filter_schema_matches_operation_literals``
    already works this way, cross-checking two real authorities; this is the one
    assertion in the file that restated instead.

    A member legitimately outside MCP's scope belongs in
    ``_NODE_TYPES_OUTSIDE_MCP_SCOPE`` with a recorded reason — and note that set
    is SUBTRACTED from the runtime enum rather than listed as an expected
    literal, so an excluded kind that MCP later starts declaring fails here too.
    """
    dag_node_hints = get_type_hints(types.DAGNode)

    runtime_vocabulary = {member.value for member in NodeType}
    assert runtime_vocabulary, "NodeType is empty — the derivation below would be vacuous."
    stale_exclusions = _NODE_TYPES_OUTSIDE_MCP_SCOPE - runtime_vocabulary
    assert not stale_exclusions, (
        f"_NODE_TYPES_OUTSIDE_MCP_SCOPE names kinds that are not in NodeType at all: "
        f"{sorted(stale_exclusions)}. An exclusion for a kind the runtime never emits is stale "
        f"and hides nothing."
    )

    assert _literal_values(dag_node_hints["node_type"]) == runtime_vocabulary - _NODE_TYPES_OUTSIDE_MCP_SCOPE


def test_diagnostic_and_contract_literals_are_finite() -> None:
    diagnostic_hints = get_type_hints(types.DiagnosticProblem)
    run_contract_hints = get_type_hints(types.RunContractReport)
    field_explanation_hints = get_type_hints(types.FieldExplanation)
    contract_hints = get_type_hints(types.ContractViolationRecord)

    assert _literal_values(diagnostic_hints["severity"]) == {"CRITICAL", "WARNING", "INFO"}
    assert _literal_values(run_contract_hints["mode"]) == {"FIXED", "FLEXIBLE", "OBSERVED"}
    assert _literal_values(field_explanation_hints["contract_mode"]) == {"FIXED", "FLEXIBLE", "OBSERVED"}
    assert _literal_values(contract_hints["schema_mode"]) == {"fixed", "flexible", "observed", "parse"}
    assert _literal_values(contract_hints["violation_type"]) == {
        "type_mismatch",
        "missing_field",
        "extra_field",
    }
