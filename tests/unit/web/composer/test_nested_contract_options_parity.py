# tests/unit/web/composer/test_nested_contract_options_parity.py
"""elspeth-9d17af642e — every node kind that nests its input contract gets
the same Stage-1 verdicts for the nested shape as for the flat one.

``builder.py`` nests an aggregation's or a collector's plugin options under
``options.options``; ``contracts/schema.py`` names those kinds in
``NESTED_CONTRACT_OPTIONS_NODE_TYPES`` and resolves the alias in
``get_aggregation_contract_options``. Six Stage-1 sites in
``web/composer/state.py`` read a node's contract through that helper, and
until this ticket every one of them gated the unwrap on the
``node_type == "aggregation"`` literal — so a collector's nested contract
was read as an empty flat one: required fields dropped, locked inputs
missed, an invalid nested schema never reported, guarantees a collector
publishes downstream lost. ``_parse_node_required_fields`` had already been
widened (elspeth-c3cbf5f4cd), which is the asymmetry this file refuses.

The oracle is PARITY, not a field list: for each kind in the contract set
(derived, so a third nesting kind inherits the obligation) and for each
scenario, ``validate()`` on the nested shape must report exactly the errors
and edge contracts it reports on the flat shape. The kinds come from the
authority. Measured on release/0.8.0@fb9f56f94 before the fix:
``locked_input`` is the collector's red (site ``_consumer_locked_input_set``:
the flat closer reports ``locked_input_extras``, the nested one reports
nothing); ``consumer_required_fields`` and ``invalid_schema_syntax`` already
agree because ``_parse_node_required_fields`` reads and reports the nested
contract first — they stay as the regression net for that parser. The
type-conflict walker, ``_consumer_effective_required_set`` and
``_sweep_schema_syntax`` have no composer collector shape that reaches them
un-shadowed today (a collector's producer is its opener transform, never a
typed source), and ``_known_producer_schema_config`` still excludes
collector producers behind a ``{"transform", "aggregation"}`` guard
(elspeth-9d3a26ce67); those four are widened by derivation, not by a pin
here. Nested PRODUCER guarantees diverge for aggregations too and are a
separate defect (the vote reads raw options): elspeth-94959b2d9a.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.schema import NESTED_CONTRACT_OPTIONS_NODE_TYPES
from elspeth.web.composer import state as state_module
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationSummary,
)

_KINDS = sorted(str(kind) for kind in NESTED_CONTRACT_OPTIONS_NODE_TYPES)

# Each scenario: the closer's contract options (flat form) and, where the
# scenario needs one, a downstream consumer that reads what the closer
# publishes. Nested form = the same mapping under an ``options`` key.
_SCENARIOS: dict[str, dict[str, Any]] = {
    # consumer side: _consumer_effective_required_set / _consumer_locked_input_set
    "consumer_required_fields": {
        "closer_contract": {"value_field": "value", "schema": {"mode": "observed", "required_fields": ["value"]}},
        "downstream": None,
    },
    # syntax sweep: an invalid nested schema must be reported like a flat one
    "invalid_schema_syntax": {
        "closer_contract": {"value_field": "value", "schema": {"mode": "not-a-mode"}},
        "downstream": None,
    },
    # locked input: a fixed schema locks the accepted-input set, and the
    # opener offers fields outside it -> locked_input_extras on the closer
    "locked_input": {
        "closer_contract": {"value_field": "value", "schema": {"mode": "fixed", "fields": ["value: float"]}},
        "downstream": None,
    },
}


def _closer(kind: str, options: dict[str, Any], *, on_success: str) -> NodeSpec:
    scope = {"scope_name": "document_pages", "scope_opener": "explode", "scope_policy": "require_all"} if kind == "collector" else {}
    return NodeSpec(
        id="closer",
        node_type=kind,  # type: ignore[arg-type]
        plugin="batch_stats",
        input="pages",
        on_success=on_success,
        on_error=None,
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        **scope,  # type: ignore[arg-type]
    )


def _state(kind: str, closer_options: dict[str, Any], downstream: dict[str, Any] | None) -> CompositionState:
    """``source -> explode -> closer(kind) [-> reader] -> out``.

    ``explode`` is a real multi-row transform so the collector arm's scope
    opener is legal; the aggregation arm ignores the scope fields. The closer
    is the only node whose options differ between the flat and nested runs.
    """
    nodes: list[NodeSpec] = [
        NodeSpec(
            id="explode",
            node_type="transform",
            plugin="json_explode",
            input="rows",
            on_success="pages",
            on_error="discard",
            options={"array_field": "items", "schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        ),
        _closer(kind, closer_options, on_success="reader" if downstream is not None else "out"),
    ]
    if downstream is not None:
        nodes.append(
            NodeSpec(
                id="reader",
                node_type="transform",
                plugin="passthrough",
                input="reader",
                on_success="out",
                on_error="discard",
                options=downstream,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"schema": {"mode": "fixed", "fields": ["items: str"]}},
                on_validation_failure="discard",
            )
        },
        nodes=tuple(nodes),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="csv", options={}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )


def _verdict(summary: ValidationSummary) -> tuple[tuple[tuple[str, str | None], ...], tuple[tuple[str, str, tuple[str, ...], bool], ...]]:
    """The Stage-1 facts a contract shape can change: errors and edge contracts."""
    errors = tuple(sorted((entry.component, entry.error_code) for entry in summary.errors))
    contracts = tuple(
        sorted(
            (contract.from_id, contract.to_id, tuple(contract.consumer_requires), contract.satisfied) for contract in summary.edge_contracts
        )
    )
    return errors, contracts


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
@pytest.mark.parametrize("kind", _KINDS)
def test_nested_contract_options_validate_exactly_like_flat_ones(kind: str, scenario: str) -> None:
    spec = _SCENARIOS[scenario]
    flat = _verdict(_state(kind, spec["closer_contract"], spec["downstream"]).validate())
    nested = _verdict(_state(kind, {"options": spec["closer_contract"]}, spec["downstream"]).validate())

    assert nested == flat, (
        f"{kind} / {scenario}: the nested options.options contract was NOT read the way the flat one is "
        f"(nested={nested}, flat={flat}) — a Stage-1 site still gates get_aggregation_contract_options on the "
        f"aggregation literal instead of node_type_nests_contract_options"
    )


@pytest.mark.parametrize("kind", _KINDS)
def test_flat_scenarios_are_not_vacuous(kind: str) -> None:
    """Each scenario must be visible in the flat verdict, or parity proves nothing."""
    required = _verdict(_state(kind, _SCENARIOS["consumer_required_fields"]["closer_contract"], None).validate())
    assert any(to_id == "closer" and requires == ("value",) for _from, to_id, requires, _ok in required[1]), required
    invalid = _verdict(_state(kind, _SCENARIOS["invalid_schema_syntax"]["closer_contract"], None).validate())
    assert ("node:closer", "contract_config_invalid") in invalid[0], invalid
    locked = _verdict(_state(kind, _SCENARIOS["locked_input"]["closer_contract"], None).validate())
    assert ("node:closer", "locked_input_extras") in locked[0], locked


def _guards_of_every_unwrap_site() -> list[tuple[int, list[ast.expr]]]:
    """For each ``get_aggregation_contract_options`` call in state.py, the ``if``
    tests that decide whether it runs: every enclosing ``if`` plus the ``if``
    immediately before it in the same block (the early-return shape).
    """
    source = Path(state_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    sites: list[tuple[int, list[ast.expr]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_aggregation_contract_options"):
            continue
        guards: list[ast.expr] = []
        child: ast.AST = node
        cursor: ast.AST | None = parents.get(node)
        while cursor is not None:
            # An ``if`` whose body or else-branch holds the call decides it.
            if isinstance(cursor, ast.If) and (child in cursor.body or child in cursor.orelse):
                guards.append(cursor.test)
            # The early-return shape: ``if <guard>: return ...`` immediately
            # before the statement carrying the call, in the same block —
            # including the enclosing function's own body.
            body = getattr(cursor, "body", None)
            if isinstance(body, list) and child in body:
                index = body.index(child)
                if index > 0 and isinstance(body[index - 1], ast.If):
                    guards.append(body[index - 1].test)
            if isinstance(cursor, ast.FunctionDef):
                break
            child, cursor = cursor, parents.get(cursor)
        sites.append((node.lineno, guards))
    return sites


def test_every_unwrap_site_gates_on_the_nesting_predicate_not_the_literal() -> None:
    """Structural pin for the sites behavioural parity cannot reach today.

    Four of the six sites have no composer collector shape that exercises
    them un-shadowed (see the module docstring), so a literal gate could
    return to them without any verdict changing. This reads state.py's AST:
    each ``get_aggregation_contract_options`` call must be guarded by
    ``node_type_nests_contract_options`` and never by a comparison against
    the string ``"aggregation"`` — the derivation from the authority is the
    fix, and this is what keeps it derived.
    """
    sites = _guards_of_every_unwrap_site()
    assert len(sites) >= 6, [line for line, _ in sites]  # the six widened sites are all still there
    for line, guards in sites:
        literal_guards = [
            ast.unparse(g) for g in guards if any(isinstance(c, ast.Constant) and c.value == "aggregation" for c in ast.walk(g))
        ]
        assert not literal_guards, f"state.py:{line} gates the unwrap on the aggregation literal: {literal_guards}"
        predicate_guards = [
            g
            for g in guards
            if any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "node_type_nests_contract_options"
                for c in ast.walk(g)
            )
        ]
        assert predicate_guards, f"state.py:{line} unwraps without node_type_nests_contract_options deciding it"
