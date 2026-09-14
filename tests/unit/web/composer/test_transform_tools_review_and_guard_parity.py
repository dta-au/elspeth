"""Review custody and guard parity for the transform-graph composer tools.

Three defects lived in ``tools/transforms.py``:

* ``remove_node``, ``remove_edge`` and ``upsert_edge`` rewrite exactly the
  graph facts a resolved ``pipeline_decision`` review hash binds (upstream
  producers, gate routes) but never ran ``reconcile_authoritative_reviews``.
  The review stayed ``resolved`` against a drifted anchor, no pending site was
  enumerated, and Execute raised a bare drift ``ValueError`` (finding #13).
* ``patch_node_options`` skipped the batch-aware placement and ADR-013
  ``required_input_fields`` guards every other mutation boundary runs, so the
  plugin's own fail-closed ``FrameworkBugError`` escaped the tool and ended the
  compose turn as a 500 (finding #7).
* ``splice_transform`` replaced the context-aware validation errors it had
  just computed with one generic sentence, so no surface could see what to
  repair (finding #8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    _batch_aware_placement_error,
    _batch_aware_required_input_fields_error,
)
from elspeth.web.composer.tools import ToolResult, transforms
from elspeth.web.composer.tools._common import REVIEW_RECONCILIATION_FAILURE_PREFIX, ToolContext
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import (
    GATE_CONDITION_AUTHORED_USER_TERM,
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SHIELD_USER_TERM,
    InterpretationReviewPending,
    materialize_state_for_execution,
    pending_execution_interpretation_sites,
    pipeline_decision_artifact_hash,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer import test_splice_transform_tool as splice_fixtures
from tests.unit.web.test_interpretation_state import _gate, _llm, _passthrough, _state, _web_scrape

_SHIELD_DRAFT = "Public web content from web_scrape flows directly into the LLM. Recommend inserting azure_prompt_shield."
_GATE_DRAFT = "I chose the 80 cutoff; you did not state one."


def _context() -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
    )


def _replace_node_options(state: CompositionState, node_id: str, options: dict[str, Any]) -> CompositionState:
    return replace(state, nodes=tuple(replace(node, options=options) if node.id == node_id else node for node in state.nodes))


def _node(state: CompositionState, node_id: str) -> NodeSpec:
    return next(node for node in state.nodes if node.id == node_id)


# ── #13: graph mutations reconcile resolved reviews ─────────────────────────


def _reviewed_shield_state() -> CompositionState:
    """web_scrape -> mid -> llm with a resolved prompt-shield pipeline decision.

    The shield decision's artifact hash binds every predecessor on the LLM's
    upstream path, so removing ``mid`` drifts it.
    """
    base = _state(
        (
            _web_scrape("scrape", input_stream="rows", on_success="scraped"),
            _passthrough("mid", input_stream="scraped", on_success="inbound"),
            _llm(),
        )
    )
    llm = _node(base, "classify")
    artifact_hash = pipeline_decision_artifact_hash(llm, base.nodes, user_term=PROMPT_SHIELD_USER_TERM)
    options = dict(llm.options)
    prompt_template = options["prompt_template"]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": "prompt_injection_shield_review:classify",
            "kind": "pipeline_decision",
            "user_term": PROMPT_SHIELD_USER_TERM,
            "status": "resolved",
            "draft": _SHIELD_DRAFT,
            "event_id": "evt-shield",
            "accepted_value": _SHIELD_DRAFT,
            "accepted_artifact_hash": artifact_hash,
            "resolved_prompt_template_hash": None,
        },
        {
            "id": "llm_prompt_template:classify",
            "kind": "llm_prompt_template",
            "user_term": "llm_prompt_template:classify",
            "status": "resolved",
            "draft": prompt_template,
            "event_id": "evt-prompt",
            "accepted_value": prompt_template,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": stable_hash(prompt_template),
        },
    ]
    return _replace_node_options(base, "classify", options)


def _gate_outputs() -> tuple[OutputSpec, ...]:
    return tuple(
        OutputSpec(name=name, plugin="json", options={}, on_write_failure="discard") for name in ("accepted", "rejected", "rejected_rows")
    )


def _with_resolved_gate_review(state: CompositionState, gate_id: str) -> CompositionState:
    gate = _node(state, gate_id)
    artifact_hash = pipeline_decision_artifact_hash(gate, state.nodes, user_term=GATE_CONDITION_AUTHORED_USER_TERM)
    options = dict(gate.options)
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": f"gate_condition_authored:{gate_id}",
            "kind": "pipeline_decision",
            "user_term": GATE_CONDITION_AUTHORED_USER_TERM,
            "status": "resolved",
            "draft": _GATE_DRAFT,
            "event_id": "evt-gate",
            "accepted_value": _GATE_DRAFT,
            "accepted_artifact_hash": artifact_hash,
            "resolved_prompt_template_hash": None,
        }
    ]
    return _replace_node_options(state, gate_id, options)


def _reviewed_gate_state() -> CompositionState:
    gate = _gate()
    state = CompositionState(source=None, nodes=(gate,), edges=(), outputs=_gate_outputs(), metadata=PipelineMetadata(), version=1)
    return _with_resolved_gate_review(state, gate.id)


def _reviewed_gate_state_with_route_edge() -> CompositionState:
    gate = _gate()
    unreviewed = CompositionState(source=None, nodes=(gate,), edges=(), outputs=_gate_outputs(), metadata=PipelineMetadata(), version=1)
    edged = transforms._execute_upsert_edge(
        {"id": "e_true", "from_node": gate.id, "to_node": "accepted", "edge_type": "route_true"},
        unreviewed,
        _context(),
    )
    assert edged.success is True
    return _with_resolved_gate_review(edged.updated_state, gate.id)


_GraphCase = tuple[
    str, Callable[[], CompositionState], Callable[[dict[str, Any], CompositionState, ToolContext], ToolResult], dict[str, Any], str
]

_GRAPH_MUTATION_CASES: tuple[_GraphCase, ...] = (
    ("remove_node", _reviewed_shield_state, transforms._execute_remove_node, {"id": "mid"}, "classify"),
    ("remove_edge", _reviewed_gate_state_with_route_edge, transforms._execute_remove_edge, {"id": "e_true"}, "score_gate"),
    (
        "upsert_edge",
        _reviewed_gate_state,
        transforms._execute_upsert_edge,
        {"id": "e_true", "from_node": "score_gate", "to_node": "rejected_rows", "edge_type": "route_true"},
        "score_gate",
    ),
)


@pytest.mark.parametrize(
    ("tool_name", "build_state", "handler", "arguments", "reviewed_node"),
    _GRAPH_MUTATION_CASES,
    ids=[case[0] for case in _GRAPH_MUTATION_CASES],
)
def test_graph_mutation_reopens_a_drifted_resolved_pipeline_decision(
    tool_name: str,
    build_state: Callable[[], CompositionState],
    handler: Callable[[dict[str, Any], CompositionState, ToolContext], ToolResult],
    arguments: dict[str, Any],
    reviewed_node: str,
) -> None:
    reviewed = build_state()
    # Control: the reviewed baseline is executable and owes no review.
    assert pending_execution_interpretation_sites(reviewed) == ()
    assert isinstance(materialize_state_for_execution(reviewed), CompositionState)

    result = handler(arguments, reviewed, _context())

    assert result.success is True, tool_name
    sites = pending_execution_interpretation_sites(result.updated_state)
    assert [(site.component_id, site.kind.value) for site in sites] == [(reviewed_node, "pipeline_decision")]
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


@pytest.mark.parametrize(
    ("tool_name", "build_state", "handler", "arguments", "reviewed_node"),
    _GRAPH_MUTATION_CASES,
    ids=[case[0] for case in _GRAPH_MUTATION_CASES],
)
def test_graph_mutation_maps_a_reconciliation_failure_to_a_repairable_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    build_state: Callable[[], CompositionState],
    handler: Callable[[dict[str, Any], CompositionState, ToolContext], ToolResult],
    arguments: dict[str, Any],
    reviewed_node: str,
) -> None:
    del reviewed_node
    reviewed = build_state()

    def _raise(previous: CompositionState, proposed: CompositionState) -> CompositionState:
        del previous, proposed
        raise ValueError("duplicate review identity 'gate_condition_authored:x'")

    monkeypatch.setattr(transforms, "reconcile_authoritative_reviews", _raise)

    result = handler(arguments, reviewed, _context())

    assert result.success is False, tool_name
    assert result.updated_state is reviewed
    leading = result.validation.errors[0]
    assert leading.component == "rejected_mutation"
    assert leading.error_code == "review_reconciliation_failed"
    assert leading.message.startswith(f"{REVIEW_RECONCILIATION_FAILURE_PREFIX}: duplicate review identity")


# ── #7: batch-aware guards run identically on every mutation boundary ───────

_CSV_SOURCE = SourceSpec(
    plugin="csv",
    on_success="in",
    options={"path": "rows.csv", "schema": {"mode": "observed"}},
    on_validation_failure="discard",
)
_JSON_OUTPUT = OutputSpec(
    name="out",
    plugin="json",
    options={"path": "out.jsonl", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
    on_write_failure="discard",
)
_BATCH_OPTIONS: dict[str, Any] = {"schema": {"mode": "observed"}, "value_field": "x"}


def _spec(node_id: str, input_name: str, output_name: str, **overrides: Any) -> NodeSpec:
    fields: dict[str, Any] = {
        "id": node_id,
        "node_type": "transform",
        "plugin": "passthrough",
        "input": input_name,
        "on_success": output_name,
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }
    fields.update(overrides)
    return NodeSpec(**fields)


def _linear_state() -> CompositionState:
    return CompositionState(
        source=_CSV_SOURCE,
        nodes=(_spec("t1", "in", "mid"), _spec("t2", "mid", "out")),
        edges=(
            EdgeSpec(id="e_src_t1", from_node="source", to_node="t1", edge_type="on_success", label=None),
            EdgeSpec(id="e_t1_t2", from_node="t1", to_node="t2", edge_type="on_success", label=None),
            EdgeSpec(id="e_t2_out", from_node="t2", to_node="out", edge_type="on_success", label=None),
        ),
        outputs=(_JSON_OUTPUT,),
        metadata=PipelineMetadata(name="batch-parity"),
        version=3,
    )


def _state_holding(batch_node: NodeSpec) -> CompositionState:
    return CompositionState(
        source=_CSV_SOURCE,
        nodes=(_spec("t1", "in", "mid"), batch_node),
        edges=(),
        outputs=(_JSON_OUTPUT,),
        metadata=PipelineMetadata(name="batch-parity"),
        version=3,
    )


def _set_pipeline_arguments(batch_node: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "in",
            "options": {"path": "rows.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "in",
                "on_success": "mid",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            batch_node,
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "out",
                "plugin": "json",
                "options": dict(_JSON_OUTPUT.options),
                "on_write_failure": "discard",
            }
        ],
    }


def _aggregation_with_required_fields() -> dict[str, Any]:
    return {
        "id": "agg",
        "node_type": "aggregation",
        "plugin": "batch_stats",
        "input": "mid",
        "on_success": "out",
        "on_error": "discard",
        "options": {**_BATCH_OPTIONS, "required_input_fields": ["x"]},
        "trigger": {"count": 10},
        "output_mode": "transform",
    }


def _batch_plugin_as_transform() -> dict[str, Any]:
    return {
        "id": "agg",
        "node_type": "transform",
        "plugin": "batch_stats",
        "input": "mid",
        "on_success": "out",
        "on_error": "discard",
        "options": dict(_BATCH_OPTIONS),
    }


def _assert_guard_rejection(result: ToolResult, state: CompositionState, guard_message: str, *, path: str) -> None:
    assert result.success is False, path
    assert result.updated_state is state, path
    leading = result.validation.errors[0]
    assert leading.component == "rejected_mutation", path
    assert leading.error_code is None, path
    # set_pipeline attributes the entry with a ``Node 'agg': `` prefix; the
    # guard text itself must be the whole remainder on every path.
    assert leading.message.endswith(guard_message), (path, leading.message)


def _dispatch(tool_name: str, arguments: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
    return execute_tool(tool_name, arguments, state, context.catalog, plugin_snapshot=context.plugin_snapshot)


def test_required_input_fields_on_a_batch_aware_aggregation_is_rejected_the_same_way_on_every_path() -> None:
    """Input A: ADR-013 declared input fields on a batch-aware aggregation.

    ``splice_transform`` cannot author an aggregation (it inserts transforms
    only), so it is covered by input B, where it meets the same guard family.
    """
    context = _context()
    guard_message = _batch_aware_required_input_fields_error("agg", "batch_stats", {"required_input_fields": ["x"]})
    assert guard_message is not None
    node_arguments = _aggregation_with_required_fields()
    existing = _state_holding(
        _spec(
            "agg",
            "mid",
            "out",
            node_type="aggregation",
            plugin="batch_stats",
            options=dict(_BATCH_OPTIONS),
            trigger={"count": 10},
            output_mode="transform",
        )
    )

    linear = _linear_state()

    upsert = _dispatch("upsert_node", node_arguments, existing, context)
    patch = _dispatch("patch_node_options", {"node_id": "agg", "patch": {"required_input_fields": ["x"]}}, existing, context)
    set_pipeline = _dispatch("set_pipeline", _set_pipeline_arguments(node_arguments), linear, context)

    _assert_guard_rejection(upsert, existing, guard_message, path="upsert_node")
    _assert_guard_rejection(patch, existing, guard_message, path="patch_node_options")
    _assert_guard_rejection(set_pipeline, linear, guard_message, path="set_pipeline")


def test_batch_aware_plugin_placed_as_a_transform_is_rejected_the_same_way_on_every_path() -> None:
    """Input B: a batch-aware plugin in row-mode placement, on all four boundaries."""
    context = _context()
    guard_message = _batch_aware_placement_error("agg", "transform", "batch_stats", None)
    assert guard_message is not None
    linear = _linear_state()
    misplaced = _state_holding(_spec("agg", "mid", "out", plugin="batch_stats", options=dict(_BATCH_OPTIONS)))

    upsert = _dispatch("upsert_node", _batch_plugin_as_transform(), linear, context)
    patch = _dispatch("patch_node_options", {"node_id": "agg", "patch": {"value_field": "y"}}, misplaced, context)
    splice = _dispatch(
        "splice_transform",
        {
            "predecessor_id": "t1",
            "successor_id": "t2",
            "node": {"id": "agg", "plugin": "batch_stats", "options": dict(_BATCH_OPTIONS), "on_error": "discard"},
        },
        linear,
        context,
    )
    set_pipeline = _dispatch("set_pipeline", _set_pipeline_arguments(_batch_plugin_as_transform()), linear, context)

    _assert_guard_rejection(upsert, linear, guard_message, path="upsert_node")
    _assert_guard_rejection(patch, misplaced, guard_message, path="patch_node_options")
    _assert_guard_rejection(splice, linear, guard_message, path="splice_transform")
    _assert_guard_rejection(set_pipeline, linear, guard_message, path="set_pipeline")


def test_patch_on_a_batch_aware_aggregation_still_accepts_unrelated_options() -> None:
    """Negative control: the new guards refuse only what they name."""
    context = _context()
    existing = _state_holding(
        _spec(
            "agg",
            "mid",
            "out",
            node_type="aggregation",
            plugin="batch_stats",
            options=dict(_BATCH_OPTIONS),
            trigger={"count": 10},
            output_mode="transform",
        )
    )

    result = _dispatch("patch_node_options", {"node_id": "agg", "patch": {"value_field": "y"}}, existing, context)

    assert result.success is True
    assert _node(result.updated_state, "agg").options["value_field"] == "y"


# ── #8: splice forwards the candidate's own validation errors ───────────────


def _ghost_field_splice_arguments() -> dict[str, object]:
    return splice_fixtures._arguments(options={"schema": {"mode": "observed"}, "required_input_fields": ["ghost_field"]})


def _rejection_entries(result: ToolResult) -> list[Any]:
    return [entry for entry in result.validation.errors if entry.component == "rejected_mutation"]


def test_splice_validation_rejection_forwards_the_candidate_errors_through_dispatch() -> None:
    context = splice_fixtures._context()
    state = splice_fixtures._state()
    assert context.catalog.validate_composition_state(state).validation.is_valid is True

    result = _dispatch("splice_transform", _ghost_field_splice_arguments(), state, context)

    assert result.success is False
    assert result.updated_state is state
    rejections = _rejection_entries(result)
    assert rejections[0].error_code == "splice_validation_failed"
    assert rejections[0].message == "Spliced pipeline failed context-aware validation."
    assert result.validation.errors[0] is rejections[0]
    forwarded = rejections[1:]
    contract_entries = [entry for entry in forwarded if entry.error_code == "schema_contract_violation"]
    assert len(contract_entries) == 1
    assert contract_entries[0].rejected_component == "node:inserted"
    assert "ghost_field" in contract_entries[0].message

    # The one-shot planner's feedback keeps only rejection entries; the
    # forwarded code must survive that gate, attributed to the inserted node.
    feedback = _allowlisted_candidate_feedback(result)
    projected = [(entry["component"], entry["error_code"]) for entry in feedback["validation"]["errors"]]
    assert projected[0] == ("rejected_mutation", "splice_validation_failed")
    assert ("node:inserted", "schema_contract_violation") in projected


def test_splice_validation_rejection_does_not_forward_errors_the_pre_state_already_had() -> None:
    context = splice_fixtures._context()
    base = splice_fixtures._state()
    stray = splice_fixtures._node("stray", input_name="nowhere", output_name="also_nowhere")
    state = replace(base, nodes=(*base.nodes, stray))
    pre_errors = context.catalog.validate_composition_state(state).validation.errors
    assert pre_errors, "control: the pre-state must already carry errors"
    pre_keys = {(entry.component, entry.error_code, entry.message) for entry in pre_errors}

    result = _dispatch("splice_transform", _ghost_field_splice_arguments(), state, context)

    assert result.success is False
    forwarded = _rejection_entries(result)[1:]
    assert forwarded, "the splice's own schema error must still be forwarded"
    assert all((entry.rejected_component, entry.error_code, entry.message) not in pre_keys for entry in forwarded)
    assert any(entry.error_code == "schema_contract_violation" and entry.rejected_component == "node:inserted" for entry in forwarded)
