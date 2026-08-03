"""Closed creation boundary for guided wrong-stage intent."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSummary
from elspeth.web.composer.guided import intent_management as intent_management_module
from elspeth.web.composer.guided.deferred_intents import (
    DeferredIntentAccepted,
    DeferredIntentAction,
    DeferredIntentCancelAction,
    DeferredIntentEditAction,
    DeferredIntentManagementActionShapeError,
    DeferredIntentRejected,
    DeferredIntentUnsupported,
    create_deferred_stage_intent,
    deferred_intent_action_from_dict,
    deferred_intent_management_action_from_dict,
    validate_deferred_intent_action,
)
from elspeth.web.composer.guided.errors import GuidedSolverResponseShapeError, InvariantError
from elspeth.web.composer.guided.intent_management import (
    DeferredIntentManagementApplied,
    resolve_deferred_intent_management,
    schema8_deferred_management_rewind_step,
)
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.stage_subjects import (
    ComponentCountConstraint,
    EdgeRouteConstraint,
    OptionValueConstraint,
    PluginSubject,
    StableSubject,
    StatedGateRoutingConstraint,
    StatedPredicateConstraint,
    SubjectPresenceConstraint,
)
from elspeth.web.composer.guided.state_machine import DeferredStageIntent, GuidedSession
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailability, PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason

INTENT_ID = "11111111-1111-4111-8111-111111111111"
MESSAGE_ID = "22222222-2222-4222-8222-222222222222"
SELECTION_TOKEN = "server-selection-token"


@pytest.mark.parametrize("action_type", [DeferredIntentCancelAction, DeferredIntentEditAction])
def test_management_selection_token_is_a_required_constructor_field(action_type: type[object]) -> None:
    assert inspect.signature(action_type).parameters["selection_token"].default is inspect.Parameter.empty


class _Catalog:
    def __init__(
        self,
        plugins: tuple[tuple[PluginKind, str], ...],
        schemas: dict[tuple[PluginKind, str], dict[str, object]] | None = None,
        schema_overrides: dict[tuple[PluginKind, str], PluginSchemaInfo] | None = None,
    ) -> None:
        self._plugins = plugins
        self._schemas = schemas or {}
        self._schema_overrides = schema_overrides or {}

    def _list(self, kind: PluginKind) -> list[PluginSummary]:
        return [
            PluginSummary(name=name, description=name, plugin_type=kind, config_fields=[])
            for plugin_kind, name in self._plugins
            if plugin_kind == kind
        ]

    def list_sources(self) -> list[PluginSummary]:
        return self._list("source")

    def list_transforms(self) -> list[PluginSummary]:
        return self._list("transform")

    def list_sinks(self) -> list[PluginSummary]:
        return self._list("sink")

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        overridden = self._schema_overrides.get((plugin_type, name))
        if overridden is not None:
            return overridden
        json_schema = self._schemas.get((plugin_type, name))
        if json_schema is None:
            raise AssertionError("deferred intent validation must inspect schemas only for option-value constraints")
        return PluginSchemaInfo(
            name=name,
            plugin_type=plugin_type,
            description=name,
            json_schema=json_schema,
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
        raise AssertionError("deferred intent validation must not dispatch plugins")


def _view(
    installed: tuple[tuple[PluginKind, str], ...],
    *,
    available: frozenset[PluginId] | None = None,
    schemas: dict[tuple[PluginKind, str], dict[str, object]] | None = None,
    schema_overrides: dict[tuple[PluginKind, str], PluginSchemaInfo] | None = None,
) -> PolicyCatalogView:
    permitted = frozenset(PluginId(kind, name) for kind, name in installed) if available is None else available
    unavailable = tuple(
        PluginAvailability(plugin_id=PluginId(kind, name), reason=PluginUnavailableReason.NOT_AUTHORIZED)
        for kind, name in installed
        if PluginId(kind, name) not in permitted
    )
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:test",
        available=permitted,
        unavailable=unavailable,
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(_Catalog(installed, schemas, schema_overrides), snapshot, profiles=None)  # type: ignore[arg-type]


def _real_json_view() -> PolicyCatalogView:
    available = frozenset({PluginId("source", "json"), PluginId("sink", "json")})
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:test",
        available=available,
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(create_catalog_service(), snapshot, profiles=None)


def _real_text_view() -> PolicyCatalogView:
    available = frozenset({PluginId("source", "text"), PluginId("sink", "text")})
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:test",
        available=available,
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(create_catalog_service(), snapshot, profiles=None)


def _action(
    *,
    target_stage: str = "topology",
    catalog_kind: str | None = "transform",
    catalog_name: str | None = "llm",
) -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage=target_stage,  # type: ignore[arg-type]
        catalog_kind=catalog_kind,  # type: ignore[arg-type]
        catalog_name=catalog_name,
        redacted_summary="Use the named transform in the topology stage.",
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind="node",
                plugin_kind="transform",
                plugin_name="llm",
                operator="at_least",
                count=1,
            ),
        ),
    )


def _option_action(
    *,
    subject: PluginSubject | StableSubject,
    option_path: tuple[str, ...],
    value: object,
    target_stage: str = "topology",
    catalog_kind: str = "transform",
    catalog_name: str = "llm",
) -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage=target_stage,  # type: ignore[arg-type]
        catalog_kind=catalog_kind,  # type: ignore[arg-type]
        catalog_name=catalog_name,
        redacted_summary="Retain one closed catalog option value.",
        constraints=(
            OptionValueConstraint(
                kind="option_value",
                subject=subject,
                option_path=option_path,
                operator="equals",
                value=value,  # type: ignore[arg-type]
            ),
        ),
    )


_TRANSFORM_OPTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "mode": {"enum": ["safe", "strict"]},
        "fixed": {"const": "locked"},
        "nested": {"$ref": "#/$defs/NestedOptions"},
    },
    "$defs": {
        "NestedOptions": {
            "type": "object",
            "properties": {"flavor": {"enum": ["vanilla", "chocolate"]}},
        }
    },
}


def test_action_is_frozen_exact_and_decoder_rejects_open_or_coerced_shapes() -> None:
    action = _action()
    with pytest.raises(FrozenInstanceError):
        action.redacted_summary = "changed"  # type: ignore[misc]

    encoded = {
        "target_stage": "topology",
        "catalog_kind": "transform",
        "catalog_name": "llm",
        "redacted_summary": "Use the named transform in the topology stage.",
        "constraints": [constraint.to_dict() for constraint in action.constraints],
    }
    assert deferred_intent_action_from_dict(encoded) == action
    with pytest.raises(GuidedSolverResponseShapeError, match="unexpected keys"):
        deferred_intent_action_from_dict({**encoded, "raw_user_message": "secret"})
    with pytest.raises(GuidedSolverResponseShapeError, match="constraints must be a list"):
        deferred_intent_action_from_dict({**encoded, "constraints": tuple(encoded["constraints"])})
    with pytest.raises(InvariantError, match="catalog fields must be paired"):
        _action(catalog_kind=None, catalog_name="llm")
    with pytest.raises(InvariantError, match="at least one structural constraint"):
        DeferredIntentAction(
            target_stage="topology",
            catalog_kind="transform",
            catalog_name="llm",
            redacted_summary="Use the named transform in the topology stage.",
            constraints=(),
        )


@pytest.mark.parametrize(
    ("option_path", "value"),
    [(("mode",), "safe"), (("fixed",), "locked"), (("nested", "flavor"), "chocolate")],
)
def test_option_value_literal_is_accepted_only_when_public_schema_proves_closed_membership(
    option_path: tuple[str, ...],
    value: object,
) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=option_path,
        value=value,
    )
    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): _TRANSFORM_OPTION_SCHEMA}),
        guided=GuidedSession.initial(),
    )
    assert result == DeferredIntentAccepted(action=action)


@pytest.mark.parametrize(
    ("plugin_kind", "option_path", "value"),
    [
        ("source", ("format",), "json"),
        ("source", ("format",), None),
        ("sink", ("format",), "jsonl"),
        ("sink", ("format",), None),
        ("sink", ("collision_policy",), "auto_increment"),
        ("sink", ("collision_policy",), None),
    ],
)
def test_real_json_plugin_pydantic_nullable_enums_are_finite_closed_domains(
    plugin_kind: PluginKind,
    option_path: tuple[str, ...],
    value: object,
) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind=plugin_kind,
            plugin_name="json",
        ),
        option_path=option_path,
        value=value,
        target_stage="output",
        catalog_kind="sink",
        catalog_name="json",
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_real_json_view(),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentAccepted(action=action)


@pytest.mark.parametrize(
    ("plugin_kind", "option_path", "value"),
    [
        ("source", ("format",), "csv"),
        ("sink", ("format",), "csv"),
        ("sink", ("collision_policy",), "overwrite"),
    ],
)
def test_real_json_plugin_pydantic_nullable_enums_reject_values_outside_the_domain(
    plugin_kind: PluginKind,
    option_path: tuple[str, ...],
    value: object,
) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind=plugin_kind,
            plugin_name="json",
        ),
        option_path=option_path,
        value=value,
        target_stage="output",
        catalog_kind="sink",
        catalog_name="json",
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_real_json_view(),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentRejected(reason="option_value_unproven")


def test_real_text_source_pydantic_boolean_option_is_a_finite_closed_domain() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="source",
            plugin_name="text",
        ),
        option_path=("strip_whitespace",),
        value=False,
        target_stage="output",
        catalog_kind="sink",
        catalog_name="text",
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_real_text_view(),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentAccepted(action=action)


@pytest.mark.parametrize(
    ("declared_types", "value", "accepted"),
    [
        (["null"], None, True),
        (["boolean", "null"], False, True),
        (["boolean", "null"], True, True),
        (["boolean", "null"], None, True),
        (["boolean", "null"], 0, False),
        (["boolean", "string"], False, False),
    ],
)
def test_only_boolean_and_null_type_arrays_have_finite_exact_scalar_domains(
    declared_types: list[str],
    value: object,
    accepted: bool,
) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=value,
    )
    schema = {"type": "object", "properties": {"mode": {"type": declared_types}}}

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    expected = DeferredIntentAccepted(action=action) if accepted else DeferredIntentRejected(reason="option_value_unproven")
    assert result == expected


def test_nested_local_ref_and_union_of_only_finite_branches_is_accepted() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"nested": {"$ref": "#/$defs/Nested"}},
        "$defs": {
            "Nested": {
                "type": "object",
                "properties": {
                    "mode": {
                        "oneOf": [
                            {"$ref": "#/$defs/Mode"},
                            {"type": "null"},
                        ]
                    }
                },
            },
            "Mode": {"enum": ["safe", "strict"], "type": "string"},
        },
    }
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("nested", "mode"),
        value=None,
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentAccepted(action=action)


@pytest.mark.parametrize(("schema_value", "constraint_value"), [(True, True), (1, 1)])
def test_finite_domain_uses_exact_type_and_value_identity(schema_value: object, constraint_value: object) -> None:
    schema = {"type": "object", "properties": {"mode": {"anyOf": [{"enum": [True]}, {"enum": [1]}]}}}
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=constraint_value,
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentAccepted(action=action)
    assert type(schema_value) is type(constraint_value)


def test_finite_domain_does_not_coerce_bool_to_integer() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=True,
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view(
            (("transform", "llm"),),
            schemas={("transform", "llm"): {"type": "object", "properties": {"mode": {"enum": [1]}}}},
        ),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentRejected(reason="option_value_unproven")


def test_union_with_one_free_form_branch_has_no_proven_finite_domain() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    schema = {
        "type": "object",
        "properties": {"mode": {"anyOf": [{"enum": ["safe"]}, {"type": "string"}]}},
    }

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentRejected(reason="option_value_unproven")


@pytest.mark.parametrize(
    ("option_schema", "value", "accepted"),
    [
        ({"oneOf": [{"const": "x"}, {"enum": ["x", "y"]}]}, "x", False),
        ({"oneOf": [{"const": "x"}, {"enum": ["x", "y"]}]}, "y", True),
        ({"enum": ["x", "y"], "not": {"const": "x"}}, "x", False),
        ({"enum": ["x", "y"], "not": {"const": "x"}}, "y", True),
        ({"enum": [1, 2], "allOf": [{"minimum": 2}]}, 1, False),
        ({"enum": [1, 2], "allOf": [{"minimum": 2}]}, 2, True),
    ],
)
def test_finite_discovery_is_filtered_by_full_draft_2020_12_semantics(
    option_schema: dict[str, object],
    value: object,
    accepted: bool,
) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=value,
    )
    schema = {"type": "object", "properties": {"mode": option_schema}}

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    expected = DeferredIntentAccepted(action=action) if accepted else DeferredIntentRejected(reason="option_value_unproven")
    assert result == expected


@pytest.mark.parametrize(("value", "accepted"), [("x", False), ("y", True)])
def test_all_of_can_supply_a_finite_candidate_domain_while_full_validation_applies_not(value: str, accepted: bool) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=value,
    )
    schema = {
        "type": "object",
        "properties": {"mode": {"allOf": [{"enum": ["x", "y"]}, {"not": {"const": "x"}}]}},
    }

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    expected = DeferredIntentAccepted(action=action) if accepted else DeferredIntentRejected(reason="option_value_unproven")
    assert result == expected


@pytest.mark.parametrize(("value", "accepted"), [("safe", True), ("other", False)])
def test_finite_sibling_enum_bounds_an_otherwise_infinite_local_ref(value: str, accepted: bool) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=value,
    )
    schema = {
        "$defs": {"String": {"type": "string"}},
        "type": "object",
        "properties": {"mode": {"$ref": "#/$defs/String", "enum": ["safe"]}},
    }

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    expected = DeferredIntentAccepted(action=action) if accepted else DeferredIntentRejected(reason="option_value_unproven")
    assert result == expected


def test_dangling_ref_in_restrictive_schema_is_integrity_failure_before_literal_membership() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="other",
    )
    schema = {
        "type": "object",
        "properties": {"mode": {"enum": ["safe"], "not": {"$ref": "#/$defs/Missing"}}},
    }

    with pytest.raises(InvariantError, match="schema"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize("value", ["safe", "other"])
def test_dynamic_ref_is_rejected_before_literal_membership(value: str) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8335-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value=value,
    )
    schema = {
        "type": "object",
        "properties": {"mode": {"enum": ["safe"], "not": {"$dynamicRef": "#missing"}}},
    }

    with pytest.raises(InvariantError, match="dynamicRef"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize(("value", "accepted"), [("inner-safe", True), ("root-safe", False)])
def test_nested_resource_ref_uses_its_own_base_for_domain_and_validation(value: str, accepted: bool) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8335-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("nested", "mode"),
        value=value,
    )
    schema = {
        "$defs": {"Mode": {"enum": ["root-safe"]}},
        "type": "object",
        "properties": {
            "nested": {
                "$id": "outer",
                "$defs": {"Mode": {"enum": ["inner-safe"]}},
                "type": "object",
                "properties": {"mode": {"$ref": "#/$defs/Mode"}},
            }
        },
    }

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    expected = DeferredIntentAccepted(action=action) if accepted else DeferredIntentRejected(reason="option_value_unproven")
    assert result == expected


def test_option_path_ref_keeps_nested_resource_base() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8335-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("nested", "config", "mode"),
        value="inner-safe",
    )
    schema = {
        "$defs": {
            "Nested": {"type": "object", "properties": {"mode": {"enum": ["root-safe"]}}},
        },
        "type": "object",
        "properties": {
            "nested": {
                "$id": "outer",
                "$defs": {
                    "Nested": {"type": "object", "properties": {"mode": {"enum": ["inner-safe"]}}},
                },
                "type": "object",
                "properties": {"config": {"$ref": "#/$defs/Nested"}},
            }
        },
    }

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentAccepted(action=action)


def test_dangling_ref_in_nested_resource_is_not_masked_by_root_definition() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8335-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("nested", "mode"),
        value="root-safe",
    )
    schema = {
        "$defs": {"Mode": {"enum": ["root-safe"]}},
        "type": "object",
        "properties": {
            "nested": {
                "$id": "outer",
                "type": "object",
                "properties": {"mode": {"$ref": "#/$defs/Mode"}},
            }
        },
    }

    with pytest.raises(InvariantError, match="schema"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize(
    "option_schema",
    [
        {"$ref": "#/$defs/Missing"},
        {"$ref": 7},
        {"$ref": "external.json#/$defs/Mode"},
        {"anyOf": "not-a-branch-list"},
        {"oneOf": [{"enum": ["safe"]}, "not-a-schema"]},
        {"enum": "not-an-enum-list"},
        {"enum": []},
        {"enum": ["safe"], "not": None},
        None,
    ],
)
def test_malformed_ref_union_or_closed_domain_declaration_is_authority_corruption(option_schema: object) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    schema = {"type": "object", "properties": {"mode": option_schema}}

    with pytest.raises(InvariantError, match="schema"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize("option_schema", [True, False])
def test_boolean_property_schemas_are_valid_but_do_not_authorize_a_retained_literal(option_schema: bool) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    schema = {"type": "object", "properties": {"mode": option_schema}}

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentRejected(reason="option_value_unproven")


@pytest.mark.parametrize("properties", [None, True, []])
def test_present_malformed_properties_declaration_is_authority_corruption(properties: object) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    schema = {"type": "object", "properties": properties}

    with pytest.raises(InvariantError, match="schema"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
            guided=GuidedSession.initial(),
        )


def test_cyclic_local_ref_is_authority_corruption() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"mode": {"$ref": "#/$defs/A"}},
        "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
    }

    with pytest.raises(InvariantError, match="schema"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): schema}),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize(
    "schema_info",
    [
        PluginSchemaInfo(
            name="other",
            plugin_type="transform",
            description="wrong name",
            json_schema={"type": "object", "properties": {"mode": {"enum": ["safe"]}}},
            knob_schema={"fields": []},
        ),
        PluginSchemaInfo(
            name="llm",
            plugin_type="sink",
            description="wrong kind",
            json_schema={"type": "object", "properties": {"mode": {"enum": ["safe"]}}},
            knob_schema={"fields": []},
        ),
    ],
)
def test_catalog_schema_identity_mismatch_is_authority_corruption(schema_info: PluginSchemaInfo) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )

    with pytest.raises(InvariantError, match="schema"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view(
                (("transform", "llm"),),
                schema_overrides={("transform", "llm"): schema_info},
            ),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize("root", [None, True, []])
def test_catalog_schema_root_must_be_an_object_schema(root: object) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    schema_info = PluginSchemaInfo.model_construct(
        name="llm",
        plugin_type="transform",
        description="malformed root",
        json_schema=root,
        knob_schema={"fields": []},
    )

    with pytest.raises(InvariantError, match="schema root"):
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view(
                (("transform", "llm"),),
                schema_overrides={("transform", "llm"): schema_info},
            ),
            guided=GuidedSession.initial(),
        )


@pytest.mark.parametrize(
    ("option_path", "value"),
    [
        (("mode",), "send the full secret sentence to an arbitrary destination"),
        (("mode",), "unknown-mode"),
        (("missing",), "safe"),
        (("nested", "missing"), "vanilla"),
    ],
)
def test_option_value_free_form_wrong_enum_and_unresolved_paths_are_rejected(
    option_path: tuple[str, ...],
    value: object,
) -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="transform",
            plugin_name="llm",
        ),
        option_path=option_path,
        value=value,
    )
    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): _TRANSFORM_OPTION_SCHEMA}),
        guided=GuidedSession.initial(),
    )
    assert result == DeferredIntentRejected(reason="option_value_unproven")


def test_option_value_wrong_plugin_kind_is_rejected_before_schema_lookup() -> None:
    action = _option_action(
        subject=PluginSubject(
            kind="plugin",
            subject_id="33333333-3333-4333-8333-333333333333",
            plugin_kind="sink",
            plugin_name="llm",
        ),
        option_path=("mode",),
        value="safe",
    )
    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), schemas={("transform", "llm"): _TRANSFORM_OPTION_SCHEMA}),
        guided=GuidedSession.initial(),
    )
    assert result == DeferredIntentRejected(reason="catalog_kind_mismatch")


def test_stable_option_subject_requires_reviewed_guided_plugin_authority_then_uses_same_schema_rule() -> None:
    stable_id = "44444444-4444-4444-8444-444444444444"
    reviewed = replace(
        GuidedSession.initial(),
        output_order=(stable_id,),
        reviewed_outputs={
            stable_id: SinkOutputResolved(
                name="main",
                plugin="json",
                options={},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )
    action = _option_action(
        subject=StableSubject(kind="stable", component_kind="output", stable_id=stable_id),
        option_path=("mode",),
        value="safe",
        target_stage="output",
        catalog_kind="sink",
        catalog_name="json",
    )
    catalog = _view(
        (("sink", "json"),),
        schemas={("sink", "json"): {"type": "object", "properties": {"mode": {"enum": ["safe"]}}}},
    )

    accepted = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=catalog,
        guided=reviewed,
    )
    unresolved = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=catalog,
        guided=GuidedSession.initial(),
    )
    assert accepted == DeferredIntentAccepted(action=action)
    assert unresolved == DeferredIntentRejected(reason="option_value_unproven")


def test_live_unique_catalog_identity_is_accepted_only_at_its_responsible_later_stage() -> None:
    result = validate_deferred_intent_action(
        _action(),
        receiving_stage="source",
        catalog=_view((("transform", "llm"),)),
        guided=GuidedSession.initial(),
    )
    assert result == DeferredIntentAccepted(action=_action())

    wrong_target = validate_deferred_intent_action(
        _action(target_stage="output"),
        receiving_stage="source",
        catalog=_view((("transform", "llm"),)),
        guided=GuidedSession.initial(),
    )
    assert wrong_target == DeferredIntentRejected(reason="wrong_responsible_stage")


def test_stated_predicate_is_a_topology_constraint_without_plugin_option_schema_authority() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated amount predicate at the topology stage.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content="Later apply a gate where csv amount is greater than 500.",
    )

    assert result == DeferredIntentAccepted(action=action)


def test_stated_gate_routing_is_a_topology_constraint_with_closed_future_output_names() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Route amount-threshold rows to the two named outputs.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=(
            "This is an orders CSV. Later on I want a gate that routes rows with amount greater than 500 to a "
            "high_value JSON sink, and everything else to a standard JSON sink. Every row must land in exactly one of them."
        ),
    )

    assert result == DeferredIntentAccepted(action=action)


@pytest.mark.parametrize(
    ("value", "true_target", "false_target"),
    (
        (999, "high_value", "standard"),
        (500, "standard", "high_value"),
    ),
)
def test_stated_gate_routing_rejects_solver_facts_not_grounded_in_the_operator_message(
    value: int,
    true_target: str,
    false_target: str,
) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Route the stated threshold.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=value,
                true_target=true_target,
                false_target=false_target,
            ),
        ),
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=("Later route rows with amount greater than 500 to high_value, and everything else to standard."),
    )

    assert result == DeferredIntentRejected(reason="stated_fact_unproven")


def test_condition_only_constraint_cannot_underrepresent_an_explicit_routing_instruction() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated amount predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=("Later route rows with amount greater than 500 to high_value, and everything else to standard."),
    )

    assert result == DeferredIntentRejected(reason="stated_fact_unproven")


def test_grounding_rejects_broad_equals_and_negated_route_false_accepts() -> None:
    subject = PluginSubject(
        kind="plugin",
        subject_id="33333333-3333-4333-8333-333333333333",
        plugin_kind="source",
        plugin_name="csv",
    )
    actions = (
        (
            DeferredIntentAction(
                target_stage="topology",
                catalog_kind=None,
                catalog_name=None,
                redacted_summary="Wrong equality.",
                constraints=(
                    StatedPredicateConstraint(
                        kind="stated_predicate",
                        subject=subject,
                        column="amount",
                        operator="equals",
                        value="greater",
                    ),
                ),
            ),
            "amount is greater than 500",
        ),
        (
            DeferredIntentAction(
                target_stage="topology",
                catalog_kind=None,
                catalog_name=None,
                redacted_summary="Negated target.",
                constraints=(
                    StatedGateRoutingConstraint(
                        kind="stated_gate_routing",
                        subject=subject,
                        column="amount",
                        operator="greater_than",
                        value=500,
                        true_target="high_value",
                        false_target="standard",
                    ),
                ),
            ),
            "amount greater than 500 not to high_value but to manual_review, and everything else to standard",
        ),
    )

    for action, message in actions:
        assert validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=_view((("source", "csv"),)),
            guided=GuidedSession.initial(),
            originating_message_content=message,
        ) == DeferredIntentRejected(reason="stated_fact_unproven")


def _stated_gate_routing_action_for_grounding() -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )


@pytest.mark.parametrize(
    "message",
    (
        "Later do not apply a gate where csv amount is greater than 500.",
        "Later route csv rows with amount greater than 500; never send them to high_value, and everything else to standard.",
    ),
)
def test_stated_grounding_rejects_semantic_negation_outside_the_matched_tokens(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Route the stated threshold.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later add a gate that routes csv rows with amount > 500 to high_value, and every other row to standard.",
        "Later send csv rows where amount > 500 into high_value; the rest go into standard.",
        "Later send csv rows where amount > 500 to high_value; the rest go to standard.",
        "Later route csv rows where amount > 500 into high_value, with remaining rows landing in standard.",
        "Later route csv rows where status equals priority to high_value, and everything else to standard.",
        "Later route csv rows where amount exceeds 500 to high_value, and everything else to standard.",
        "Later route csv rows where amount is higher than 500 to high_value, and everything else to standard.",
        "Later route csv rows where amount is between 500 and 1000 to high_value, and everything else to standard.",
        "Later route csv rows where status is priority to high_value, and everything else to standard.",
    ),
)
def test_explicit_gate_routing_prose_cannot_be_retained_as_a_weaker_constraint_kind(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary="Preserve a source during topology authoring.",
        constraints=(
            SubjectPresenceConstraint(
                kind="subject_presence",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                present=True,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"), ("transform", "passthrough"))),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later add a gate for csv rows whose amount exceeds 500.",
        "Later add a gate for csv rows whose amount is higher than 500.",
        "Later add a gate for csv rows whose amount is between 500 and 1000.",
        "Later add a gate for csv rows where status is priority.",
        "Later add a gate which routes csv rows with amount exceeding 500 to high_value.",
        "Later route csv rows with amount exceeding 500 to high_value.",
    ),
)
def test_explicit_condition_only_gate_prose_cannot_be_retained_as_a_weaker_constraint_kind(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary="Preserve a source during topology authoring.",
        constraints=(
            SubjectPresenceConstraint(
                kind="subject_presence",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                present=True,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"), ("transform", "passthrough"))),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later add a gate that routes csv rows with amount greater than 500 to high_value.",
        "Later send csv rows with amount greater than 500 to high_value.",
    ),
)
def test_condition_only_constraint_cannot_drop_an_explicit_single_branch_destination(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="33333333-3333-4333-8333-333333333333",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later add a gate only if approved that routes csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate if the owner approves that routes csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate provided the owner agrees that routes csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate subject to security review that routes csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate after the change is signed off that routes csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate only if approved and route csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later add a gate only if approved then route csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate only if approved but route csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later add a gate only if approved while route csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate only if approved also route csv rows with amount greater than 500 to high_value, "
        "and everything else to standard.",
        "Later add a gate only if approved; route csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later add a gate only if approved: route csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Only after security review. Later route csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Pending owner sign-off. Later route csv rows with amount greater than 500 to high_value, and everything else to standard.",
    ),
)
def test_stated_grounding_rejects_unrepresented_authority_preconditions(message: str) -> None:
    assert validate_deferred_intent_action(
        _stated_gate_routing_action_for_grounding(),
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize("row_qualifier", ("priority", "authorized", "pending"))
def test_stated_grounding_rejects_unrepresented_row_qualifier(row_qualifier: str) -> None:
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=("77777777-7777-4777-8777-777777777777",),
        reviewed_sources={
            "77777777-7777-4777-8777-777777777777": SourceResolved(
                name="orders",
                plugin="csv",
                options={"path": "/data/orders.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
    )
    assert validate_deferred_intent_action(
        _stated_gate_routing_action_for_grounding(),
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=guided,
        originating_message_content=(
            f"Later add a gate for {row_qualifier} rows where amount greater than 500 to high_value, and everything else to standard."
        ),
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_grounding_accepts_exact_live_stable_subject_name_with_ambiguous_plugins() -> None:
    orders_id = "77777777-7777-4777-8777-777777777777"
    returns_id = "88888888-8888-4888-8888-888888888888"
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=(orders_id, returns_id),
        reviewed_sources={
            orders_id: SourceResolved(
                name="orders",
                plugin="csv",
                options={"path": "/data/orders.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
            returns_id: SourceResolved(
                name="returns",
                plugin="csv",
                options={"path": "/data/returns.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
        },
    )
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=StableSubject(kind="stable", component_kind="source", stable_id=orders_id),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=guided,
        originating_message_content=(
            "Later route orders rows with amount greater than 500 to high_value, and everything else to standard."
        ),
    ) == DeferredIntentAccepted(action=action)


def test_stated_constraint_rejects_nonexistent_stable_subject_even_when_text_matches() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=StableSubject(
                    kind="stable",
                    component_kind="source",
                    stable_id="99999999-9999-4999-8999-999999999999",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view(()),
        guided=GuidedSession.initial(),
        originating_message_content="Later apply a gate where amount is greater than 500.",
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_plugin_subject_rejects_ambiguous_same_plugin_sources() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )
    source_ids = (
        "77777777-7777-4777-8777-777777777777",
        "88888888-8888-4888-8888-888888888888",
    )
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=source_ids,
        reviewed_sources={
            stable_id: SourceResolved(
                name=f"source_{index}",
                plugin="csv",
                options={"path": f"/data/source_{index}.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            )
            for index, stable_id in enumerate(source_ids, start=1)
        },
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=guided,
        originating_message_content="Later apply a gate where csv amount is greater than 500.",
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_plugin_subject_rejects_exact_live_id_when_message_names_another_source() -> None:
    csv_id = "77777777-7777-4777-8777-777777777777"
    json_id = "88888888-8888-4888-8888-888888888888"
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=(csv_id, json_id),
        reviewed_sources={
            csv_id: SourceResolved(
                name="csv_source",
                plugin="csv",
                options={"path": "/data/input.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
            json_id: SourceResolved(
                name="json_source",
                plugin="json",
                options={"path": "/data/input.jsonl"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
        },
    )
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id=json_id,
                    plugin_kind="source",
                    plugin_name="json",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"), ("source", "json"))),
        guided=guided,
        originating_message_content="Later apply a gate to csv rows where amount is greater than 500.",
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_plugin_subject_ignores_destination_plugin_words_when_binding_source() -> None:
    csv_id = "77777777-7777-4777-8777-777777777777"
    json_id = "88888888-8888-4888-8888-888888888888"
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=(csv_id, json_id),
        reviewed_sources={
            csv_id: SourceResolved(
                name="csv_source",
                plugin="csv",
                options={"path": "/data/input.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
            json_id: SourceResolved(
                name="json_source",
                plugin="json",
                options={"path": "/data/input.jsonl"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
        },
    )
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id=json_id,
                    plugin_kind="source",
                    plugin_name="json",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"), ("source", "json"))),
        guided=guided,
        originating_message_content=(
            "Later route csv rows where amount is greater than 500 to a high_value JSON sink, and everything else to standard."
        ),
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_stable_subject_rejects_message_that_names_a_different_live_source() -> None:
    csv_id = "77777777-7777-4777-8777-777777777777"
    json_id = "88888888-8888-4888-8888-888888888888"
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=(csv_id, json_id),
        reviewed_sources={
            csv_id: SourceResolved(
                name="csv_source",
                plugin="csv",
                options={"path": "/data/input.csv"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
            json_id: SourceResolved(
                name="json_source",
                plugin="json",
                options={"path": "/data/input.jsonl"},
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
        },
    )
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=StableSubject(kind="stable", component_kind="source", stable_id=json_id),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view(()),
        guided=guided,
        originating_message_content="Later apply a gate to csv rows where amount is greater than 500.",
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_grounding_does_not_turn_an_unrelated_word_limit_into_a_row_predicate() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="result",
                operator="less_than",
                value=50,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=(
            "Summarize each csv result in under 50 words, then route errors to high_value and everything else to standard."
        ),
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later add a gate that routes csv rows with amount greater than 500 and amount less than 1000 "
        "to high_value, and everything else to standard.",
        "Later add a gate that routes csv rows with amount greater than 500 or priority equals true "
        "to high_value, and everything else to standard.",
    ),
)
def test_stated_grounding_rejects_unrepresented_compound_predicates(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later add a gate that routes csv rows with amount greater than 500 to high_value and audit_copy, and everything else to standard.",
        "Later add a gate that routes csv rows with amount greater than 500 to high_value after first sending them "
        "to manual_review, and everything else to standard.",
    ),
)
def test_stated_grounding_rejects_unrepresented_extra_branch_destinations(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    "message",
    (
        "Later avoid a gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later skip the gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later prohibit routing csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later, no gate should route csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later refrain from routing csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later remove the gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later delete the gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later disable the gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later we cannot use a gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later we can't use a gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later we can\u2019t use a gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later we won't use a gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
        "Later we won\u2019t use a gate that routes csv rows with amount greater than 500 to high_value, and everything else to standard.",
    ),
)
def test_stated_grounding_rejects_negative_authority_verbs(message: str) -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated routing.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content=message,
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


def test_stated_predicate_rejects_unrepresented_exclusion_clause() -> None:
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated predicate.",
        constraints=(
            StatedPredicateConstraint(
                kind="stated_predicate",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="99999999-9999-4999-8999-999999999999",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
            ),
        ),
    )

    assert validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "csv"),)),
        guided=GuidedSession.initial(),
        originating_message_content="Later apply a gate where csv amount is greater than 500, excluding priority rows.",
    ) == DeferredIntentRejected(reason="stated_fact_unproven")


@pytest.mark.parametrize(
    ("edge_type", "target_stage", "expected"),
    (
        ("route_true", "topology", DeferredIntentAccepted),
        ("route_false", "topology", DeferredIntentAccepted),
        ("fork", "topology", DeferredIntentAccepted),
        ("on_success", "topology", DeferredIntentRejected),
    ),
)
def test_gate_route_constraints_belong_to_topology_instead_of_wire_review(
    edge_type: str,
    target_stage: str,
    expected: type[DeferredIntentAccepted] | type[DeferredIntentRejected],
) -> None:
    action = DeferredIntentAction(
        target_stage=target_stage,  # type: ignore[arg-type]
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Apply the stated gate route while authoring topology.",
        constraints=(
            EdgeRouteConstraint(
                kind="edge_route",
                from_subject=StableSubject(
                    kind="stable",
                    component_kind="node",
                    stable_id="33333333-3333-4333-8333-333333333333",
                ),
                edge_type=edge_type,  # type: ignore[arg-type]
                to_subject=StableSubject(
                    kind="stable",
                    component_kind="output",
                    stable_id="44444444-4444-4444-8444-444444444444",
                ),
                present=True,
            ),
        ),
    )

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view(()),
        guided=GuidedSession.initial(),
    )

    assert type(result) is expected
    if expected is DeferredIntentRejected:
        assert result == DeferredIntentRejected(reason="wrong_responsible_stage")


def test_kind_qualified_name_resolves_without_guessing_across_other_plugin_kinds() -> None:
    action = _action()
    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view((("source", "llm"), ("transform", "llm"), ("sink", "llm"))),
        guided=GuidedSession.initial(),
    )
    assert result == DeferredIntentAccepted(action=action)


def test_absent_and_policy_denied_catalog_subjects_remain_distinct() -> None:
    absent = validate_deferred_intent_action(
        _action(),
        receiving_stage="source",
        catalog=_view(()),
        guided=GuidedSession.initial(),
    )
    denied = validate_deferred_intent_action(
        _action(),
        receiving_stage="source",
        catalog=_view((("transform", "llm"),), available=frozenset()),
        guided=GuidedSession.initial(),
    )
    assert absent == DeferredIntentUnsupported(
        plugin_kind="transform",
        plugin_name="llm",
        reason=PluginUnavailableReason.NOT_INSTALLED,
    )
    assert denied == DeferredIntentUnsupported(
        plugin_kind="transform",
        plugin_name="llm",
        reason=PluginUnavailableReason.NOT_AUTHORIZED,
    )


def test_exact_policy_denial_wins_over_same_name_visible_in_another_plugin_kind() -> None:
    action = _action()

    result = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=_view(
            (("source", "llm"), ("transform", "llm")),
            available=frozenset({PluginId("source", "llm")}),
        ),
        guided=GuidedSession.initial(),
    )

    assert result == DeferredIntentUnsupported(
        plugin_kind="transform",
        plugin_name="llm",
        reason=PluginUnavailableReason.NOT_AUTHORIZED,
    )


@pytest.mark.parametrize(
    ("receiving_stage", "target_stage"),
    [("source", "source"), ("output", "source"), ("topology", "output")],
)
def test_current_and_past_targets_are_rejected(receiving_stage: str, target_stage: str) -> None:
    result = validate_deferred_intent_action(
        _action(target_stage=target_stage),
        receiving_stage=receiving_stage,  # type: ignore[arg-type]
        catalog=_view((("transform", "llm"),)),
        guided=GuidedSession.initial(),
    )
    assert result == DeferredIntentRejected(reason="target_not_later")


def test_creation_binds_server_ids_and_exact_message_hash_without_raw_prose_in_metadata() -> None:
    action = _action()
    private_message = "Use llm on the private customer_notes field."
    intent = create_deferred_stage_intent(
        action,
        receiving_stage="source",
        intent_id=INTENT_ID,
        originating_message_id=MESSAGE_ID,
        originating_message_content=private_message,
    )

    assert intent.intent_id == INTENT_ID
    assert intent.originating_message_id == MESSAGE_ID
    assert intent.message_content_hash == stable_hash(private_message)
    assert intent.receiving_stage == "source"
    assert intent.target_stage == "topology"
    assert private_message not in repr(intent.to_dict())
    assert intent.to_dict()["redacted_summary"] == ("Future topology instruction for transform plugin 'llm'; 1 structural constraint(s).")


def test_model_summary_cannot_echo_private_message_into_durable_metadata() -> None:
    private_message = "Use the private customer_notes field with the llm transform."
    base = _action()
    echoing_action = DeferredIntentAction(
        target_stage=base.target_stage,
        catalog_kind=base.catalog_kind,
        catalog_name=base.catalog_name,
        redacted_summary=private_message,
        constraints=base.constraints,
    )

    intent = create_deferred_stage_intent(
        echoing_action,
        receiving_stage="source",
        intent_id=INTENT_ID,
        originating_message_id=MESSAGE_ID,
        originating_message_content=private_message,
    )

    assert private_message not in repr(intent.to_dict())
    assert intent.redacted_summary == "Future topology instruction for transform plugin 'llm'; 1 structural constraint(s)."


def test_deferred_intent_management_decoder_returns_closed_cancel_and_edit_actions() -> None:
    replacement = _action()
    replacement_dict = {
        "target_stage": replacement.target_stage,
        "catalog_kind": replacement.catalog_kind,
        "catalog_name": replacement.catalog_name,
        "redacted_summary": replacement.redacted_summary,
        "constraints": [constraint.to_dict() for constraint in replacement.constraints],
    }
    cancel = deferred_intent_management_action_from_dict({"action": "cancel", "intent_id": INTENT_ID, "selection_token": SELECTION_TOKEN})
    edit = deferred_intent_management_action_from_dict(
        {
            "action": "edit",
            "intent_id": INTENT_ID,
            "selection_token": SELECTION_TOKEN,
            "replacement": replacement_dict,
        }
    )

    assert cancel == DeferredIntentCancelAction(intent_id=INTENT_ID, selection_token=SELECTION_TOKEN)
    assert edit == DeferredIntentEditAction(intent_id=INTENT_ID, selection_token=SELECTION_TOKEN, replacement=replacement)


def test_management_resolution_cancels_one_exact_stable_id_and_edits_in_place() -> None:
    first = create_deferred_stage_intent(
        _action(),
        receiving_stage="source",
        intent_id=INTENT_ID,
        originating_message_id=MESSAGE_ID,
        originating_message_content="first private instruction",
    )
    second = create_deferred_stage_intent(
        _action(),
        receiving_stage="source",
        intent_id="33333333-3333-4333-8333-333333333333",
        originating_message_id="44444444-4444-4444-8444-444444444444",
        originating_message_content="second private instruction",
    )
    guided = replace(GuidedSession.initial(), deferred_intents=(first, second))
    catalog = _view((("transform", "llm"),))
    selection_token = intent_management_module.deferred_intent_management_option(first).selection_token

    cancelled = resolve_deferred_intent_management(
        DeferredIntentCancelAction(intent_id=INTENT_ID, selection_token=selection_token),
        guided=guided,
        catalog=catalog,
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content=f"cancel exact intent {INTENT_ID}",
    )
    edited = resolve_deferred_intent_management(
        DeferredIntentEditAction(intent_id=INTENT_ID, selection_token=selection_token, replacement=_action()),
        guided=guided,
        catalog=catalog,
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content=f"edit exact intent {INTENT_ID}: require the named transform",
    )

    assert type(cancelled) is DeferredIntentManagementApplied
    assert cancelled.deferred_intents == (second,)
    assert type(edited) is DeferredIntentManagementApplied
    assert [intent.intent_id for intent in edited.deferred_intents] == [first.intent_id, second.intent_id]
    assert edited.deferred_intents[0].originating_message_id == "55555555-5555-4555-8555-555555555555"


@pytest.mark.parametrize(
    ("prior_action", "replacement", "expected_target"),
    [
        pytest.param(
            _action(),
            DeferredIntentAction(
                target_stage="output",
                catalog_kind="sink",
                catalog_name="json",
                redacted_summary="Keep the JSON output.",
                constraints=(
                    ComponentCountConstraint(
                        kind="component_count",
                        component_kind="output",
                        plugin_kind="sink",
                        plugin_name="json",
                        operator="at_least",
                        count=1,
                    ),
                ),
            ),
            "output",
            id="edit-moves-authority-earlier",
        ),
        pytest.param(
            DeferredIntentAction(
                target_stage="output",
                catalog_kind="sink",
                catalog_name="json",
                redacted_summary="Keep the JSON output.",
                constraints=(
                    ComponentCountConstraint(
                        kind="component_count",
                        component_kind="output",
                        plugin_kind="sink",
                        plugin_name="json",
                        operator="at_least",
                        count=1,
                    ),
                ),
            ),
            _action(),
            "topology",
            id="edit-moves-authority-later",
        ),
    ],
)
def test_management_edit_exposes_replacement_as_effective_rewind_authority(
    prior_action: DeferredIntentAction,
    replacement: DeferredIntentAction,
    expected_target: str,
) -> None:
    prior = create_deferred_stage_intent(
        prior_action,
        receiving_stage="source",
        intent_id=INTENT_ID,
        originating_message_id=MESSAGE_ID,
        originating_message_content="private prior instruction",
    )
    preserved = create_deferred_stage_intent(
        _action(),
        receiving_stage="source",
        intent_id="33333333-3333-4333-8333-333333333333",
        originating_message_id="44444444-4444-4444-8444-444444444444",
        originating_message_content="private preserved instruction",
    )
    result = resolve_deferred_intent_management(
        DeferredIntentEditAction(
            intent_id=prior.intent_id,
            selection_token=intent_management_module.deferred_intent_management_option(prior).selection_token,
            replacement=replacement,
        ),
        guided=replace(GuidedSession.initial(), deferred_intents=(prior, preserved)),
        catalog=_view((("transform", "llm"), ("sink", "json"))),
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content=f"Edit exact intent {prior.intent_id}: replace the saved instruction",
    )

    assert type(result) is DeferredIntentManagementApplied
    assert result.effective_intent.target_stage == expected_target
    assert result.effective_intent.intent_id == prior.intent_id
    assert [intent.intent_id for intent in result.deferred_intents] == [prior.intent_id, preserved.intent_id]


def test_management_cancel_keeps_prior_intent_as_effective_rewind_authority() -> None:
    prior = create_deferred_stage_intent(
        _action(),
        receiving_stage="source",
        intent_id=INTENT_ID,
        originating_message_id=MESSAGE_ID,
        originating_message_content="private prior instruction",
    )
    result = resolve_deferred_intent_management(
        DeferredIntentCancelAction(
            intent_id=prior.intent_id,
            selection_token=intent_management_module.deferred_intent_management_option(prior).selection_token,
        ),
        guided=replace(GuidedSession.initial(), deferred_intents=(prior,)),
        catalog=_view((("transform", "llm"),)),
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content=f"Cancel exact intent {prior.intent_id}.",
    )

    assert type(result) is DeferredIntentManagementApplied
    assert result.effective_intent is prior


def _count_intent(*, intent_id: str, message_id: str, count: int, message: str) -> DeferredStageIntent:
    return create_deferred_stage_intent(
        DeferredIntentAction(
            target_stage="topology",
            catalog_kind="transform",
            catalog_name="llm",
            redacted_summary="Use the named transform.",
            constraints=(
                ComponentCountConstraint(
                    kind="component_count",
                    component_kind="node",
                    plugin_kind="transform",
                    plugin_name="llm",
                    operator="at_least",
                    count=count,
                ),
            ),
        ),
        receiving_stage="source",
        intent_id=intent_id,
        originating_message_id=message_id,
        originating_message_content=message,
    )


def test_management_options_distinguish_exact_structural_constraints_without_private_prose() -> None:
    first = _count_intent(intent_id=INTENT_ID, message_id=MESSAGE_ID, count=1, message="PRIVATE-FIRST-CANARY")
    second = _count_intent(
        intent_id="33333333-3333-4333-8333-333333333333",
        message_id="44444444-4444-4444-8444-444444444444",
        count=2,
        message="PRIVATE-SECOND-CANARY",
    )

    first_option = intent_management_module.deferred_intent_management_option(first)
    second_option = intent_management_module.deferred_intent_management_option(second)

    assert first_option.structural_constraints[0]["count"] == 1
    assert second_option.structural_constraints[0]["count"] == 2
    assert first_option.selection_token != second_option.selection_token
    assert "PRIVATE-FIRST-CANARY" not in repr(first_option)
    assert "PRIVATE-SECOND-CANARY" not in repr(second_option)


def test_management_option_hashes_option_values_instead_of_egressing_raw_value() -> None:
    private_value = "PRIVATE-OPTION-VALUE-CANARY"
    intent = create_deferred_stage_intent(
        _option_action(
            subject=PluginSubject(
                kind="plugin",
                subject_id="33333333-3333-4333-8333-333333333333",
                plugin_kind="transform",
                plugin_name="llm",
            ),
            option_path=("mode",),
            value=private_value,
        ),
        receiving_stage="source",
        intent_id=INTENT_ID,
        originating_message_id=MESSAGE_ID,
        originating_message_content="private instruction",
    )

    option = intent_management_module.deferred_intent_management_option(intent)
    constraint = option.structural_constraints[0]

    assert "value" not in constraint
    assert constraint["value_hash"] == stable_hash({"schema": "guided.deferred-option-value.v1", "value": private_value})
    assert private_value not in repr(option)


def test_management_rejects_model_intent_id_and_selection_token_mixup_without_mutation() -> None:
    first = _count_intent(intent_id=INTENT_ID, message_id=MESSAGE_ID, count=1, message="first private")
    second = _count_intent(
        intent_id="33333333-3333-4333-8333-333333333333",
        message_id="44444444-4444-4444-8444-444444444444",
        count=2,
        message="second private",
    )
    guided = replace(GuidedSession.initial(), deferred_intents=(first, second))

    result = resolve_deferred_intent_management(
        DeferredIntentCancelAction(
            intent_id=first.intent_id,
            selection_token=intent_management_module.deferred_intent_management_option(second).selection_token,
        ),
        guided=guided,
        catalog=_view((("transform", "llm"),)),
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content="Cancel the count-one instruction.",
    )

    assert type(result) is intent_management_module.DeferredIntentManagementBindingMismatch
    assert guided.deferred_intents == (first, second)


@pytest.mark.parametrize(
    ("intent_count", "action_kind", "message_template", "applied"),
    [
        pytest.param(1, "cancel", "Cancel exact intent {first_id}.", True, id="single-explicit-cancel"),
        pytest.param(
            1,
            "edit",
            "Edit exact intent {first_id}: require one named transform.",
            True,
            id="single-explicit-edit",
        ),
        pytest.param(1, "cancel", "Cancel the count-one instruction.", False, id="single-ambiguous"),
        pytest.param(1, "cancel", "Keep this saved instruction; explain it.", False, id="single-explanation-only"),
        pytest.param(
            1,
            "cancel",
            "Do not cancel exact intent {first_id}; explain it.",
            False,
            id="single-contradictory",
        ),
        pytest.param(1, "edit", "Cancel exact intent {first_id}.", False, id="single-action-mismatch"),
        pytest.param(2, "cancel", "Cancel exact intent {first_id}.", True, id="plural-explicit-cancel"),
        pytest.param(
            2,
            "edit",
            "Edit exact intent {first_id}: require one named transform.",
            True,
            id="plural-explicit-edit",
        ),
        pytest.param(2, "cancel", "Cancel the count-one instruction.", False, id="plural-ambiguous"),
        pytest.param(2, "cancel", "Cancel exact intent {second_id}.", False, id="plural-wrong-target"),
        pytest.param(
            2,
            "cancel",
            "Compare {first_id} with {second_id}, then cancel one.",
            False,
            id="plural-multiple-targets",
        ),
        pytest.param(2, "cancel", "Explain exact intent {first_id}.", False, id="plural-explanation-only"),
        pytest.param(
            2,
            "cancel",
            "Do not cancel exact intent {first_id}; explain it.",
            False,
            id="plural-contradictory",
        ),
        pytest.param(2, "edit", "Cancel exact intent {first_id}.", False, id="plural-action-mismatch"),
    ],
)
def test_management_requires_exact_action_specific_user_authority(
    intent_count: int,
    action_kind: str,
    message_template: str,
    applied: bool,
) -> None:
    first = _count_intent(intent_id=INTENT_ID, message_id=MESSAGE_ID, count=1, message="first private")
    second = _count_intent(
        intent_id="33333333-3333-4333-8333-333333333333",
        message_id="44444444-4444-4444-8444-444444444444",
        count=2,
        message="second private",
    )
    deferred_intents = (first,) if intent_count == 1 else (first, second)
    guided = replace(GuidedSession.initial(), deferred_intents=deferred_intents)
    selection_token = intent_management_module.deferred_intent_management_option(first).selection_token
    action = (
        DeferredIntentCancelAction(intent_id=first.intent_id, selection_token=selection_token)
        if action_kind == "cancel"
        else DeferredIntentEditAction(intent_id=first.intent_id, selection_token=selection_token, replacement=_action())
    )

    result = resolve_deferred_intent_management(
        action,
        guided=guided,
        catalog=_view((("transform", "llm"),)),
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content=message_template.format(first_id=first.intent_id, second_id=second.intent_id),
    )

    if applied:
        assert type(result) is DeferredIntentManagementApplied
        if action_kind == "cancel":
            assert result.deferred_intents == (() if intent_count == 1 else (second,))
        else:
            assert tuple(intent.intent_id for intent in result.deferred_intents) == tuple(intent.intent_id for intent in deferred_intents)
    else:
        assert type(result) is intent_management_module.DeferredIntentManagementAmbiguous
        assert guided.deferred_intents == deferred_intents


def test_identical_management_options_require_private_message_to_name_exact_uuid() -> None:
    first = _count_intent(intent_id=INTENT_ID, message_id=MESSAGE_ID, count=1, message="first private")
    second = _count_intent(
        intent_id="33333333-3333-4333-8333-333333333333",
        message_id="44444444-4444-4444-8444-444444444444",
        count=1,
        message="second private",
    )
    guided = replace(GuidedSession.initial(), deferred_intents=(first, second))
    action = DeferredIntentCancelAction(
        intent_id=first.intent_id,
        selection_token=intent_management_module.deferred_intent_management_option(first).selection_token,
    )

    ambiguous = resolve_deferred_intent_management(
        action,
        guided=guided,
        catalog=_view((("transform", "llm"),)),
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content="Cancel the saved count-one instruction.",
    )
    explicit = resolve_deferred_intent_management(
        action,
        guided=guided,
        catalog=_view((("transform", "llm"),)),
        originating_message_id="55555555-5555-4555-8555-555555555555",
        originating_message_content=f"Cancel exact intent {first.intent_id}.",
    )

    assert type(ambiguous) is intent_management_module.DeferredIntentManagementAmbiguous
    assert type(explicit) is DeferredIntentManagementApplied
    assert explicit.deferred_intents == (second,)


def test_schema8_rewind_boundary_is_explicit_for_passed_output_and_topology_only() -> None:
    assert (
        schema8_deferred_management_rewind_step(
            current_step=GuidedStep.STEP_4_WIRE,
            target_stage="output",
        )
        is GuidedStep.STEP_2_SINK
    )
    assert (
        schema8_deferred_management_rewind_step(
            current_step=GuidedStep.STEP_4_WIRE,
            target_stage="topology",
        )
        is GuidedStep.STEP_2_SINK
    )
    assert (
        schema8_deferred_management_rewind_step(
            current_step=GuidedStep.STEP_4_WIRE,
            target_stage="wire_review",
        )
        is None
    )
    with pytest.raises(AuditIntegrityError, match="already-passed source"):
        schema8_deferred_management_rewind_step(
            current_step=GuidedStep.STEP_4_WIRE,
            target_stage="source",
        )


def _encoded_action() -> dict[str, object]:
    action = _action()
    return {
        "target_stage": action.target_stage,
        "catalog_kind": action.catalog_kind,
        "catalog_name": action.catalog_name,
        "redacted_summary": action.redacted_summary,
        "constraints": [constraint.to_dict() for constraint in action.constraints],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "delete", "intent_id": INTENT_ID},
        {"action": "cancel", "intent_id": "not-a-uuid"},
        {"action": "cancel", "intent_id": INTENT_ID, "replacement": _encoded_action()},
        {"action": "edit", "intent_id": INTENT_ID},
        {"action": "edit", "intent_id": INTENT_ID, "replacement": _encoded_action(), "raw_prose": "secret"},
    ],
)
def test_deferred_intent_management_decoder_rejects_every_malformed_shape(payload: object) -> None:
    with pytest.raises(DeferredIntentManagementActionShapeError):
        deferred_intent_management_action_from_dict(payload)


def test_create_deferred_clarification_intent_is_constraint_free_and_prose_free() -> None:
    """Last-resort retention (R2-F15): durable, unclaimable, no user prose.

    The empty constraint set keeps the intent permanently unclaimable
    (``evaluate_deferred_intent_coverage`` rejects claims on constraint-free
    intents — pinned in test_deferred_intent_coverage), so it stays visibly
    pending until the user cancels it or edits it into a structural intent.
    """
    from elspeth.web.composer.guided.deferred_intents import create_deferred_clarification_intent

    private_prose = "Later do the private-needle thing."
    intent = create_deferred_clarification_intent(
        receiving_stage="source",
        intent_id="00000000-0000-4000-8000-000000000777",
        originating_message_id="00000000-0000-4000-8000-000000000778",
        originating_message_content=private_prose,
    )

    assert intent.receiving_stage == "source"
    assert intent.target_stage == "wire_review"
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None
    assert "private-needle" not in intent.redacted_summary
    assert intent.message_content_hash == stable_hash(private_prose)
    # The management surface must be able to list/select it.
    option = intent_management_module.deferred_intent_management_option(intent)
    assert option.intent_id == intent.intent_id
    assert option.structural_constraints == ()
