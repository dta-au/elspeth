"""Mechanical proof boundary for model-claimed deferred intent coverage."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest

from elspeth.core.canonical import stable_hash
from elspeth.web.composer.guided.deferred_intents import (
    DeferredIntentClaimError,
    evaluate_deferred_intent_coverage,
)
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.stage_subjects import (
    ComponentCountConstraint,
    DeferredConstraint,
    EdgeRouteConstraint,
    FailureRouteConstraint,
    OptionValueConstraint,
    PluginSubject,
    StableSubject,
    StatedGateRoutingConstraint,
    StatedPredicateConstraint,
    SubjectPresenceConstraint,
)
from elspeth.web.composer.guided.state_machine import DeferredStageIntent, GuidedSession
from elspeth.web.composer.state import CompositionState, EdgeSpec, NodeSpec, NodeType, OutputSpec, PipelineMetadata, SourceSpec

SOURCE_ID = "00000000-0000-4000-8000-000000000301"
OUTPUT_ID = "00000000-0000-4000-8000-000000000302"
SECOND_OUTPUT_ID = "00000000-0000-4000-8000-000000000315"
INTENT_A = "00000000-0000-4000-8000-000000000303"
INTENT_B = "00000000-0000-4000-8000-000000000304"
MESSAGE_A = "00000000-0000-4000-8000-000000000305"
MESSAGE_B = "00000000-0000-4000-8000-000000000306"
SUBJECT_ID = "00000000-0000-4000-8000-000000000307"
NODE_ID = "00000000-0000-4000-8000-000000000308"
MISSING_ID = "00000000-0000-4000-8000-000000000309"
MISSING_TARGET_ID = "00000000-0000-4000-8000-000000000310"


def _intent(intent_id: str, message_id: str, *, option_path: tuple[str, ...], value: object) -> DeferredStageIntent:
    return DeferredStageIntent.create(
        intent_id=intent_id,
        receiving_stage="output",
        target_stage="topology",
        catalog_kind="source",
        catalog_name="csv",
        redacted_summary="Retained structural constraint.",
        originating_message_id=message_id,
        message_content_hash=stable_hash(f"message:{message_id}"),
        constraints=(
            OptionValueConstraint(
                kind="option_value",
                subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
                option_path=option_path,
                operator="equals",
                value=value,  # type: ignore[arg-type]
            ),
        ),
    )


def _guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        step=GuidedStep.STEP_3_TRANSFORMS,
        source_order=(SOURCE_ID,),
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={"schema": {"mode": "observed"}, "delimiter": ","},
                observed_columns=("name",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(OUTPUT_ID,),
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="rows",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        deferred_intents=(
            _intent(INTENT_A, MESSAGE_A, option_path=("schema", "mode"), value="observed"),
            _intent(INTENT_B, MESSAGE_B, option_path=("delimiter",), value=","),
        ),
    )


def _candidate(*, delimiter: str = ",") -> CompositionState:
    return CompositionState(
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}, "delimiter": delimiter},
                on_success="rows",
                on_validation_failure="discard",
            )
        },
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="rows",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=2,
    )


def _node(
    *,
    node_id: str,
    plugin: str | None = "passthrough",
    node_type: NodeType = "transform",
    input_name: str = "data",
    on_success: str | None = "rows",
    on_error: str | None = None,
    routes: dict[str, str] | None = None,
    fork_to: tuple[str, ...] | None = None,
    condition: str | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=input_name,
        on_success=on_success,
        on_error=on_error,
        options={},
        condition=(condition or "True") if node_type == "gate" else None,
        routes=routes,
        fork_to=fork_to,
        branches=None,
        policy=None,
        merge=None,
    )


def _guided_with_constraint(constraint: DeferredConstraint) -> GuidedSession:
    intent = DeferredStageIntent.create(
        intent_id=INTENT_A,
        receiving_stage="output",
        target_stage="wire_review",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Retained structural constraint.",
        originating_message_id=MESSAGE_A,
        message_content_hash=stable_hash("adversarial coverage constraint"),
        constraints=(constraint,),
    )
    return replace(_guided(), deferred_intents=(intent,))


def _assert_unproven(candidate: CompositionState, constraint: DeferredConstraint) -> None:
    with pytest.raises(DeferredIntentClaimError, match="unproven"):
        evaluate_deferred_intent_coverage(
            candidate=candidate,
            reviewed_guided=_guided_with_constraint(constraint),
            claimed_intent_ids=(INTENT_A,),
        )


def test_claims_return_reviewed_order_independent_of_model_order() -> None:
    assert evaluate_deferred_intent_coverage(
        candidate=_candidate(),
        reviewed_guided=_guided(),
        claimed_intent_ids=(INTENT_B, INTENT_A),
    ) == (INTENT_A, INTENT_B)


@pytest.mark.parametrize(
    ("claims", "match"),
    [
        ((INTENT_A, INTENT_A), "duplicate"),
        (("00000000-0000-4000-8000-000000000399",), "unknown"),
    ],
)
def test_duplicate_and_unknown_claims_are_invalid_model_output(claims: tuple[str, ...], match: str) -> None:
    with pytest.raises(DeferredIntentClaimError, match=match):
        evaluate_deferred_intent_coverage(
            candidate=_candidate(),
            reviewed_guided=_guided(),
            claimed_intent_ids=claims,
        )


def test_unproven_claim_is_invalid_but_unclaimed_intent_remains_pending() -> None:
    with pytest.raises(DeferredIntentClaimError, match="unproven"):
        evaluate_deferred_intent_coverage(
            candidate=_candidate(delimiter=";"),
            reviewed_guided=_guided(),
            claimed_intent_ids=(INTENT_B,),
        )
    assert evaluate_deferred_intent_coverage(
        candidate=_candidate(delimiter=";"),
        reviewed_guided=_guided(),
        claimed_intent_ids=(INTENT_A,),
    ) == (INTENT_A,)


def test_required_deferred_intent_omission_is_repairable_model_output() -> None:
    with pytest.raises(DeferredIntentClaimError, match="omitted required"):
        evaluate_deferred_intent_coverage(
            candidate=_candidate(),
            reviewed_guided=_guided(),
            claimed_intent_ids=(INTENT_A,),
            required_intent_ids=(INTENT_B,),
        )


def test_ambiguous_plugin_subject_cannot_prove_negative_presence() -> None:
    ambiguous_intent = DeferredStageIntent.create(
        intent_id=INTENT_A,
        receiving_stage="output",
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary="Retained structural constraint.",
        originating_message_id=MESSAGE_A,
        message_content_hash=stable_hash("ambiguous plugin subject"),
        constraints=(
            SubjectPresenceConstraint(
                kind="subject_presence",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="00000000-0000-4000-8000-000000000307",
                    plugin_kind="transform",
                    plugin_name="passthrough",
                ),
                present=False,
            ),
        ),
    )
    candidate = replace(
        _candidate(),
        nodes=tuple(
            NodeSpec(
                id=f"copy-{index}",
                node_type="transform",
                plugin="passthrough",
                input="primary",
                on_success="rows",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
            for index in range(2)
        ),
    )

    with pytest.raises(DeferredIntentClaimError, match="unproven"):
        evaluate_deferred_intent_coverage(
            candidate=candidate,
            reviewed_guided=replace(_guided(), deferred_intents=(ambiguous_intent,)),
            claimed_intent_ids=(INTENT_A,),
        )


@pytest.mark.parametrize(
    "constraint",
    (
        SubjectPresenceConstraint(
            kind="subject_presence",
            subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
            present=False,
        ),
        ComponentCountConstraint(
            kind="component_count",
            component_kind="source",
            plugin_kind=None,
            plugin_name=None,
            operator="equals",
            count=0,
        ),
    ),
    ids=("negative-presence", "zero-count"),
)
def test_source_edge_shared_id_does_not_hide_source(constraint: DeferredConstraint) -> None:
    candidate = replace(
        _candidate(),
        edges=(EdgeSpec(id=SOURCE_ID, from_node="primary", to_node="rows", edge_type="on_success", label=None),),
    )
    assert candidate.validate().is_valid

    _assert_unproven(candidate, constraint)


@pytest.mark.parametrize(
    "constraint",
    (
        SubjectPresenceConstraint(
            kind="subject_presence",
            subject=StableSubject(kind="stable", component_kind="node", stable_id=NODE_ID),
            present=False,
        ),
        ComponentCountConstraint(
            kind="component_count",
            component_kind="node",
            plugin_kind=None,
            plugin_name=None,
            operator="equals",
            count=0,
        ),
    ),
    ids=("negative-presence", "zero-count"),
)
def test_node_edge_shared_id_does_not_hide_node(constraint: DeferredConstraint) -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="data",
                on_validation_failure="discard",
            )
        },
        nodes=(_node(node_id=NODE_ID, on_error="discard"),),
        edges=(EdgeSpec(id=NODE_ID, from_node="primary", to_node=NODE_ID, edge_type="on_success", label=None),),
    )
    # Fixture sanity: the shared node/edge id must be the ONLY thing Stage 1
    # objects to. ``NODE_ID`` doubles as a ``StableSubject.stable_id`` (which
    # must be a UUID) and as the ``NodeSpec.id`` (which the runtime rejects
    # for its leading digit); Stage 1 now mirrors that name rule
    # (elspeth-2ed41f0a4a), so a UUID-named node is correctly not runnable.
    # The coverage evaluator under test never consults ``is_valid``.
    assert {e.error_code for e in candidate.validate().errors} == {"node_id_invalid"}

    _assert_unproven(candidate, constraint)


@pytest.mark.parametrize(("present", "proven"), ((True, True), (False, False)))
def test_source_success_route_resolves_connection_through_node_input(present: bool, proven: bool) -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="data",
                on_validation_failure="discard",
            )
        },
        nodes=(_node(node_id="copy", input_name="data"),),
        edges=(EdgeSpec(id="source-copy", from_node="primary", to_node="copy", edge_type="on_success", label=None),),
    )
    constraint = EdgeRouteConstraint(
        kind="edge_route",
        from_subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        edge_type="on_success",
        to_subject=PluginSubject(
            kind="plugin",
            subject_id=SUBJECT_ID,
            plugin_kind="transform",
            plugin_name="passthrough",
        ),
        present=present,
    )

    if proven:
        assert evaluate_deferred_intent_coverage(
            candidate=candidate,
            reviewed_guided=_guided_with_constraint(constraint),
            claimed_intent_ids=(INTENT_A,),
        ) == (INTENT_A,)
    else:
        _assert_unproven(candidate, constraint)


@pytest.mark.parametrize(
    ("edge_type", "routes", "fork_to", "connection"),
    (
        ("route_true", {"true": "accepted"}, None, "accepted"),
        ("route_false", {"false": "rejected"}, None, "rejected"),
        ("fork", {"true": "fork"}, ("branch",), "branch"),
    ),
)
@pytest.mark.parametrize(("present", "proven"), ((True, True), (False, False)))
def test_gate_routes_resolve_connections_through_consumer_inputs(
    edge_type: Literal["route_true", "route_false", "fork"],
    routes: dict[str, str],
    fork_to: tuple[str, ...] | None,
    connection: str,
    present: bool,
    proven: bool,
) -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes=routes,
                fork_to=fork_to,
            ),
            _node(node_id="copy", input_name=connection),
        ),
        edges=(),
    )
    constraint = EdgeRouteConstraint(
        kind="edge_route",
        from_subject=StableSubject(kind="stable", component_kind="node", stable_id=NODE_ID),
        edge_type=edge_type,
        to_subject=PluginSubject(
            kind="plugin",
            subject_id=SUBJECT_ID,
            plugin_kind="transform",
            plugin_name="passthrough",
        ),
        present=present,
    )

    if proven:
        assert evaluate_deferred_intent_coverage(
            candidate=candidate,
            reviewed_guided=_guided_with_constraint(constraint),
            claimed_intent_ids=(INTENT_A,),
        ) == (INTENT_A,)
    else:
        _assert_unproven(candidate, constraint)


@pytest.mark.parametrize(
    "condition",
    (
        "row['amount'] > 500",
        'row["amount"] > 500',
        "row.get('amount') > 500",
        "500 < row['amount']",
    ),
)
def test_stated_predicate_is_proven_against_a_reachable_gate(condition: str) -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "rows", "false": "rows"},
                condition=condition,
            ),
        ),
    )
    constraint = StatedPredicateConstraint(
        kind="stated_predicate",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
    )

    assert evaluate_deferred_intent_coverage(
        candidate=candidate,
        reviewed_guided=_guided_with_constraint(constraint),
        claimed_intent_ids=(INTENT_A,),
    ) == (INTENT_A,)


@pytest.mark.parametrize(
    "condition",
    (
        "True",
        "row['amount'] >= 500",
        "row['amount'] > 501",
        "row['total'] > 500",
        "row['amount'] > 500 and row['active'] == True",
    ),
)
def test_stated_predicate_rejects_constant_or_semantically_different_gate(condition: str) -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "rows", "false": "rows"},
                condition=condition,
            ),
        ),
    )
    constraint = StatedPredicateConstraint(
        kind="stated_predicate",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
    )

    _assert_unproven(candidate, constraint)


@pytest.mark.parametrize(
    ("routes", "fork_to", "proven"),
    (
        ({"true": "high_value", "false": "standard"}, None, True),
        ({"true": "standard", "false": "high_value"}, None, False),
        ({"true": "high_value", "false": "high_value"}, None, False),
        ({"true": "fork", "false": "fork"}, ("high_value", "standard"), False),
    ),
)
def test_stated_gate_routing_proves_the_predicate_and_exactly_one_sink_per_branch(
    routes: dict[str, str],
    fork_to: tuple[str, ...] | None,
    proven: bool,
) -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes=routes,
                fork_to=fork_to,
                condition="row['amount'] > 500",
            ),
        ),
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="standard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    constraint = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
        true_target="high_value",
        false_target="standard",
    )
    reviewed = replace(
        _guided_with_constraint(constraint),
        output_order=(OUTPUT_ID, SECOND_OUTPUT_ID),
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
            SECOND_OUTPUT_ID: SinkOutputResolved(
                name="standard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        },
    )

    if proven:
        assert evaluate_deferred_intent_coverage(
            candidate=candidate,
            reviewed_guided=reviewed,
            claimed_intent_ids=(INTENT_A,),
        ) == (INTENT_A,)
    else:
        with pytest.raises(DeferredIntentClaimError, match="unproven"):
            evaluate_deferred_intent_coverage(
                candidate=candidate,
                reviewed_guided=reviewed,
                claimed_intent_ids=(INTENT_A,),
            )


def test_stated_gate_routing_resolves_real_output_named_discard_before_virtual_sentinel() -> None:
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "high_value", "false": "discard"},
                fork_to=None,
                condition="row['amount'] > 500",
            ),
        ),
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="discard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    constraint = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
        true_target="high_value",
        false_target="discard",
    )
    reviewed = replace(
        _guided_with_constraint(constraint),
        output_order=(OUTPUT_ID, SECOND_OUTPUT_ID),
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
            SECOND_OUTPUT_ID: SinkOutputResolved(
                name="discard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        },
    )

    assert evaluate_deferred_intent_coverage(
        candidate=candidate,
        reviewed_guided=reviewed,
        claimed_intent_ids=(INTENT_A,),
    ) == (INTENT_A,)


def test_stated_gate_routing_rejects_a_matching_gate_beyond_an_upstream_fanout_gate() -> None:
    downstream_gate_id = "00000000-0000-4000-8000-000000000311"
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="first-gate",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="first-gate",
                on_success=None,
                routes={"true": "fork", "false": "fork"},
                fork_to=("standard", "second-gate"),
                condition="True",
            ),
            _node(
                node_id=downstream_gate_id,
                plugin=None,
                node_type="gate",
                input_name="second-gate",
                on_success=None,
                routes={"true": "high_value", "false": "standard"},
                condition="row['amount'] > 500",
            ),
        ),
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="standard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    constraint = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
        true_target="high_value",
        false_target="standard",
    )

    _assert_unproven(candidate, constraint)


def test_stated_gate_routing_rejects_duplicate_paths_to_the_same_branch_sink() -> None:
    second_path_id = "00000000-0000-4000-8000-000000000312"
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "duplicated-high-path", "false": "standard"},
                condition="row['amount'] > 500",
            ),
            _node(
                node_id=MISSING_ID,
                input_name="duplicated-high-path",
                on_success="high_value",
            ),
            _node(
                node_id=second_path_id,
                input_name="duplicated-high-path",
                on_success="high_value",
            ),
        ),
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="standard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    constraint = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
        true_target="high_value",
        false_target="standard",
    )

    _assert_unproven(candidate, constraint)


def test_stated_gate_routing_rejects_a_downstream_gate_that_can_discard_rows() -> None:
    downstream_gate_id = "00000000-0000-4000-8000-000000000313"
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success="gate-input",
                on_validation_failure="discard",
            )
        },
        nodes=(
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "second-gate", "false": "standard"},
                condition="row['amount'] > 500",
            ),
            _node(
                node_id=downstream_gate_id,
                plugin=None,
                node_type="gate",
                input_name="second-gate",
                on_success=None,
                routes={"true": "high_value", "false": "discard"},
                condition="row['region'] == 'priority'",
            ),
        ),
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="standard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    constraint = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
        true_target="high_value",
        false_target="standard",
    )

    _assert_unproven(candidate, constraint)


@pytest.mark.parametrize("transform_position", ("before_gate", "true_branch"))
def test_stated_gate_routing_rejects_transform_error_paths_that_bypass_required_sinks(
    transform_position: Literal["before_gate", "true_branch"],
) -> None:
    transform_id = "00000000-0000-4000-8000-000000000314"
    if transform_position == "before_gate":
        source_connection = "pre-transform"
        nodes = (
            _node(
                node_id=transform_id,
                input_name="pre-transform",
                on_success="gate-input",
                on_error="discard",
            ),
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "high_value", "false": "standard"},
                condition="row['amount'] > 500",
            ),
        )
    else:
        source_connection = "gate-input"
        nodes = (
            _node(
                node_id=NODE_ID,
                plugin=None,
                node_type="gate",
                input_name="gate-input",
                on_success=None,
                routes={"true": "post-transform", "false": "standard"},
                condition="row['amount'] > 500",
            ),
            _node(
                node_id=transform_id,
                input_name="post-transform",
                on_success="high_value",
                on_error="discard",
            ),
        )
    candidate = replace(
        _candidate(),
        sources={
            "primary": SourceSpec(
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                on_success=source_connection,
                on_validation_failure="discard",
            )
        },
        nodes=nodes,
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="standard",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    constraint = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
        column="amount",
        operator="greater_than",
        value=500,
        true_target="high_value",
        false_target="standard",
    )

    _assert_unproven(candidate, constraint)


def test_missing_option_path_cannot_prove_not_equals() -> None:
    _assert_unproven(
        _candidate(),
        OptionValueConstraint(
            kind="option_value",
            subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
            option_path=("missing",),
            operator="not_equals",
            value="value",
        ),
    )


def test_missing_failure_subject_cannot_prove_not_equals() -> None:
    _assert_unproven(
        _candidate(),
        FailureRouteConstraint(
            kind="failure_route",
            subject=StableSubject(kind="stable", component_kind="source", stable_id=MISSING_ID),
            failure_kind="source_validation",
            operator="not_equals",
            target="discard",
        ),
    )


def test_missing_failure_target_cannot_prove_not_equals() -> None:
    _assert_unproven(
        _candidate(),
        FailureRouteConstraint(
            kind="failure_route",
            subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
            failure_kind="source_validation",
            operator="not_equals",
            target=StableSubject(kind="stable", component_kind="output", stable_id=MISSING_TARGET_ID),
        ),
    )


@pytest.mark.parametrize("missing", ("origin", "destination"))
def test_missing_edge_route_authority_cannot_prove_absence(missing: str) -> None:
    existing_source = StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID)
    existing_output = StableSubject(kind="stable", component_kind="output", stable_id=OUTPUT_ID)
    _assert_unproven(
        _candidate(),
        EdgeRouteConstraint(
            kind="edge_route",
            from_subject=(
                StableSubject(kind="stable", component_kind="source", stable_id=MISSING_ID) if missing == "origin" else existing_source
            ),
            edge_type="on_success",
            to_subject=(
                StableSubject(kind="stable", component_kind="output", stable_id=MISSING_TARGET_ID)
                if missing == "destination"
                else existing_output
            ),
            present=False,
        ),
    )
