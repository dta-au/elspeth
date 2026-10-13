"""Editable recovery eligibility is independent of execution readiness."""

from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.web.composer.graph_recovery import GraphRecoveryIntent, qualify_editable_checkpoint
from elspeth.web.composer.state import CompositionState, EdgeSpec, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec


def _empty_state() -> CompositionState:
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _draft_node(**overrides: Any) -> NodeSpec:
    return NodeSpec.from_dict(
        {
            "id": "draft",
            "node_type": "transform",
            "plugin": "llm",
            "input": "unfinished_input",
            "on_success": None,
            "on_error": None,
            "options": {},
            **overrides,
        }
    )


def test_empty_authoring_seed_is_eligible_without_validation() -> None:
    with patch.object(CompositionState, "validate", autospec=True, side_effect=AssertionError("must not run validation")):
        result = qualify_editable_checkpoint(_empty_state())
    assert result.eligible
    assert result.reason_codes == ()


def test_missing_resources_credentials_reviews_and_topology_do_not_block_recovery() -> None:
    state = replace(
        _empty_state(),
        sources={
            "input": SourceSpec(
                plugin="csv",
                on_success="not_authored_yet",
                options={"path": "/unavailable-resource/input.csv"},
                on_validation_failure="discard",
            )
        },
        nodes=(
            _draft_node(
                options={
                    "api_key": {"secret_ref": "UNAVAILABLE_CREDENTIAL"},
                    "interpretation_requirements": [{"status": "pending"}],
                }
            ),
        ),
    )
    with (
        patch.object(CompositionState, "validate", autospec=True, side_effect=AssertionError("must not run validation")),
        patch("elspeth.web.composer.state.ValidationProbeCache", autospec=True, side_effect=AssertionError("no plugin probes")),
    ):
        assert qualify_editable_checkpoint(state).eligible


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (replace(_empty_state(), nodes=(_draft_node(), _draft_node())), "duplicate_node_id"),
        (
            replace(
                _empty_state(),
                sources={"draft": SourceSpec("csv", "missing", {}, "discard")},
                nodes=(_draft_node(),),
            ),
            "graph_namespace_collision",
        ),
        (
            replace(_empty_state(), outputs=(OutputSpec("out", "csv", {}, "discard"), OutputSpec("out", "csv", {}, "discard"))),
            "duplicate_output_name",
        ),
        (replace(_empty_state(), nodes=(_draft_node(id="source"),)), "reserved_node_id"),
        (replace(_empty_state(), nodes=(_draft_node(id="bad node"),)), "node_id_invalid"),
        (replace(_empty_state(), nodes=(_draft_node(node_type="not_a_node"),)), "checkpoint_spec_malformed"),
        (replace(_empty_state(), nodes=(_draft_node(node_type="gate", plugin="csv"),)), "structural_node_plugin_forbidden"),
        (replace(_empty_state(), nodes=(_draft_node(node_type="queue", plugin=None),)), "queue_shape_invalid"),
        (replace(_empty_state(), nodes=(_draft_node(input=47),)), "checkpoint_spec_malformed"),
        (replace(_empty_state(), nodes=(_draft_node(expected_output_count=True),)), "checkpoint_not_representable"),
        (replace(_empty_state(), edges=(EdgeSpec("e", "missing", "gone", "on_success", None),)), "edge_unknown_node"),
    ],
)
def test_malformed_identity_or_spec_is_not_a_recovery_checkpoint(state: CompositionState, reason: str) -> None:
    result = qualify_editable_checkpoint(state)
    assert not result.eligible
    assert reason in result.reason_codes


@pytest.mark.parametrize("unrepresentable", [object(), float("nan"), float("inf"), frozenset({"private-value"})])
def test_unrepresentable_candidate_returns_safe_reason_without_capture(unrepresentable: object) -> None:
    state = replace(_empty_state(), nodes=(_draft_node(options={"private-option": unrepresentable}),))
    result = qualify_editable_checkpoint(state)
    assert not result.eligible
    assert result.reason_codes == ("checkpoint_not_representable",)
    assert "private" not in repr(result)


def test_canonical_shapes_round_trip_without_changing_checkpoint() -> None:
    state = replace(
        _empty_state(),
        nodes=(
            _draft_node(node_type="coalesce", plugin=None, branches={"left": "left_input", "right": "right_input"}),
            _draft_node(id="waiting", node_type="queue", plugin=None, input="waiting"),
        ),
    )
    before = state.to_dict()
    assert qualify_editable_checkpoint(state).eligible
    assert state.to_dict() == before


def test_unsaveable_current_draft_does_not_taint_independent_checkpoint() -> None:
    invalid = replace(_empty_state(), nodes=(_draft_node(options={"bad": object()}),))
    assert not qualify_editable_checkpoint(invalid).eligible
    assert qualify_editable_checkpoint(_empty_state()).eligible


@pytest.mark.parametrize(
    "options",
    [
        {"api_key": "literal-credential-value"},
        {"connection_string": "literal-connection-material"},
    ],
)
def test_checkpoint_literal_credentials_are_refused_locally(options: dict[str, str]) -> None:
    state = replace(_empty_state(), nodes=(_draft_node(options=options),))
    result = qualify_editable_checkpoint(state)
    assert not result.eligible
    assert result.reason_codes == ("checkpoint_credential_material",)
    assert "literal" not in repr(result)


def test_checkpoint_managed_reference_needs_no_secret_resolution() -> None:
    state = replace(_empty_state(), outputs=(OutputSpec("out", "database", {"url": {"secret_ref": "MISSING_DB_URL"}}, "discard"),))
    assert qualify_editable_checkpoint(state).eligible


@pytest.mark.parametrize(
    "overrides",
    [
        {"action": "unknown"},
        {"message": "   "},
        {"message": "x" * 16001},
        {"tool_call_id": ""},
        {"tool_call_id": "x" * 1025},
        {"park_id": "unexpected"},
        {"action": "restore_parked_graph"},
        {"action": "restore_parked_graph", "park_id": " "},
        {"action": "restore_parked_graph", "park_id": "x" * 129},
    ],
)
def test_owned_recovery_intent_enforces_invariants(overrides: dict[str, Any]) -> None:
    values: dict[str, Any] = {"action": "park", "message": "Let's clarify.", "park_id": None, "tool_call_id": "call_1"}
    with pytest.raises(ValueError):
        GraphRecoveryIntent(**(values | overrides))
