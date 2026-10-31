"""Precise secret refusals preserve the saved graph and credential custody."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.error_codes import REGISTERED_ERROR_CODES
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.composer.tools import _dispatch, secrets
from elspeth.web.composer.tools._common import ToolContext, ToolResult, normalize_tool_result_validation
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context, execute_tool
from elspeth.web.composer.tools.generation import explain_validation_code, validation_guidance_items
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy, SecretWiringRule
from tests.unit.web.composer.test_tools import _restricted_policy_pair
from tests.unit.web.composer.test_wire_ref_review_reconciliation import _llm_state_with_model_review, _secret_wire


class _ReferenceInventory:
    """Metadata only; any attempt to obtain a credential value fails."""

    def __init__(self, *, available: bool = True, armed: bool = False) -> None:
        self.available = available
        self.armed = armed
        self.calls: list[tuple[str, ...]] = []
        self.items: list[SecretInventoryItem] = []

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        assert not self.armed, "missing-context guard reached inventory"
        self.calls.append(("list", user_id))
        return self.items

    def has_ref(self, user_id: str, name: str) -> bool:
        assert not self.armed, "missing-context guard reached availability"
        self.calls.append(("has", user_id, name))
        return self.available

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        raise AssertionError("Composer must never request a credential value")


@pytest.fixture
def context() -> ToolContext:
    full = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(full)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(full, snapshot),
        plugin_snapshot=snapshot,
        secret_service=_ReferenceInventory(),
        user_id="operator-one",
    )


def _state() -> CompositionState:
    node = NodeSpec(
        id="clean",
        node_type="transform",
        plugin="passthrough",
        input="rows",
        on_success="main",
        on_error="discard",
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    return CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            options={"path": "input.json", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(node,),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="json",
                options={"path": "outputs/result.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Reference routing"),
        version=7,
    )


def _policy(component: str, plugin: str, key: str = "api_key") -> SecretWiringPolicy:
    assert component in {"source", "transform", "sink"}
    return SecretWiringPolicy(rules=(SecretWiringRule(secret="ROUTE_REF", component_type=component, plugin=plugin, option_key=key),))


def _wire_arguments(target: str, target_id: str | None = None, key: str = "api_key") -> dict[str, Any]:
    args: dict[str, Any] = {"name": "ROUTE_REF", "target": target, "option_key": key}
    if target_id is not None:
        args["target_id"] = target_id
    return args


def _assert_refusal(result: ToolResult, state: CompositionState, standing: ValidationSummary, code: str, subject: str | None) -> None:
    assert result.success is False
    assert result.validation.is_valid is False
    assert result.updated_state is state
    assert result.updated_state.version == 7
    assert result.affected_nodes == ()
    assert result.data is None
    assert result._state_validation_withheld is False
    assert result.plugin_schemas is None
    assert replace(result.validation, is_valid=standing.is_valid, errors=result.validation.errors[1:]) == standing
    rejection = result.validation.errors[0]
    assert rejection.component == "rejected_mutation"
    assert rejection.plugin_identity is None
    assert rejection.error_code == code
    assert rejection.rejected_component == subject


_DIRECT_CALLS = (
    pytest.param(secrets._handle_list_secret_refs, {}, id="list"),
    pytest.param(secrets._handle_validate_secret_ref, {"name": "ROUTE_REF"}, id="validate"),
    pytest.param(secrets._execute_wire_secret_ref, _wire_arguments("node", "clean"), id="wire"),
)


@pytest.mark.parametrize("handler,arguments", _DIRECT_CALLS)
@pytest.mark.parametrize("gap", ["service", "user", "both"])
def test_direct_context_refusal_preserves_saved_findings(
    handler: Callable[..., ToolResult],
    arguments: dict[str, Any],
    gap: str,
    context: ToolContext,
) -> None:
    state = _state()
    inventory = _ReferenceInventory(armed=True)
    missing = replace(context, secret_service=None if gap != "user" else inventory, user_id=None if gap != "service" else "operator-one")
    result = handler(arguments, state, missing)
    assert inventory.calls == []
    assert result.validation.errors[0].message == "Secret tools require secret service context."
    _assert_refusal(result, state, state.validate(), "secret_context_unavailable", None)


@pytest.mark.parametrize(
    "tool,arguments",
    [("list_secret_refs", {}), ("validate_secret_ref", {"name": "ROUTE_REF"}), ("wire_secret_ref", _wire_arguments("node", "clean"))],
)
@pytest.mark.parametrize("gap", ["service", "user", "both"])
def test_public_context_refusal_attaches_only_mutation_guidance(
    tool: str, arguments: dict[str, Any], gap: str, context: ToolContext
) -> None:
    state = _state()
    inventory = _ReferenceInventory(armed=True)
    result = execute_tool(
        tool,
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        secret_service=None if gap != "user" else inventory,
        user_id=None if gap != "service" else "operator-one",
        validate_arguments=True,
    )
    assert inventory.calls == []
    standing = context.catalog.validate_composition_state(state).validation
    _assert_refusal(result, state, standing, "secret_context_unavailable", None)
    if tool == "wire_secret_ref":
        assert result.validation_guidance is not None
        guidance = result.validation_guidance["codes"]["secret_context_unavailable"]
        assert (guidance["explanation"], guidance["suggested_fix"]) == explain_validation_code("secret_context_unavailable")
    else:
        _assert_discovery_failure(tool, arguments, result)


def _assert_discovery_failure(tool: str, arguments: dict[str, Any], result: ToolResult) -> None:
    assert result.validation_guidance is None
    admitted = admit_discovery_result(tool, result)
    assert admitted.response is None
    ordinary = json.loads(serialize_admitted_discovery_result(admitted))
    assert ordinary["success"] is False
    assert "data" not in ordinary
    assert "validation_guidance" not in ordinary
    assert ordinary["validation"]["errors"][0]["error_code"] == "secret_context_unavailable"


@pytest.mark.parametrize(
    "tool,arguments",
    [("list_secret_refs", {}), ("validate_secret_ref", {"name": "ROUTE_REF"}), ("wire_secret_ref", _wire_arguments("node", "clean"))],
)
def test_public_context_guard_never_enters_handler(
    tool: str, arguments: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(arguments: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
        raise AssertionError("public context refusal entered handler")

    monkeypatch.setattr(
        _dispatch,
        "_SECRET_DISCOVERY_TOOLS",
        {**_dispatch._SECRET_DISCOVERY_TOOLS, **({tool: unreachable} if tool != "wire_secret_ref" else {})},
    )
    monkeypatch.setattr(
        _dispatch,
        "_SECRET_MUTATION_TOOLS",
        {**_dispatch._SECRET_MUTATION_TOOLS, **({tool: unreachable} if tool == "wire_secret_ref" else {})},
    )
    result = execute_tool(tool, arguments, _state(), context.catalog, plugin_snapshot=context.plugin_snapshot, validate_arguments=True)
    assert result.success is False
    assert result.validation.errors[0].error_code == "secret_context_unavailable"


@pytest.mark.parametrize("tool,arguments", [("list_secret_refs", {}), ("validate_secret_ref", {"name": "ROUTE_REF"})])
def test_readonly_context_guard_never_enters_handler(
    tool: str, arguments: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(arguments: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
        raise AssertionError("read-only context refusal entered handler")

    monkeypatch.setattr(_dispatch, "_SECRET_DISCOVERY_TOOLS", {**_dispatch._SECRET_DISCOVERY_TOOLS, tool: unreachable})
    result = execute_discovery_tool_with_context(tool, arguments, _state(), replace(context, secret_service=None))
    assert result.success is False
    assert result.validation.errors[0].error_code == "secret_context_unavailable"


@pytest.mark.parametrize("tool,arguments", [("list_secret_refs", {}), ("validate_secret_ref", {"name": "ROUTE_REF"})])
@pytest.mark.parametrize("gap", ["service", "user", "both"])
def test_readonly_dispatch_context_refusal_is_not_metadata_success(
    tool: str, arguments: dict[str, Any], gap: str, context: ToolContext
) -> None:
    state = _state()
    inventory = _ReferenceInventory(armed=True)
    missing = replace(context, secret_service=None if gap != "user" else inventory, user_id=None if gap != "service" else "operator-one")
    result = execute_discovery_tool_with_context(tool, arguments, state, missing)
    assert inventory.calls == []
    _assert_refusal(result, state, context.catalog.validate_composition_state(state).validation, "secret_context_unavailable", None)
    _assert_discovery_failure(tool, arguments, result)


def test_missing_context_does_not_reorder_argument_admission(context: ToolContext) -> None:
    state = _state()
    missing = replace(context, secret_service=None, user_id=None)
    with pytest.raises(ToolArgumentError):
        secrets._handle_list_secret_refs({"extra": True}, state, missing)
    for handler in (secrets._handle_validate_secret_ref, secrets._execute_wire_secret_ref):
        result = handler({}, state, missing)
        assert result.validation.errors[0].message == "Secret tools require secret service context."
    for tool in ("list_secret_refs", "validate_secret_ref", "wire_secret_ref"):
        with pytest.raises(ToolArgumentError):
            execute_tool(
                tool, {"extra": True}, state, context.catalog, plugin_snapshot=context.plugin_snapshot, raise_schema_argument_errors=True
            )
    with pytest.raises(ToolArgumentError):
        execute_discovery_tool_with_context("validate_secret_ref", {}, state, missing)


@pytest.mark.parametrize(
    "case,code,subject",
    [
        ("unavailable", "missing_secret_ref", None),
        ("unavailable-before-target", "missing_secret_ref", None),
        ("no-source", "source_not_found", None),
        ("ambiguous-source", "secret_target_required", None),
        ("absent-source", "source_not_found", "source:unknown"),
        ("node-id", "secret_target_required", None),
        ("output-id", "secret_target_required", None),
        ("absent-node", "node_not_found", "node:unknown"),
        ("absent-output", "output_not_found", "output:unknown"),
        ("unsupported", "secret_target_unsupported", "node:clean"),
        ("unsupported-no-plugin", "secret_target_unsupported", "node:clean"),
        ("source-denied", "unauthorized_secret_ref", "source"),
        ("named-source-denied", "unauthorized_secret_ref", "source:records"),
        ("node-denied", "unauthorized_secret_ref", "node:clean"),
        ("output-denied", "unauthorized_secret_ref", "output:main"),
        ("source-placement", "disallowed_secret_ref", "source"),
        ("node-placement", "disallowed_secret_ref", "node:clean"),
        ("output-placement", "disallowed_secret_ref", "output:main"),
        ("source-endpoint", "aws_s3_endpoint_url_not_allowed", "source"),
        ("output-endpoint", "aws_s3_endpoint_url_not_allowed", "output:main"),
    ],
)
@pytest.mark.parametrize("dispatch", [False, True], ids=["direct", "public"])
def test_wire_refusal_code_subject_and_surviving_graph(
    case: str, code: str, subject: str | None, dispatch: bool, context: ToolContext
) -> None:
    state = _state()
    inventory = _ReferenceInventory(available=not case.startswith("unavailable"))
    args = _wire_arguments("node", "clean")
    policy = None
    if case in {
        "no-source",
        "ambiguous-source",
        "absent-source",
        "source-denied",
        "named-source-denied",
        "source-placement",
        "source-endpoint",
    }:
        args = _wire_arguments("source", "unknown" if case == "absent-source" else None)
        if case == "no-source":
            state = replace(state, sources={})
        elif case == "ambiguous-source":
            state = replace(state, sources={"first": state.sources["source"], "second": state.sources["source"]})
        elif case == "named-source-denied":
            state = replace(state, sources={"records": state.sources["source"]})
    elif case.startswith("output") or case == "absent-output":
        args = _wire_arguments("output", None if case == "output-id" else "unknown" if case == "absent-output" else "main")
    elif case in {"node-id", "unavailable-before-target"}:
        args = _wire_arguments("node")
    elif case == "absent-node":
        args = _wire_arguments("node", "unknown")
    elif case == "unsupported":
        state = replace(
            state,
            nodes=(replace(state.nodes[0], node_type="gate", plugin=None, condition="True", routes={"true": "main", "false": "main"}),),
        )
    elif case == "unsupported-no-plugin":
        state = replace(state, nodes=(replace(state.nodes[0], plugin=None),))
    if case.endswith("placement"):
        args["option_key"] = "description"
        policy = _policy(
            "source" if args["target"] == "source" else "sink" if args["target"] == "output" else "transform",
            "passthrough" if args["target"] == "node" else "json",
            "description",
        )
    elif case.endswith("endpoint"):
        args["option_key"] = "endpoint_url"
        if args["target"] == "source":
            state = replace(state, sources={"source": replace(state.sources["source"], plugin="aws_s3")})
        else:
            state = replace(state, outputs=(replace(state.outputs[0], plugin="aws_s3"),))
        policy = _policy("source" if args["target"] == "source" else "sink", "aws_s3", "endpoint_url")
    before = state.to_dict()
    active = replace(context, secret_service=inventory, secret_wiring_policy=policy)
    if dispatch:
        result = execute_tool(
            "wire_secret_ref",
            args,
            state,
            active.catalog,
            plugin_snapshot=active.plugin_snapshot,
            secret_service=inventory,
            secret_wiring_policy=policy,
            user_id=active.user_id,
            validate_arguments=True,
        )
        standing = active.catalog.validate_composition_state(state).validation
    else:
        result = secrets._execute_wire_secret_ref(args, state, active)
        standing = state.validate()
    assert inventory.calls == [("has", "operator-one", "ROUTE_REF")]
    assert state.to_dict() == before
    _assert_refusal(result, state, standing, code, subject)
    if dispatch:
        assert result.validation_guidance is not None
        text = result.validation_guidance["codes"][code]
        assert (text["explanation"], text["suggested_fix"]) == explain_validation_code(code)


@pytest.mark.parametrize(
    "target,component,plugin,target_id,affected",
    [
        ("source", "source", "json", None, "source"),
        ("source", "source", "json", None, "source:records"),
        ("node", "transform", "passthrough", "clean", "clean"),
        ("output", "sink", "json", "main", "main"),
    ],
)
def test_authorized_wiring_keeps_success_shape(
    target: str, component: str, plugin: str, target_id: str | None, affected: str, context: ToolContext
) -> None:
    state = _state()
    if affected == "source:records":
        state = replace(state, sources={"records": state.sources["source"]})
    before = state.to_dict()
    result = secrets._execute_wire_secret_ref(
        _wire_arguments(target, target_id), state, replace(context, secret_wiring_policy=_policy(component, plugin))
    )
    assert result.success is True
    assert result.updated_state.version == 8
    assert result.affected_nodes == (affected,)
    assert result.data is None
    assert result.validation_guidance is None
    wired = (
        result.updated_state.sources["records" if affected == "source:records" else "source"].options
        if target == "source"
        else result.updated_state.nodes[0].options
        if target == "node"
        else result.updated_state.outputs[0].options
    )
    assert deep_thaw(wired)["api_key"] == {"secret_ref": "ROUTE_REF"}
    assert state.to_dict() == before


@pytest.mark.parametrize("mismatch", ["absent-policy", "empty-policy", "secret", "component", "plugin", "option"])
def test_available_reference_does_not_authorize_a_near_match(mismatch: str, context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    policy = _policy("transform", "passthrough")
    rule = policy.rules[0]
    if mismatch == "absent-policy":
        policy = None
    elif mismatch == "empty-policy":
        policy = SecretWiringPolicy(rules=())
    elif mismatch == "secret":
        policy = SecretWiringPolicy(rules=(replace(rule, secret="ROUTE_REF_EXTRA"),))
    elif mismatch == "component":
        policy = SecretWiringPolicy(rules=(replace(rule, component_type="source"),))
    elif mismatch == "plugin":
        policy = SecretWiringPolicy(rules=(replace(rule, plugin="passthrough_extra"),))
    else:
        policy = SecretWiringPolicy(rules=(replace(rule, option_key="api_key_extra"),))

    def unreachable_placement(*args: object) -> str | None:
        raise AssertionError("authorization refusal reached placement")

    monkeypatch.setattr(secrets, "_secret_ref_placement_error", unreachable_placement)
    result = secrets._execute_wire_secret_ref(_wire_arguments("node", "clean"), state, replace(context, secret_wiring_policy=policy))
    _assert_refusal(result, state, state.validate(), "unauthorized_secret_ref", "node:clean")
    assert "not authorized" in result.validation.errors[0].message


@pytest.mark.parametrize("target", ["source", "output"])
def test_destination_authorization_precedes_endpoint_policy(target: str, context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    if target == "source":
        state = replace(state, sources={"source": replace(state.sources["source"], plugin="aws_s3")})
    else:
        state = replace(state, outputs=(replace(state.outputs[0], plugin="aws_s3"),))

    def unreachable_endpoint(*args: object) -> str | None:
        raise AssertionError("authorization refusal reached endpoint policy")

    monkeypatch.setattr(secrets, "web_aws_s3_endpoint_url_policy_error", unreachable_endpoint)
    result = secrets._execute_wire_secret_ref(
        _wire_arguments(target, None if target == "source" else "main", "endpoint_url"), state, context
    )
    _assert_refusal(result, state, state.validate(), "unauthorized_secret_ref", "source" if target == "source" else "output:main")


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("listed", [False, True])
def test_availability_discovery_remains_successful_metadata(available: bool, listed: bool, context: ToolContext) -> None:
    inventory = _ReferenceInventory(available=available)
    if listed:
        inventory.items = [
            SecretInventoryItem(
                name="ROUTE_REF",
                scope="user",
                available=not available,
                source_kind="stored",
                reason=None if not available else "value_decryption_failed",
            )
        ]
    active = replace(context, secret_service=inventory)
    state = _state()
    args = {"name": "ROUTE_REF"}
    result = execute_discovery_tool_with_context("validate_secret_ref", args, state, active)
    assert result.success is True
    assert result.updated_state is state
    assert result.affected_nodes == ()
    admitted = admit_discovery_result("validate_secret_ref", result)
    assert admitted.response is not None
    expected: dict[str, Any] = {"name": "ROUTE_REF", "available": available}
    if listed:
        expected.update(scope="user", source_kind="stored", reason=None if available else "value_decryption_failed")
    assert admitted.response.to_wire() == expected
    assert json.loads(serialize_admitted_discovery_result(admitted))["data"] == expected
    assert inventory.calls == [("list", "operator-one"), ("has", "operator-one", "ROUTE_REF")]


@pytest.mark.parametrize("target", ["source", "node", "output"])
@pytest.mark.parametrize("dispatch", [False, True], ids=["direct", "public"])
def test_real_review_drift_refuses_wire_as_whole_graph_operation(
    target: str, dispatch: bool, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _llm_state_with_model_review({"blob_ref": "00000000-0000-4000-8000-000000000001", "mode": "inline_content", "sha256": "0" * 64})
    state = replace(state, version=7)
    args, wiring = _secret_wire(state, target)
    reconcile = secrets.reconcile_authoritative_reviews
    observed: list[CompositionState] = []

    def observe_private_proposal(prior: CompositionState, proposed: CompositionState) -> CompositionState:
        assert prior is state
        assert proposed is not prior and proposed.version == 8
        assert proposed.to_dict() != prior.to_dict()
        observed.append(proposed)
        return reconcile(prior, proposed)

    monkeypatch.setattr(secrets, "reconcile_authoritative_reviews", observe_private_proposal)
    if dispatch:
        result = execute_tool(
            "wire_secret_ref",
            args,
            state,
            context.catalog,
            plugin_snapshot=context.plugin_snapshot,
            secret_service=wiring.secret_service,
            secret_wiring_policy=wiring.secret_wiring_policy,
            user_id=wiring.user_id,
            validate_arguments=True,
        )
        standing = context.catalog.validate_composition_state(state).validation
    else:
        result = secrets._execute_wire_secret_ref(args, state, wiring)
        standing = state.validate()
    assert len(observed) == 1
    assert "has no model" in result.validation.errors[0].message
    _assert_refusal(result, state, standing, "review_reconciliation_failed", None)


@pytest.mark.parametrize("target", ["source", "node", "output"])
@pytest.mark.parametrize("dispatch", [False, True], ids=["direct", "public"])
def test_injected_canonical_boundary_refuses_changed_private_proposal(
    target: str, dispatch: bool, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject the owned final boundary; do not claim this is natural reach."""
    state = replace(_llm_state_with_model_review("openai/gpt-4o"), version=7)
    args, wiring = _secret_wire(state, target)
    before = state.to_dict()
    observed: list[CompositionState] = []

    def refuse(proposed: CompositionState, *, tool_name: str) -> str | None:
        assert tool_name == "wire_secret_ref"
        assert proposed is not state and proposed.version == 8
        assert proposed.to_dict() != before
        options = (
            proposed.sources["source"].options
            if target == "source"
            else proposed.nodes[0].options
            if target == "node"
            else proposed.outputs[0].options
        )
        assert deep_thaw(options)["api_key"] == {"secret_ref": "OTHER_API_KEY"}
        observed.append(proposed)
        return "Generic canonical requirement refusal."

    monkeypatch.setattr(secrets, "_composition_canonical_interpretation_requirement_error", refuse)
    if dispatch:
        result = execute_tool(
            "wire_secret_ref",
            args,
            state,
            context.catalog,
            plugin_snapshot=context.plugin_snapshot,
            secret_service=wiring.secret_service,
            secret_wiring_policy=wiring.secret_wiring_policy,
            user_id=wiring.user_id,
            validate_arguments=True,
        )
        standing = context.catalog.validate_composition_state(state).validation
    else:
        result = secrets._execute_wire_secret_ref(args, state, wiring)
        standing = state.validate()
    assert len(observed) == 1
    assert state.to_dict() == before
    assert result.validation.errors[0].message == "Generic canonical requirement refusal."
    _assert_refusal(result, state, standing, "interpretation_requirements_invalid", None)


@pytest.mark.parametrize(
    "code",
    [
        "secret_context_unavailable",
        "secret_target_required",
        "secret_target_unsupported",
        "missing_secret_ref",
        "unauthorized_secret_ref",
        "disallowed_secret_ref",
    ],
)
def test_secret_codes_have_exact_static_guidance(code: str, context: ToolContext) -> None:
    assert code in REGISTERED_ERROR_CODES
    pair = explain_validation_code(code)
    assert pair is not None
    assert dict(validation_guidance_items())[code] == pair
    assert explain_validation_code(f"noise {code} noise") is None
    assert explain_validation_code(code.upper()) is None
    result = execute_tool(
        "explain_validation_error", {"error_text": code}, _state(), context.catalog, plugin_snapshot=context.plugin_snapshot
    )
    assert result.success is True
    assert result.data is not None
    assert (result.data["explanation"], result.data["suggested_fix"]) == pair


def test_restricted_refusal_carries_static_repair_guidance(context: ToolContext) -> None:
    state = _state()
    args = _wire_arguments("node", "clean", "PRIVATE_OPTION_CANARY")
    args["name"] = "PRIVATE_REF_CANARY"
    result = execute_tool(
        "wire_secret_ref",
        args,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        secret_service=_ReferenceInventory(),
        user_id="operator-one",
        validate_arguments=True,
    )
    rejection = result.validation.errors[0]
    assert "PRIVATE_REF_CANARY" in rejection.message
    assert "PRIVATE_OPTION_CANARY" in rejection.message
    assert rejection.error_code == "unauthorized_secret_ref"
    assert rejection.plugin_identity is None
    assert result.plugin_schemas is None
    pair = explain_validation_code("unauthorized_secret_ref")
    assert pair is not None
    assert result.validation_guidance is not None
    guidance = result.validation_guidance["codes"]["unauthorized_secret_ref"]
    assert (guidance["explanation"], guidance["suggested_fix"]) == pair


def test_changed_owned_snapshot_recomputes_surviving_findings(context: ToolContext) -> None:
    state = _state()
    full = create_catalog_service()
    restricted, snapshot = _restricted_policy_pair(full, PluginId("transform", "passthrough"))
    raw = secrets._execute_wire_secret_ref(_wire_arguments("node", "clean"), state, context)
    first = normalize_tool_result_validation(raw, context.catalog)
    second = normalize_tool_result_validation(first, restricted)
    expected = restricted.validate_composition_state(state).validation
    assert snapshot.snapshot_hash != context.plugin_snapshot.snapshot_hash
    assert expected != context.catalog.validate_composition_state(state).validation
    assert any(entry.error_code == "plugin_not_enabled" for entry in expected.errors)
    _assert_refusal(second, state, expected, "unauthorized_secret_ref", "node:clean")


def _two_source_state() -> CompositionState:
    root = _state().sources["source"]
    return replace(_state(), sources={"left": root, "right": root})


@pytest.mark.parametrize(
    "target_id,code,subject",
    [
        pytest.param(None, "secret_target_required", None, id="omitted-with-plural-roots"),
        pytest.param("source:left", "source_not_found", "source:source:left", id="source-prefixed"),
        pytest.param("   ", "source_not_found", "source:   ", id="whitespace-only"),
        pytest.param(" left ", "source_not_found", "source: left ", id="padded"),
    ],
)
@pytest.mark.parametrize("dispatch", [False, True], ids=["direct", "public"])
def test_source_selector_is_an_exact_raw_name_never_an_alias(
    target_id: str | None, code: str, subject: str | None, dispatch: bool, context: ToolContext
) -> None:
    """TC-003: the selector guidance's three refusals hold in the handler.

    With plural roots an omitted selector refuses rather than picking one;
    a ``source:``-prefixed, whitespace-only or padded selector is not
    normalised to a real root and refuses with the saved graph intact.
    """
    state = _two_source_state()
    policy = _policy("source", "json")
    active = replace(context, secret_wiring_policy=policy)
    args = _wire_arguments("source", target_id)
    before = state.to_dict()
    if dispatch:
        result = execute_tool(
            "wire_secret_ref",
            args,
            state,
            active.catalog,
            plugin_snapshot=active.plugin_snapshot,
            secret_service=active.secret_service,
            secret_wiring_policy=policy,
            user_id=active.user_id,
            validate_arguments=True,
        )
        standing = active.catalog.validate_composition_state(state).validation
    else:
        result = secrets._execute_wire_secret_ref(args, state, active)
        standing = state.validate()
    assert state.to_dict() == before
    _assert_refusal(result, state, standing, code, subject)


def test_raw_named_source_selector_wires_exactly_that_root(context: ToolContext) -> None:
    """Positive control for the refusals above: the raw name selects one root."""
    state = _two_source_state()
    result = secrets._execute_wire_secret_ref(
        _wire_arguments("source", "left"), state, replace(context, secret_wiring_policy=_policy("source", "json"))
    )
    assert result.success is True
    assert result.affected_nodes == ("source:left",)
    assert deep_thaw(result.updated_state.sources["left"].options)["api_key"] == {"secret_ref": "ROUTE_REF"}
    assert "api_key" not in deep_thaw(result.updated_state.sources["right"].options)


def test_target_id_description_states_the_source_selector_contract() -> None:
    description = secrets._WIRE_SECRET_REF_DECLARATION.json_schema["properties"]["target_id"]["description"]
    assert "Raw source name" in description
    assert "required for node/output targets and for source targets when multiple sources are configured" in description
    assert "For a sole source, an unspecified selector defaults to that source" in description
    assert "Do not prefix a source name with 'source:'." in description
