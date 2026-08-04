"""ADR-033 closed deferred-intent contradiction checker (lane A).

Every test drives ``validate_deferred_intent_action`` through its public API
only.  The checker decides contradiction within (i) a single exact subject
identity and (ii) closed count-bound arithmetic, declines existential subject
resolution, and never converts "unproven" into "rejected".  Sets that are
unsatisfiable outside the closed rules stay admitted and are caught
fail-closed at wire confirmation.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import combinations

from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSummary
from elspeth.web.composer.guided.deferred_intents import (
    DeferredIntentAccepted,
    DeferredIntentAction,
    DeferredIntentRejected,
    constraint_holds,
    validate_deferred_intent_action,
)
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
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId

SUBJECT_ID = "33333333-3333-4333-8333-333333333333"
SECOND_SUBJECT_ID = "44444444-4444-4444-8444-444444444444"
INTENT_ID = "11111111-1111-4111-8111-111111111111"
SECOND_INTENT_ID = "12121212-1212-4212-8212-121212121212"
MESSAGE_ID = "22222222-2222-4222-8222-222222222222"

_CLOSED_RULES = frozenset(
    {
        "conflicting_subject_facts",
        "empty_count_bounds",
        "count_group_subsumption",
        "predicate_gate_capacity",
        "required_subject_absent",
        "option_path_collapse",
        "option_domain_exhausted",
    }
)

_MODE_ENUM_SCHEMA: dict[str, object] = {"type": "object", "properties": {"mode": {"enum": ["safe", "strict"]}}}
_DELIMITER_ENUM_SCHEMA: dict[str, object] = {"type": "object", "properties": {"delimiter": {"enum": [";", ","]}}}
_FREE_STRING_SCHEMA: dict[str, object] = {"type": "object", "properties": {"mode": {"type": "string"}}}


class _Catalog:
    def __init__(self, schemas: dict[tuple[PluginKind, str], dict[str, object]]) -> None:
        self._schemas = schemas

    def _list(self, kind: PluginKind) -> list[PluginSummary]:
        return [
            PluginSummary(name=name, description=name, plugin_type=plugin_kind, config_fields=[])
            for (plugin_kind, name) in self._schemas
            if plugin_kind == kind
        ]

    def list_sources(self) -> list[PluginSummary]:
        return self._list("source")

    def list_transforms(self) -> list[PluginSummary]:
        return self._list("transform")

    def list_sinks(self) -> list[PluginSummary]:
        return self._list("sink")

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        return PluginSchemaInfo(
            name=name,
            plugin_type=plugin_type,
            description=name,
            json_schema=self._schemas[(plugin_type, name)],
            knob_schema={"fields": []},
        )

    def post_call_hints(
        self,
        *,
        plugin_type: PluginKind,
        plugin_name: str,
        tool_name: str,
        config_snapshot: dict[str, object],
    ) -> tuple[str, ...]:
        raise AssertionError("deferred intent admission must not dispatch plugins")


def _view(schemas: dict[tuple[PluginKind, str], dict[str, object]] | None = None) -> PolicyCatalogView:
    resolved_schemas = (
        schemas
        if schemas is not None
        else {
            ("transform", "normalize"): _MODE_ENUM_SCHEMA,
            ("transform", "llm"): _FREE_STRING_SCHEMA,
            ("sink", "json"): _MODE_ENUM_SCHEMA,
            ("sink", "archive"): _FREE_STRING_SCHEMA,
            ("source", "csv"): _DELIMITER_ENUM_SCHEMA,
        }
    )
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:test",
        available=frozenset(PluginId(kind, name) for kind, name in resolved_schemas),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(_Catalog(resolved_schemas), snapshot, profiles=None)  # type: ignore[arg-type]


def _plugin_subject(*, kind: PluginKind = "transform", name: str = "normalize", subject_id: str = SUBJECT_ID) -> PluginSubject:
    return PluginSubject(kind="plugin", subject_id=subject_id, plugin_kind=kind, plugin_name=name)


def _stable(component_kind: str, stable_id: str) -> StableSubject:
    return StableSubject(kind="stable", component_kind=component_kind, stable_id=stable_id)  # type: ignore[arg-type]


def _presence(subject: PluginSubject | StableSubject, present: bool) -> SubjectPresenceConstraint:
    return SubjectPresenceConstraint(kind="subject_presence", subject=subject, present=present)


def _option(
    subject: PluginSubject | StableSubject,
    *,
    path: tuple[str, ...] = ("mode",),
    operator: str = "equals",
    value: object = "safe",
) -> OptionValueConstraint:
    return OptionValueConstraint(
        kind="option_value",
        subject=subject,
        option_path=path,
        operator=operator,  # type: ignore[arg-type]
        value=value,  # type: ignore[arg-type]
    )


def _count(
    component_kind: str,
    operator: str,
    count: int,
    *,
    plugin_kind: PluginKind | None = None,
    plugin_name: str | None = None,
) -> ComponentCountConstraint:
    return ComponentCountConstraint(
        kind="component_count",
        component_kind=component_kind,  # type: ignore[arg-type]
        plugin_kind=plugin_kind,
        plugin_name=plugin_name,
        operator=operator,  # type: ignore[arg-type]
        count=count,
    )


def _predicate(
    subject: PluginSubject | StableSubject,
    *,
    column: str = "amount",
    operator: str = "greater_than",
    value: object = 500,
) -> StatedPredicateConstraint:
    return StatedPredicateConstraint(
        kind="stated_predicate",
        subject=subject,
        column=column,
        operator=operator,  # type: ignore[arg-type]
        value=value,  # type: ignore[arg-type]
    )


def _routing(
    subject: PluginSubject | StableSubject | None = None,
    *,
    true_target: str = "high_value",
    false_target: str = "standard",
) -> StatedGateRoutingConstraint:
    return StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=subject if subject is not None else _plugin_subject(),
        column="amount",
        operator="greater_than",
        value=500,
        true_target=true_target,
        false_target=false_target,
    )


_STAGE_ORDINAL = {"source": 0, "output": 1, "topology": 2, "wire_review": 3}
_COMPONENT_STAGE = {"source": "source", "output": "output", "node": "topology", "edge": "wire_review"}
_PLUGIN_STAGE = {"source": "source", "sink": "output", "transform": "topology"}


def _constraint_stage(constraint: DeferredConstraint) -> str:
    if type(constraint) in {SubjectPresenceConstraint, OptionValueConstraint}:
        subject = constraint.subject  # type: ignore[union-attr]
        if type(subject) is StableSubject:
            return _COMPONENT_STAGE[subject.component_kind]
        return _PLUGIN_STAGE[subject.plugin_kind]  # type: ignore[union-attr]
    if type(constraint) is ComponentCountConstraint:
        return _COMPONENT_STAGE[constraint.component_kind]
    if type(constraint) in {StatedPredicateConstraint, StatedGateRoutingConstraint}:
        return "topology"
    if type(constraint) is EdgeRouteConstraint:
        return "topology" if constraint.edge_type in {"route_true", "route_false", "fork"} else "wire_review"
    if type(constraint) is FailureRouteConstraint:
        if constraint.target != "discard":
            return "wire_review"
        subject = constraint.subject
        if type(subject) is StableSubject:
            return _COMPONENT_STAGE[subject.component_kind]
        return _PLUGIN_STAGE[subject.plugin_kind]
    raise AssertionError("unsupported constraint in stage helper")


def _action(
    *constraints: DeferredConstraint,
    catalog_kind: str | None = None,
    catalog_name: str | None = None,
) -> DeferredIntentAction:
    stages = [_constraint_stage(constraint) for constraint in constraints]
    if catalog_kind is not None:
        stages.append(_PLUGIN_STAGE[catalog_kind])
    target = max(stages, key=_STAGE_ORDINAL.__getitem__)
    return DeferredIntentAction(
        target_stage=target,  # type: ignore[arg-type]
        catalog_kind=catalog_kind,  # type: ignore[arg-type]
        catalog_name=catalog_name,
        redacted_summary="Closed-conjunction admission probe.",
        constraints=constraints,
    )


_BENIGN_CONSTRAINT = _count("edge", "at_least", 0)


def _benign_action() -> DeferredIntentAction:
    """A vacuous wire-review constraint used to probe a retained conjunction."""

    return DeferredIntentAction(
        target_stage="wire_review",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Benign structural probe.",
        constraints=(_BENIGN_CONSTRAINT,),
    )


def _intent(
    *constraints: DeferredConstraint,
    intent_id: str = INTENT_ID,
    summary: str = "Retained structural instruction.",
) -> DeferredStageIntent:
    return DeferredStageIntent.create(
        intent_id=intent_id,
        receiving_stage="source",
        target_stage="wire_review",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary=summary,
        originating_message_id=MESSAGE_ID,
        message_content_hash=stable_hash("private originating message"),
        constraints=constraints,
    )


def _validate(
    action: DeferredIntentAction,
    *,
    guided: GuidedSession | None = None,
    catalog: PolicyCatalogView | None = None,
    message: str | None = None,
    replacing_intent_id: str | None = None,
) -> object:
    return validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=catalog if catalog is not None else _view(),
        guided=guided if guided is not None else GuidedSession.initial(),
        originating_message_content=message,
        replacing_intent_id=replacing_intent_id,
    )


def _assert_contradiction(result: object, *, rule: str) -> DeferredIntentRejected:
    assert type(result) is DeferredIntentRejected, result
    assert result.reason == "constraint_contradiction"
    assert result.contradiction is not None
    assert result.contradiction.rule == rule
    assert result.contradiction.rule in _CLOSED_RULES
    return result


def _assert_retained_conjunction_admitted(*constraints: DeferredConstraint, guided: GuidedSession | None = None) -> None:
    """Assert the checker admits a conjunction carried entirely by retained intents.

    Routes accept-side cases whose constraints would otherwise trip the
    unrelated stated-fact grounding gates: the retained side is exercised
    through the same public entry point via a vacuous new action.
    """

    base = guided if guided is not None else GuidedSession.initial()
    populated = replace(base, deferred_intents=(_intent(*constraints),))
    result = _validate(_benign_action(), guided=populated)
    assert type(result) is DeferredIntentAccepted, result


# --- The ticket's four named verification classes (elspeth-d293c5d139) ---


def test_compatible_duplicate_constraints_are_accepted() -> None:
    option = _option(_plugin_subject(), value="strict")
    action = _action(
        option,
        option,
        _count("node", "at_least", 1, plugin_kind="transform", plugin_name="normalize"),
        _count("node", "at_most", 2, plugin_kind="transform", plugin_name="normalize"),
        catalog_kind="transform",
        catalog_name="normalize",
    )

    result = _validate(action)

    assert result == DeferredIntentAccepted(action=action)


def test_contradictory_constraints_are_rejected() -> None:
    result = _validate(
        _action(
            _option(_plugin_subject(), value="safe"),
            _option(_plugin_subject(), value="strict"),
            catalog_kind="transform",
            catalog_name="normalize",
        )
    )

    _assert_contradiction(result, rule="conflicting_subject_facts")


def test_disjoint_subject_constraints_are_accepted() -> None:
    first_id = "44444444-4444-4444-8444-444444444444"
    second_id = "55555555-5555-4555-8555-555555555555"
    reviewed = SinkOutputResolved(
        name="results",
        plugin="json",
        options={"mode": "strict"},
        required_fields=(),
        schema_mode="observed",
        on_write_failure="discard",
    )
    guided = replace(
        GuidedSession.initial(),
        output_order=(first_id, second_id),
        reviewed_outputs={first_id: reviewed, second_id: replace(reviewed, name="archive")},
    )
    action = _action(
        _option(_stable("output", first_id), value="strict"),
        _option(_stable("output", second_id), operator="not_equals", value="strict"),
        catalog_kind="sink",
        catalog_name="json",
    )

    result = _validate(action, guided=guided)

    assert result == DeferredIntentAccepted(action=action)


def test_empty_count_bound_intersections_are_rejected() -> None:
    result = _validate(
        _action(
            _count("node", "at_least", 2, plugin_kind="transform", plugin_name="normalize"),
            _count("node", "at_most", 1, plugin_kind="transform", plugin_name="normalize"),
            catalog_kind="transform",
            catalog_name="normalize",
        )
    )

    _assert_contradiction(result, rule="empty_count_bounds")


# --- One named test per ADR-033 closed rule ---


def test_rule_1_functional_dependency_conflicts_on_one_subject_reject() -> None:
    subject = _plugin_subject()
    destination = _plugin_subject(kind="sink", name="json", subject_id="66666666-6666-4666-8666-666666666666")
    edge = EdgeRouteConstraint(kind="edge_route", from_subject=subject, edge_type="on_success", to_subject=destination, present=True)
    failure = FailureRouteConstraint(
        kind="failure_route", subject=subject, failure_kind="node_error", operator="equals", target=destination
    )
    conflicting_pairs: tuple[tuple[DeferredConstraint, DeferredConstraint], ...] = (
        (_presence(subject, True), _presence(subject, False)),
        (_option(subject, value="safe"), _option(subject, value="strict")),
        (_option(subject, value="safe"), _option(subject, operator="not_equals", value="safe")),
        (edge, replace(edge, present=False)),
        (_predicate(subject), _predicate(subject, column="total")),
        (_predicate(subject, value=1, operator="equals"), _predicate(subject, value=1.0, operator="equals")),
        (_routing(subject), _routing(subject, true_target="standard", false_target="high_value")),
        (failure, replace(failure, operator="not_equals")),
    )

    for first, second in conflicting_pairs:
        result = _validate(_action(first, second, catalog_kind="transform", catalog_name="normalize"))
        _assert_contradiction(result, rule="conflicting_subject_facts")


def test_rule_2_empty_count_bounds_reject() -> None:
    subject = _plugin_subject()
    stable_node = _stable("node", "77777777-7777-4777-8777-777777777777")
    cases: tuple[tuple[DeferredConstraint, ...], ...] = (
        (_count("node", "at_least", 2), _count("node", "at_most", 1)),
        (_count("node", "equals", 1), _count("node", "equals", 2)),
        (_presence(subject, False), _count("node", "at_least", 1, plugin_kind="transform", plugin_name="normalize")),
        (_presence(subject, True), _count("node", "equals", 0, plugin_kind="transform", plugin_name="normalize")),
        (_presence(stable_node, True), _count("node", "equals", 0)),
        (_option(subject, value="strict"), _count("node", "at_most", 0, plugin_kind="transform", plugin_name="normalize")),
    )

    for constraints in cases:
        result = _validate(_action(*constraints, catalog_kind="transform", catalog_name="normalize"))
        _assert_contradiction(result, rule="empty_count_bounds")


def test_rule_3_cross_group_count_subsumption_rejects() -> None:
    # The ADR's normative example: a global node at_most 1 alongside
    # transform:normalize at_least 2 is a contradiction.
    result = _validate(
        _action(
            _count("node", "at_most", 1),
            _count("node", "at_least", 2, plugin_kind="transform", plugin_name="normalize"),
            catalog_kind="transform",
            catalog_name="normalize",
        )
    )
    _assert_contradiction(result, rule="count_group_subsumption")

    # Two provably-distinct exact required subjects also sum against the cap.
    two_nodes = _validate(
        _action(
            _presence(_stable("node", "66666666-6666-4666-8666-666666666666"), True),
            _presence(_stable("node", "77777777-7777-4777-8777-777777777777"), True),
            _count("node", "at_most", 1),
        )
    )
    _assert_contradiction(two_nodes, rule="count_group_subsumption")


def test_rule_4_exact_subject_predicate_gates_count_against_node_caps() -> None:
    first_source = _stable("source", "55555555-5555-4555-8555-555555555555")
    second_source = _stable("source", "66666666-6666-4666-8666-666666666666")

    # Distinct predicate signatures on distinct exact subjects each imply one
    # synthesised gate node; with a required ambiguous normalize identity the
    # closed minimum is three nodes against a cap of two.
    rejected = _validate(
        _action(
            _presence(_plugin_subject(), True),
            _predicate(first_source),
            _predicate(second_source, column="risk_score", value=80),
            _count("node", "at_most", 2),
            catalog_kind="transform",
            catalog_name="normalize",
        )
    )
    _assert_contradiction(rejected, rule="predicate_gate_capacity")

    # A stable node subject witnesses its own predicate: no extra gate.
    _assert_retained_conjunction_admitted(
        _predicate(_stable("node", "55555555-5555-4555-8555-555555555555")),
        _count("node", "at_most", 1),
    )

    # Declined for ambiguous plugin subjects: a gate implied by a subject that
    # has not resolved to an exact identity is not counted.
    _assert_retained_conjunction_admitted(
        _predicate(_plugin_subject()),
        _predicate(_plugin_subject(subject_id=SECOND_SUBJECT_ID), column="risk_score", value=80),
        _count("node", "at_most", 1),
    )


def test_rule_5_presence_false_conflicts_with_implicit_requirement() -> None:
    stable_id = "44444444-4444-4444-8444-444444444444"
    guided = replace(
        GuidedSession.initial(),
        output_order=(stable_id,),
        reviewed_outputs={
            stable_id: SinkOutputResolved(
                name="results",
                plugin="json",
                options={"mode": "strict"},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )
    # An option constraint on the single-match plugin alias implicitly
    # requires the same reviewed output the stable subject asserts absent.
    aliased = _validate(
        _action(
            _presence(_stable("output", stable_id), False),
            _option(_plugin_subject(kind="sink", name="json", subject_id=stable_id), value="strict"),
            catalog_kind="sink",
            catalog_name="json",
        ),
        guided=guided,
    )
    _assert_contradiction(aliased, rule="required_subject_absent")

    # An edge requires both endpoints regardless of the edge's own present flag.
    subject = _plugin_subject()
    destination = _plugin_subject(kind="sink", name="json", subject_id="66666666-6666-4666-8666-666666666666")
    edge_absent = _validate(
        _action(
            EdgeRouteConstraint(kind="edge_route", from_subject=subject, edge_type="on_success", to_subject=destination, present=False),
            _presence(destination, False),
            catalog_kind="transform",
            catalog_name="normalize",
        )
    )
    _assert_contradiction(edge_absent, rule="required_subject_absent")

    # A globally absent plugin identity intersects a required identity even
    # across distinct subject hints.
    absent_identity = _validate(
        _action(
            _presence(_plugin_subject(subject_id="55555555-5555-4555-8555-555555555555"), True),
            _presence(_plugin_subject(subject_id="66666666-6666-4666-8666-666666666666"), False),
            catalog_kind="transform",
            catalog_name="normalize",
        )
    )
    _assert_contradiction(absent_identity, rule="required_subject_absent")


def test_rule_6_scalar_parent_path_conflicts_with_descendant_on_one_subject() -> None:
    subject = _plugin_subject()
    for descendant_operator in ("equals", "not_equals"):
        result = _validate(
            _action(
                _option(subject, value="safe"),
                _option(subject, path=("mode", "level"), operator=descendant_operator, value="strict"),
                catalog_kind="transform",
                catalog_name="normalize",
            )
        )
        _assert_contradiction(result, rule="option_path_collapse")


def test_rule_7_not_equals_set_exhausting_validated_finite_domain_rejects() -> None:
    strict_exclusion = _action(
        _option(_plugin_subject(), operator="not_equals", value="strict"),
        catalog_kind="transform",
        catalog_name="normalize",
    )
    guided = replace(GuidedSession.initial(), deferred_intents=(_intent(*strict_exclusion.constraints),))

    result = _validate(
        _action(
            _option(_plugin_subject(), operator="not_equals", value="safe"),
            catalog_kind="transform",
            catalog_name="normalize",
        ),
        guided=guided,
    )

    rejection = _assert_contradiction(result, rule="option_domain_exhausted")
    assert rejection.contradiction is not None
    assert rejection.contradiction.conflicting_intent_id == INTENT_ID


# --- Ported closed-checker behavior (release constraint kinds) ---


def test_cross_vocabulary_direct_contradictions_are_rejected() -> None:
    subject = _plugin_subject()
    destination = _plugin_subject(kind="sink", name="json", subject_id="66666666-6666-4666-8666-666666666666")
    edge = EdgeRouteConstraint(kind="edge_route", from_subject=subject, edge_type="on_success", to_subject=destination, present=True)
    contradictions: tuple[tuple[DeferredConstraint, ...], ...] = (
        (_presence(subject, False), _option(subject, value="safe")),
        (edge, replace(edge, present=False)),
        (
            FailureRouteConstraint(kind="failure_route", subject=subject, failure_kind="node_error", operator="equals", target=destination),
            FailureRouteConstraint(
                kind="failure_route", subject=subject, failure_kind="node_error", operator="not_equals", target=destination
            ),
        ),
        (_count("node", "at_most", 1), _count("node", "at_least", 2, plugin_kind="transform", plugin_name="normalize")),
        (_presence(subject, False), _count("node", "at_least", 1, plugin_kind="transform", plugin_name="normalize")),
        (replace(edge, present=False), _presence(destination, False)),
    )

    for constraints in contradictions:
        result = _validate(_action(*constraints))
        assert type(result) is DeferredIntentRejected, constraints
        assert result.reason == "constraint_contradiction"
        assert result.contradiction is not None
        assert result.contradiction.rule in _CLOSED_RULES


def test_transform_hint_stays_existential_and_distinct_from_stable_node() -> None:
    stable_id = "44444444-4444-4444-8444-444444444444"
    stable_node = _stable("node", stable_id)
    transform_alias = _plugin_subject(subject_id=stable_id)
    output = _stable("output", "55555555-5555-4555-8555-555555555555")
    failure = FailureRouteConstraint(kind="failure_route", subject=stable_node, failure_kind="node_error", operator="equals", target=output)
    cases: tuple[tuple[DeferredConstraint, ...], ...] = (
        (_presence(stable_node, False), _presence(transform_alias, True)),
        (_presence(stable_node, False), _option(transform_alias, value="safe")),
        (
            EdgeRouteConstraint(kind="edge_route", from_subject=stable_node, edge_type="on_success", to_subject=output, present=True),
            EdgeRouteConstraint(kind="edge_route", from_subject=transform_alias, edge_type="on_success", to_subject=output, present=False),
        ),
        (_presence(stable_node, False), _predicate(transform_alias)),
        (failure, replace(failure, subject=transform_alias, operator="not_equals")),
    )

    for constraints in cases:
        _assert_retained_conjunction_admitted(*constraints)


def test_failure_route_targets_unify_reviewed_stable_and_plugin_aliases() -> None:
    stable_id = "44444444-4444-4444-8444-444444444444"
    subject = _stable("output", stable_id)
    guided = replace(
        GuidedSession.initial(),
        output_order=(stable_id,),
        reviewed_outputs={
            stable_id: SinkOutputResolved(
                name="results",
                plugin="json",
                options={},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )
    action = _action(
        FailureRouteConstraint(
            kind="failure_route",
            subject=subject,
            failure_kind="output_write",
            operator="equals",
            target=_stable("output", stable_id),
        ),
        FailureRouteConstraint(
            kind="failure_route",
            subject=subject,
            failure_kind="output_write",
            operator="not_equals",
            target=_plugin_subject(kind="sink", name="json", subject_id=stable_id),
        ),
        catalog_kind="sink",
        catalog_name="json",
    )

    result = _validate(action, guided=guided)

    _assert_contradiction(result, rule="conflicting_subject_facts")


def test_new_constraints_are_checked_against_retained_intents_and_name_the_culprit() -> None:
    guided = replace(
        GuidedSession.initial(),
        deferred_intents=(
            _intent(_option(_plugin_subject(), value="strict"), summary="Future topology instruction; 1 structural constraint(s)."),
        ),
    )

    result = _validate(
        _action(_option(_plugin_subject(), value="safe"), catalog_kind="transform", catalog_name="normalize"),
        guided=guided,
    )

    rejection = _assert_contradiction(result, rule="conflicting_subject_facts")
    assert rejection.contradiction is not None
    assert rejection.contradiction.conflicting_intent_id == INTENT_ID
    assert rejection.contradiction.conflicting_intent_summary == "Future topology instruction; 1 structural constraint(s)."


def test_cross_intent_required_cardinality_is_checked_prospectively() -> None:
    guided = replace(
        GuidedSession.initial(),
        deferred_intents=(_intent(_presence(_stable("node", "66666666-6666-4666-8666-666666666666"), True)),),
    )

    result = _validate(
        _action(
            _presence(_stable("node", "77777777-7777-4777-8777-777777777777"), True),
            _count("node", "at_most", 1),
        ),
        guided=guided,
    )

    rejection = _assert_contradiction(result, rule="count_group_subsumption")
    assert rejection.contradiction is not None
    assert rejection.contradiction.conflicting_intent_id == INTENT_ID


def test_required_subject_with_positive_count_bounds_is_accepted() -> None:
    action = _action(
        _presence(_plugin_subject(), True),
        _count("node", "at_least", 1, plugin_kind="transform", plugin_name="normalize"),
        _count("node", "at_most", 2, plugin_kind="transform", plugin_name="normalize"),
        catalog_kind="transform",
        catalog_name="normalize",
    )

    result = _validate(action)

    assert result == DeferredIntentAccepted(action=action)


def test_gate_route_targets_count_against_output_upper_bound() -> None:
    result = _validate(_action(_routing(), _count("output", "at_most", 1), catalog_kind="transform", catalog_name="normalize"))

    _assert_contradiction(result, rule="count_group_subsumption")


def test_unresolved_route_target_with_global_plugin_exclusion_is_accepted() -> None:
    _assert_retained_conjunction_admitted(
        _routing(),
        _presence(_plugin_subject(kind="sink", name="json", subject_id="88888888-8888-4888-8888-888888888888"), False),
    )


def test_reviewed_route_target_exclusion_is_rejected() -> None:
    output_id = "99999999-9999-4999-8999-999999999999"
    guided = replace(
        GuidedSession.initial(),
        output_order=(output_id,),
        reviewed_outputs={
            output_id: SinkOutputResolved(
                name="high_value",
                plugin="json",
                options={},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )

    result = _validate(
        _action(_routing(), _presence(_stable("output", output_id), False)),
        guided=guided,
    )

    _assert_contradiction(result, rule="required_subject_absent")


def test_distinct_reviewed_output_exclusion_with_future_routes_is_accepted() -> None:
    output_id = "99999999-9999-4999-8999-999999999999"
    guided = replace(
        GuidedSession.initial(),
        output_order=(output_id,),
        reviewed_outputs={
            output_id: SinkOutputResolved(
                name="archive",
                plugin="json",
                options={},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )

    _assert_retained_conjunction_admitted(
        _routing(),
        _presence(_stable("output", output_id), False),
        guided=guided,
    )


def test_explicit_routes_are_not_counted_as_edge_components() -> None:
    first_source = _stable("source", "55555555-5555-4555-8555-555555555555")
    second_source = _stable("source", "66666666-6666-4666-8666-666666666666")
    first_target = _stable("output", "77777777-7777-4777-8777-777777777777")
    second_target = _stable("output", "88888888-8888-4888-8888-888888888888")

    def route(from_subject: StableSubject, to_subject: StableSubject, *, present: bool = True) -> EdgeRouteConstraint:
        return EdgeRouteConstraint(
            kind="edge_route", from_subject=from_subject, edge_type="on_success", to_subject=to_subject, present=present
        )

    cases: tuple[tuple[DeferredConstraint, ...], ...] = (
        (route(first_source, first_target), _count("edge", "at_most", 0)),
        (route(first_source, first_target), route(second_source, second_target), _count("edge", "at_most", 0)),
        (route(first_source, first_target, present=False), _count("edge", "at_most", 0)),
    )

    for constraints in cases:
        action = _action(*constraints)
        result = _validate(action)
        assert result == DeferredIntentAccepted(action=action)


def test_stable_node_and_transform_alias_count_as_one_subject() -> None:
    stable_id = "66666666-6666-4666-8666-666666666666"
    action = _action(
        _presence(_stable("node", stable_id), True),
        _presence(_plugin_subject(subject_id=stable_id), True),
        _count("node", "at_most", 1),
        catalog_kind="transform",
        catalog_name="normalize",
    )

    result = _validate(action)

    assert result == DeferredIntentAccepted(action=action)


def test_same_plugin_fallbacks_merge_for_cardinality_minimum() -> None:
    action = _action(
        _presence(_plugin_subject(subject_id="55555555-5555-4555-8555-555555555555"), True),
        _presence(_plugin_subject(subject_id="66666666-6666-4666-8666-666666666666"), True),
        _count("node", "at_most", 1),
        catalog_kind="transform",
        catalog_name="normalize",
    )

    result = _validate(action)

    assert result == DeferredIntentAccepted(action=action)


def test_required_sink_plugin_may_inhabit_a_named_route_target() -> None:
    _assert_retained_conjunction_admitted(
        _routing(),
        _presence(_plugin_subject(kind="sink", name="json", subject_id="77777777-7777-4777-8777-777777777777"), True),
        _count("output", "at_most", 2),
    )


def test_plugin_specific_zero_count_resolves_reviewed_stable_subject() -> None:
    stable_id = "99999999-9999-4999-8999-999999999999"
    guided = replace(
        GuidedSession.initial(),
        output_order=(stable_id,),
        reviewed_outputs={
            stable_id: SinkOutputResolved(
                name="results",
                plugin="json",
                options={},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )

    def action_for(plugin_name: str) -> DeferredIntentAction:
        return _action(
            _presence(_stable("output", stable_id), True),
            _count("output", "at_most", 0, plugin_kind="sink", plugin_name=plugin_name),
            catalog_kind="sink",
            catalog_name=plugin_name,
        )

    matching = _validate(action_for("json"), guided=guided)
    different = _validate(action_for("archive"), guided=guided)

    _assert_contradiction(matching, rule="empty_count_bounds")
    assert different == DeferredIntentAccepted(action=action_for("archive"))


def test_one_predicate_signature_per_subject() -> None:
    first = _predicate(_plugin_subject())
    different_column = _predicate(_plugin_subject(), column="total")
    exact_route = _routing(_plugin_subject(), true_target="high", false_target="standard")
    other_subject = _predicate(_plugin_subject(subject_id="88888888-8888-4888-8888-888888888888"), column="total")

    rejected = _validate(_action(first, different_column))
    _assert_contradiction(rejected, rule="conflicting_subject_facts")

    _assert_retained_conjunction_admitted(first, exact_route)
    _assert_retained_conjunction_admitted(first, other_subject)


def test_conflicting_gate_routes_for_one_predicate_are_rejected() -> None:
    first_route = _routing(_plugin_subject(), true_target="high", false_target="standard")
    conflicting_route = _routing(_plugin_subject(), true_target="standard", false_target="high")

    result = _validate(_action(first_route, conflicting_route))

    _assert_contradiction(result, rule="conflicting_subject_facts")


def test_signed_float_zero_predicates_agree_like_gate_coverage() -> None:
    _assert_retained_conjunction_admitted(
        _predicate(_plugin_subject(), operator="equals", value=0.0),
        _predicate(_plugin_subject(), operator="equals", value=-0.0),
    )


def test_different_transform_plugins_are_not_aliased_by_subject_hint() -> None:
    shared_hint = "55555555-5555-4555-8555-555555555555"
    action = _action(
        _presence(_plugin_subject(name="normalize", subject_id=shared_hint), True),
        _presence(_plugin_subject(name="llm", subject_id=shared_hint), False),
    )

    result = _validate(action)

    assert result == DeferredIntentAccepted(action=action)


def test_edit_replacement_does_not_conflict_with_its_own_prior_version() -> None:
    guided = replace(
        GuidedSession.initial(),
        deferred_intents=(_intent(_option(_plugin_subject(), value="safe")),),
    )
    replacement = _action(_option(_plugin_subject(), value="strict"), catalog_kind="transform", catalog_name="normalize")

    without_exclusion = _validate(replacement, guided=guided)
    with_exclusion = _validate(replacement, guided=guided, replacing_intent_id=INTENT_ID)

    _assert_contradiction(without_exclusion, rule="conflicting_subject_facts")
    assert with_exclusion == DeferredIntentAccepted(action=replacement)


# --- ADR-033 documented declinations ---


def test_adr033_declined_cardinality_forced_merge_sets_are_admitted() -> None:
    """ADR-033: the four cardinality-forced-merge rejections are declined.

    These sets need witness partitioning or alias-profile resolution to prove
    unsatisfiable.  The closed checker deliberately declines existential
    subject resolution, so they are admitted here and caught fail-closed at
    wire confirmation (HTTP 409 with edit/cancel recourse).
    """

    first = _plugin_subject(subject_id="55555555-5555-4555-8555-555555555555")
    second = _plugin_subject(subject_id="66666666-6666-4666-8666-666666666666")
    target = _stable("output", "77777777-7777-4777-8777-777777777777")
    plugin_cap = _count("node", "at_most", 1, plugin_kind="transform", plugin_name="normalize")
    declined_sets: tuple[tuple[DeferredConstraint, ...], ...] = (
        # test_closed_conjunction_does_not_merge_incompatible_same_plugin_option_witnesses
        (_option(first, value="strict"), _option(second, value="safe"), _count("node", "at_most", 1)),
        # test_closed_conjunction_does_not_alias_scalar_parent_with_descendant_option
        (_option(first, value="safe"), _option(second, path=("mode", "level"), value="strict"), plugin_cap),
        # test_closed_conjunction_uses_whole_bin_finite_option_exclusions
        (
            _option(first, operator="not_equals", value="safe"),
            _option(second, operator="not_equals", value="strict"),
            plugin_cap,
        ),
        # test_closed_conjunction_does_not_alias_opposite_edge_presence_profiles
        (
            EdgeRouteConstraint(kind="edge_route", from_subject=first, edge_type="on_success", to_subject=target, present=True),
            EdgeRouteConstraint(kind="edge_route", from_subject=second, edge_type="on_success", to_subject=target, present=False),
            plugin_cap,
        ),
    )

    # A schema whose ``mode`` proves both the scalar literals and the
    # descendant ``level`` path, so every declined set passes individual
    # option validation and the verdict is the conjunction checker's alone.
    nested_mode_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "mode": {
                "enum": ["safe", "strict"],
                "properties": {"level": {"enum": ["strict", "lax"]}},
            }
        },
    }
    catalog = _view({("transform", "normalize"): nested_mode_schema})

    for constraints in declined_sets:
        action = _action(*constraints, catalog_kind="transform", catalog_name="normalize")
        result = _validate(action, catalog=catalog)
        assert result == DeferredIntentAccepted(action=action), constraints


# --- The ADR-033 aliasing residual window and the source/sink asymmetry ---


def _reviewed_csv_source(name: str) -> SourceResolved:
    return SourceResolved(
        name=name,
        plugin="csv",
        options={},
        observed_columns=("amount",),
        sample_rows=(),
        on_validation_failure="discard",
    )


def _csv_option(subject_id: str, delimiter: str) -> OptionValueConstraint:
    return _option(
        _plugin_subject(kind="source", name="csv", subject_id=subject_id),
        path=("delimiter",),
        value=delimiter,
    )


def test_csv_alias_residual_window_fuses_option_constraints_before_second_source_reviewed() -> None:
    """ADR-033 recorded residual risk: the fixture decides the fusion is correct.

    With exactly one reviewed csv source, two option constraints stated
    against distinct plugin-subject hints both alias to that source and fuse:
    an equals mismatch rejects.  Coverage applies the same single-match
    resolution rule — once a second csv source exists the subjects resolve
    ambiguous and coverage fails every candidate — so a set fused at admission
    under a unique match is genuinely unsatisfiable at coverage.
    """

    source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    one_reviewed = replace(
        GuidedSession.initial(),
        source_order=(source_id,),
        reviewed_sources={source_id: _reviewed_csv_source("events")},
        deferred_intents=(_intent(_csv_option("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", ";")),),
    )
    action = _action(_csv_option("cccccccc-cccc-4ccc-8ccc-cccccccccccc", ","), catalog_kind="transform", catalog_name="normalize")

    fused = _validate(action, guided=one_reviewed)

    rejection = _assert_contradiction(fused, rule="conflicting_subject_facts")
    assert rejection.contradiction is not None
    assert rejection.contradiction.conflicting_intent_id == INTENT_ID

    # Once a second csv source is reviewed the single-match window closes:
    # the hints stay existential and the same conjunction is admitted.
    second_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    two_reviewed = replace(
        one_reviewed,
        source_order=(source_id, second_id),
        reviewed_sources={
            source_id: _reviewed_csv_source("events"),
            second_id: _reviewed_csv_source("archive_events"),
        },
    )

    admitted = _validate(action, guided=two_reviewed)

    assert admitted == DeferredIntentAccepted(action=action)


def test_source_sink_aliasing_asymmetry_transform_hints_never_fuse() -> None:
    """The asymmetry is deliberate: transform identities stay existential.

    The same two-constraint shape that fuses (and rejects) for a uniquely
    reviewed csv source stays admitted for transforms, because coverage keeps
    future-transform identities existential and may resolve each hint to a
    different node.
    """

    guided = replace(
        GuidedSession.initial(),
        deferred_intents=(_intent(_option(_plugin_subject(subject_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), value="safe")),),
    )
    action = _action(
        _option(_plugin_subject(subject_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"), value="strict"),
        catalog_kind="transform",
        catalog_name="normalize",
    )

    result = _validate(action, guided=guided)

    assert result == DeferredIntentAccepted(action=action)


# --- Exhaustive small-model soundness oracle (ADR-033 verification) ---

_NODE_A_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_NODE_B_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_SOURCE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_OUTPUT_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

_GATE_CONDITIONS = {
    "gate_amount": "row['amount'] > 500",
    "gate_risk": "row['risk_score'] > 80",
}


def _oracle_reviewed_guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        source_order=(_SOURCE_ID,),
        reviewed_sources={
            _SOURCE_ID: SourceResolved(
                name="events",
                plugin="csv",
                options={},
                observed_columns=("amount", "risk_score"),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(_OUTPUT_ID,),
        reviewed_outputs={
            _OUTPUT_ID: SinkOutputResolved(
                name="results",
                plugin="json",
                options={},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )


def _oracle_node(node_id: str, variant: str, input_connection: str, on_success: str | None) -> NodeSpec:
    if variant in _GATE_CONDITIONS:
        return NodeSpec(
            id=node_id,
            node_type="gate",
            plugin=None,
            input=input_connection,
            on_success=None,
            on_error=None,
            options={},
            condition=_GATE_CONDITIONS[variant],
            routes={"true": "results", "false": "results"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    plugin, mode = (
        ("normalize", "safe") if variant == "norm_safe" else ("normalize", "strict") if variant == "norm_strict" else ("llm", None)
    )
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin=plugin,
        input=input_connection,
        on_success=on_success,
        on_error=None,
        options={"mode": mode} if mode is not None else {},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _oracle_candidates() -> tuple[CompositionState, ...]:
    """Schema-honest small candidates: gates carry no plugin, normalize modes
    stay inside the validated enum, and reviewed identities bind by name."""

    a_variants = (None, "norm_safe", "norm_strict", "llm", "gate_amount", "gate_risk")
    b_variants = (None, "norm_safe", "norm_strict", "gate_amount")
    candidates: list[CompositionState] = []
    for a_variant in a_variants:
        for b_variant in b_variants:
            nodes: list[NodeSpec] = []
            if a_variant is None and b_variant is None:
                source_success = "results"
            else:
                source_success = "c1"
            if a_variant is not None:
                a_success = "c2" if b_variant is not None else "results"
                nodes.append(_oracle_node(_NODE_A_ID, a_variant, "c1", a_success))
                b_input = "c2"
            else:
                b_input = "c1"
            if b_variant is not None:
                nodes.append(_oracle_node(_NODE_B_ID, b_variant, b_input, "results"))
            candidates.append(
                CompositionState(
                    nodes=tuple(nodes),
                    edges=(),
                    outputs=(OutputSpec(name="results", plugin="json", options={}, on_write_failure="discard"),),
                    metadata=PipelineMetadata(),
                    version=1,
                    sources={
                        "events": SourceSpec(
                            plugin="csv",
                            on_success=source_success,
                            options={},
                            on_validation_failure="discard",
                        )
                    },
                )
            )
    return tuple(candidates)


def _oracle_atoms() -> tuple[DeferredConstraint, ...]:
    norm_a = _plugin_subject(subject_id=_NODE_A_ID)
    norm_b = _plugin_subject(subject_id=_NODE_B_ID)
    llm_a = _plugin_subject(name="llm", subject_id=_NODE_A_ID)
    stable_node = _stable("node", _NODE_A_ID)
    stable_source = _stable("source", _SOURCE_ID)
    return (
        _presence(norm_a, True),
        _presence(norm_b, False),
        _presence(llm_a, True),
        _presence(stable_node, True),
        _presence(stable_node, False),
        _option(norm_a, value="safe"),
        _option(norm_a, value="strict"),
        _option(norm_a, operator="not_equals", value="safe"),
        _option(norm_a, operator="not_equals", value="strict"),
        _option(norm_b, value="strict"),
        _count("node", "at_most", 1),
        _count("node", "at_most", 0),
        _count("node", "at_least", 1, plugin_kind="transform", plugin_name="normalize"),
        _count("node", "at_most", 0, plugin_kind="transform", plugin_name="normalize"),
        _predicate(stable_source),
        _predicate(stable_source, column="risk_score", value=80),
        _predicate(stable_node),
    )


def test_checker_never_rejects_a_satisfiable_small_model_and_every_rejection_names_a_closed_rule() -> None:
    """ADR-033 normative verification: exhaustive small-model soundness.

    Enumerate small constraint models, brute-force their satisfiability over
    schema-honest small candidates via the public ``constraint_holds``
    coverage semantics, and assert the checker never rejects a satisfiable
    model and that every rejection names a closed rule.  The checker is sound
    and deliberately incomplete: admitted-but-unsatisfiable models are
    expected and are caught fail-closed at wire confirmation.
    """

    reviewed = _oracle_reviewed_guided()
    candidates = _oracle_candidates()
    atoms = _oracle_atoms()
    models: list[tuple[DeferredConstraint, ...]] = [(atom,) for atom in atoms]
    models.extend((first, second) for first, second in combinations(atoms, 2))
    triple_core = (atoms[0], atoms[2], atoms[3], atoms[5], atoms[6], atoms[10], atoms[12], atoms[14], atoms[16])
    models.extend(tuple(model) for model in combinations(triple_core, 3))

    rejected_count = 0
    admitted_satisfiable_count = 0
    for model in models:
        guided = replace(reviewed, deferred_intents=(_intent(*model),))
        result = _validate(_benign_action(), guided=guided)
        satisfiable = any(all(constraint_holds(candidate, reviewed, constraint) for constraint in model) for candidate in candidates)
        if type(result) is DeferredIntentRejected:
            assert result.reason == "constraint_contradiction", (model, result)
            assert result.contradiction is not None, model
            assert result.contradiction.rule in _CLOSED_RULES, (model, result.contradiction.rule)
            rejected_count += 1
            assert not satisfiable, (
                "checker rejected a satisfiable model",
                model,
                result.contradiction.rule,
            )
        else:
            assert type(result) is DeferredIntentAccepted, (model, result)
            if satisfiable:
                admitted_satisfiable_count += 1

    # The oracle must not be vacuous: it exercises both verdicts.
    assert rejected_count > 0
    assert admitted_satisfiable_count > 0
