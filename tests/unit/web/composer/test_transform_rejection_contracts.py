"""Saved transform refusals retain state, owned reasons and safe repair evidence."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest.mock import create_autospec

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, EdgeSpec, ValidationSummary
from elspeth.web.composer.tools import execute_tool, transforms
from elspeth.web.composer.tools._common import ToolContext, ToolResult, normalize_tool_result_validation
from elspeth.web.composer.tools._dispatch import finalize_tool_result
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from tests.unit.web.composer import test_profile_policy_validation_adapter as profile_fixtures
from tests.unit.web.composer import test_splice_transform_tool as splice_fixtures
from tests.unit.web.composer._helpers import _composer_service_with_session, _persisted_tool_responses
from tests.unit.web.composer.test_compose_loop_audit_wiring import _make_llm_response, _make_settings
from tests.unit.web.composer.test_output_rejection_contracts import _saved_state as _standing_state
from tests.unit.web.composer.test_tools import _empty_state, _llm_options_with_api_key, _restricted_policy_pair

Surface = Literal["handler", "dispatch"]
Operation = Literal["upsert", "patch", "splice", "replay"]


def _context(**kwargs: Any) -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot, **kwargs)


def _saved_state() -> CompositionState:
    standing, linear = _standing_state(), splice_fixtures._state()
    return replace(standing, nodes=(*standing.nodes, *linear.nodes), edges=(*standing.edges, linear.edges[1]), version=17)


def _operation(operation: Operation, *, plugin: str = "passthrough", options: dict[str, Any] | None = None):
    state = _saved_state()
    options = {"schema": {"mode": "observed"}} if options is None else options
    if operation == "upsert":
        return (
            "upsert_node",
            {
                "id": "after",
                "node_type": "transform",
                "plugin": plugin,
                "input": "middle",
                "on_success": "result",
                "on_error": "discard",
                "options": options,
            },
            state,
        )
    if operation == "patch":
        state = replace(
            state, nodes=tuple(replace(node, plugin=plugin, options=options) if node.id == "after" else node for node in state.nodes)
        )
        return "patch_node_options", {"node_id": "after", "patch": options}, state
    args = {
        "predecessor_id": "before",
        "successor_id": "after",
        "node": {"id": "inserted", "plugin": plugin, "options": options, "on_error": "discard"},
    }
    if operation == "replay":
        base = replace(splice_fixtures._state(), version=17)
        applied = transforms._execute_splice_transform(splice_fixtures._arguments(), base, _context())
        assert applied.success and applied.updated_state.version == 18
        state = replace(
            applied.updated_state,
            nodes=tuple(
                replace(node, plugin=plugin, options=options) if node.id == "inserted" else node for node in applied.updated_state.nodes
            ),
        )
    return "splice_transform", args, state


def _invoke(surface: Surface, tool: str, args: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
    if surface == "handler":
        return {
            "upsert_node": transforms._handle_upsert_node,
            "patch_node_options": transforms._handle_patch_node_options,
            "splice_transform": transforms._handle_splice_transform,
        }[tool](args, state, context)
    return execute_tool(
        tool,
        args,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
    )


def _assert_refusal(result: ToolResult, state: CompositionState, code: str, subject: str | None, standing: ValidationSummary) -> None:
    assert not result.success and result.updated_state is state
    assert result.updated_state.version == state.version and result.affected_nodes == ()
    assert result.prior_validation is None and "validation_delta" not in result.to_dict()
    assert result.data is None
    entry = result.validation.errors[0]
    assert (entry.component, entry.error_code, entry.rejected_component) == ("rejected_mutation", code, subject)
    assert result.validation.errors[1:] == standing.errors
    assert result.validation.warnings == standing.warnings and result.validation.suggestions == standing.suggestions
    assert (
        result.validation.edge_contracts == standing.edge_contracts and result.validation.semantic_contracts == standing.semantic_contracts
    )


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize(
    "case",
    [
        "aggregation-count",
        "failure-choice",
        "queue",
        "gate-expression",
        "gate-parity",
        "aggregation-trigger",
        "batch-placement",
        "batch-fields",
        "missing-node",
        "routing-patch",
        "splice-ids",
        "splice-successor",
        "splice-topology",
        "splice-collision",
        "splice-derivation",
    ],
)
def test_transform_refusal_retains_saved_findings_code_subject_and_guidance(surface: Surface, case: str) -> None:
    tool, args, state = _operation("upsert")
    code, subject = "failure_route_required", "node:after"
    if case == "aggregation-count":
        args.update(node_type="aggregation", output_mode="passthrough", expected_output_count=2)
        code = "aggregation_expected_output_count_mode_invalid"
    elif case == "failure-choice":
        args["on_error"] = None
    elif case == "queue":
        args = {"id": "queue", "node_type": "queue", "input": "different"}
        code, subject = "queue_config_invalid", "node:queue"
    elif case == "gate-expression":
        args = {
            "id": "decision",
            "node_type": "gate",
            "input": "middle",
            "condition": "row.text ===",
            "routes": {"true": "result", "false": "discard"},
        }
        code, subject = "gate_condition_invalid", "node:decision"
    elif case == "gate-parity":
        args = {"id": "decision", "node_type": "gate", "input": "middle", "condition": "True", "routes": {"pass": "result"}}
        code, subject = "gate_route_labels_mismatch", "node:decision"
    elif case == "aggregation-trigger":
        args.update(
            node_type="aggregation", plugin=None, output_mode="transform", expected_output_count=1, trigger={"condition": "end_of_source"}
        )
        code = "aggregation_trigger_invalid"
    elif case in {"batch-placement", "batch-fields"}:
        args["plugin"] = "batch_stats"
        code = "batch_transform_misplaced"
        if case == "batch-fields":
            args.update(
                node_type="aggregation",
                output_mode="transform",
                expected_output_count=1,
                trigger={"count": 2},
                options={"schema": {"mode": "observed"}, "required_input_fields": ["text"]},
            )
            code = "batch_required_fields_invalid"
    elif case in {"missing-node", "routing-patch"}:
        tool = "patch_node_options"
        args = {
            "node_id": "absent" if case == "missing-node" else "after",
            "patch": {} if case == "missing-node" else {"on_success": "result"},
        }
        code, subject = ("node_not_found", "node:absent") if case == "missing-node" else ("node_routing_patch_invalid", "node:after")
    else:
        tool, args, state = _operation("splice")
        code, subject = "splice_topology_unsupported", None
        if case == "splice-ids":
            args["node"]["id"] = "before"
            code = "splice_ids_not_distinct"
        elif case == "splice-successor":
            args["successor_id"] = "absent"
            code, subject = "node_not_found", "node:absent"
        elif case == "splice-topology":
            args["predecessor_id"] = "absent"
        elif case == "splice-collision":
            args["node"]["id"] = next(iter(state.sources))
            code, subject = "node_id_collides_with_source_or_sink", f"node:{args['node']['id']}"
        elif case == "splice-derivation":
            collision = EdgeSpec(
                id=transforms._splice_edge_id("before-after", "inserted"),
                from_node="other",
                to_node="elsewhere",
                edge_type="on_success",
                label=None,
            )
            state = replace(state, edges=(*state.edges, collision))
            code, subject = "splice_derivation_conflict", "node:inserted"
    context, before = _context(), state.to_dict()
    standing = state.validate() if surface == "handler" else context.catalog.validate_composition_state(state).validation
    assert standing.errors and standing.warnings and standing.edge_contracts and standing.semantic_contracts
    result = _invoke(surface, tool, args, state, context)
    _assert_refusal(result, state, code, subject, standing)
    other_snapshot = replace(context.plugin_snapshot, snapshot_hash="transform-other-policy-snapshot")
    other_view = PolicyCatalogView.for_trained_operator(create_catalog_service(), other_snapshot)
    normalized = normalize_tool_result_validation(result, other_view)
    _assert_refusal(normalized, state, code, subject, other_view.validate_composition_state(state).validation)
    assert state.to_dict() == before
    feedback = _allowlisted_candidate_feedback(result)["validation"]["errors"][0]
    assert feedback["error_code"] == code and "detail" not in feedback
    if code in {
        "node_not_found",
        "node_routing_patch_invalid",
        "splice_ids_not_distinct",
        "splice_topology_unsupported",
        "splice_derivation_conflict",
    }:
        assert explain_validation_code(code) is not None and feedback["suggested_fix"]
        if surface == "dispatch":
            assert result.validation_guidance is not None and code in result.validation_guidance["codes"]


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_splice_replay_conflict_keeps_saved_state_and_specific_guidance(surface: Surface) -> None:
    tool, args, state = _operation("replay")
    identical = _invoke(surface, tool, args, state, _context())
    assert identical.success and identical.updated_state is state and identical.data["already_applied"] is True
    args["node"]["options"] = {"schema": {"mode": "observed"}, "required_input_fields": ["text"]}
    context = _context()
    assert transforms._prevalidate_transform_for_context(context, "passthrough", args["node"]["options"]) is None
    standing = state.validate() if surface == "handler" else context.catalog.validate_composition_state(state).validation
    result = _invoke(surface, tool, args, state, context)
    _assert_refusal(result, state, "splice_replay_conflict", "node:inserted", standing)
    feedback = _allowlisted_candidate_feedback(result)["validation"]["errors"][0]
    assert feedback["error_code"] == "splice_replay_conflict" and "identical" in feedback["suggested_fix"]
    assert "detail" not in feedback


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("node_type", ["transform", "queue"])
def test_transform_intrinsically_admissible_incomplete_draft_still_succeeds(surface: Surface, node_type: str) -> None:
    state = _empty_state()
    args = {"id": "buffer", "node_type": node_type, "input": "buffer"}
    if node_type == "transform":
        args.update(plugin="passthrough", on_success="later", on_error="discard", options={"schema": {"mode": "observed"}})
    result = _invoke(surface, "upsert_node", args, state, _context())
    assert result.success and result.updated_state.version == state.version + 1
    assert not result.validation.is_valid and result.updated_state.nodes[0].id == "buffer"


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
@pytest.mark.parametrize(
    "reason",
    [PluginUnavailableReason.NOT_AUTHORIZED, PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING, PluginUnavailableReason.NOT_INSTALLED],
    ids=["not_authorized", "local_missing", "not_installed"],
)
def test_transform_policy_denial_has_exact_reason_without_schema_access(
    monkeypatch: pytest.MonkeyPatch, surface: Surface, operation: Operation, reason: PluginUnavailableReason
) -> None:
    plugin = "not_installed_contract_transform" if reason is PluginUnavailableReason.NOT_INSTALLED else "passthrough"
    tool, args, state = _operation(operation, plugin=plugin)
    catalog = create_catalog_service()
    if reason is PluginUnavailableReason.NOT_INSTALLED:
        context = _context()
    else:
        view, snapshot = _restricted_policy_pair(catalog, PluginId("transform", plugin), reason)
        context = ToolContext(catalog=view, plugin_snapshot=snapshot)
    assert context.catalog.unavailable_reason(PluginId("transform", plugin)) is reason
    original = PolicyCatalogView.get_schema
    reads = []

    def schema(view, kind, name):
        if (kind, name) == ("transform", plugin):
            reads.append((kind, name))
            raise AssertionError("unadmitted transform schema was read")
        return original(view, kind, name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    result = _invoke(surface, tool, args, state, context)
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        reason.value,
        "node:after" if operation in {"upsert", "patch"} else "node:inserted",
        None,
    )
    assert result.plugin_schemas is None and reads == []


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_transform_admitted_schema_fault_escapes_actual_handler(
    monkeypatch: pytest.MonkeyPatch, surface: Surface, operation: Operation, error_type: type[Exception]
) -> None:
    tool, args, state = _operation(operation)
    context, before = _context(), state.to_dict()
    fault, reads = error_type("OWNED-TRANSFORM-SCHEMA-FAULT"), []
    original_schema = PolicyCatalogView.get_schema
    attribute, original_handler = {
        "upsert_node": ("_execute_upsert_node", transforms._execute_upsert_node),
        "patch_node_options": ("_execute_patch_node_options", transforms._execute_patch_node_options),
        "splice_transform": ("_execute_splice_transform", transforms._execute_splice_transform),
    }[tool]
    armed = False

    def schema(view, kind, name):
        if armed and (kind, name) == ("transform", "passthrough"):
            reads.append((kind, name))
            raise fault
        return original_schema(view, kind, name)

    def handler(arguments, saved, handler_context):
        nonlocal armed
        assert saved is state and handler_context.catalog.unavailable_reason(PluginId("transform", "passthrough")) is None
        armed = True
        try:
            return original_handler(arguments, saved, handler_context)
        finally:
            armed = False

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    monkeypatch.setattr(transforms, attribute, handler)
    with pytest.raises(error_type) as caught:
        _invoke(surface, tool, args, state, context)
    assert caught.value is fault and reads == [("transform", "passthrough")]
    assert state.to_dict() == before


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
def test_transform_invalid_options_keep_owned_tuple_and_admitted_schema(surface: Surface, operation: Operation) -> None:
    tool, args, state = _operation(operation, options={"schema": {"mode": "observed"}, "unknown_contract_option": "authored-invalid-value"})
    context = _context()
    result = _invoke(surface, tool, args, state, context)
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        "plugin_options_invalid",
        "node:after" if operation in {"upsert", "patch"} else "node:inserted",
        ("transform", "passthrough"),
    )
    assert "unknown_contract_option" in entry.message
    if surface == "handler":
        assert result.plugin_schemas is None
    else:
        assert result.to_dict()["plugin_schemas"] == {
            "transform/passthrough": context.catalog.get_schema("transform", "passthrough").model_dump(mode="json")
        }


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice"])
@pytest.mark.parametrize("lowering_error", [ValueError, RuntimeError])
def test_transform_profile_outage_retains_distinct_owned_tuple(
    monkeypatch: pytest.MonkeyPatch, surface: Surface, operation: Operation, lowering_error: type[Exception]
) -> None:
    catalog, registry, snapshot = profile_fixtures._context()
    profiled = profile_fixtures._state().nodes[0]
    plugin_id = PluginId("transform", profiled.plugin)
    assert plugin_id in snapshot.available
    context = ToolContext(catalog=PolicyCatalogView(catalog, snapshot, registry), plugin_snapshot=snapshot)
    options = deep_thaw(profiled.options)
    tool, args, state = _operation(operation, plugin=profiled.plugin, options=options)
    original = OperatorProfileRegistry.lower_options
    lowering_calls = []

    def lower(profiles, requested_plugin, *, alias, safe_options):
        if requested_plugin == plugin_id:
            assert alias == "prompt-default"
            lowering_calls.append((requested_plugin, alias, deep_thaw(safe_options)))
            raise lowering_error("PRIVATE-TRANSFORM-PROFILE-LOWERING-CANARY")
        return original(profiles, requested_plugin, alias=alias, safe_options=safe_options)

    monkeypatch.setattr(OperatorProfileRegistry, "lower_options", lower)
    result = _invoke(surface, tool, args, state, context)
    assert lowering_calls and all(call[:2] == (plugin_id, "prompt-default") for call in lowering_calls)
    assert any(call[2] == {key: value for key, value in options.items() if key != "profile"} for call in lowering_calls)
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        "profile_unavailable",
        "node:inserted" if operation == "splice" else "node:after",
        ("transform", profiled.plugin),
    )
    if surface == "dispatch":
        assert result.to_dict()["plugin_schemas"] == {
            f"transform/{profiled.plugin}": context.catalog.get_schema("transform", profiled.plugin).model_dump(mode="json")
        }
    for payload in (repr(result.to_dict()), serialize_tool_result(result), repr(_allowlisted_candidate_feedback(result))):
        assert "PRIVATE-TRANSFORM-PROFILE-LOWERING-CANARY" not in payload


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
def test_transform_unknown_authored_provider_remains_options_repair(surface: Surface, operation: Operation) -> None:
    options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
    options["provider"] = "not_an_authored_provider"
    tool, args, state = _operation(operation, plugin="llm", options=options)
    result = _invoke(surface, tool, args, state, _context())
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        "plugin_options_invalid",
        "node:after" if operation in {"upsert", "patch"} else "node:inserted",
        ("transform", "llm"),
    )
    assert "not_an_authored_provider" in entry.message


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
def test_transform_provider_path_is_static_in_response_history_redaction_and_planner(
    tmp_path: Path, surface: Surface, operation: Operation
) -> None:
    private_root = tmp_path / "PRIVATE-TRANSFORM-ROOT-CANARY"
    options = {
        "provider": "chroma",
        "provider_config": {"persist_directory": "/outside-session/authored-chroma", "collection": "docs"},
        "schema": {"mode": "observed"},
        "output_prefix": "rag_",
        "query_field": "text",
    }
    raw = transforms._validate_transform_provider_config_path(options, str(private_root), session_id="test-session")
    assert raw is not None and str(private_root) in raw
    tool, args, state = _operation(operation, plugin="rag_retrieval", options=options)
    context = _context(data_dir=str(private_root), session_id="test-session")
    assert context.catalog.unavailable_reason(PluginId("transform", "rag_retrieval")) is None
    result = _invoke(surface, tool, args, state, context)
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component) == (
        "provider_config_path_policy_invalid",
        "node:after" if operation in {"upsert", "patch"} else "node:inserted",
    )
    feedback = _allowlisted_candidate_feedback(result)
    assert "detail" not in feedback["validation"]["errors"][0]
    redacted = redact_tool_call_response(tool, result.to_dict(), telemetry=NoopRedactionTelemetry())
    assert redacted["validation"]["errors"][0]["error_code"] == "provider_config_path_policy_invalid"
    for payload in (repr(result.to_dict()), serialize_tool_result(result), repr(feedback), repr(redacted)):
        assert "PRIVATE-TRANSFORM-ROOT-CANARY" not in payload
    assert (
        entry.message
        == "Node options.provider_config.persist_directory is outside this session's permitted locations. Use an authorized session-local directory."
    )


@pytest.mark.parametrize("finalized", [False, True])
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
def test_transform_literal_credential_refusal_retains_complete_repair_payload(finalized: bool, operation: Operation) -> None:
    # The public guard refuses literal credentials before the handler. Exercise
    # the owner's independent defense, followed by its actual public finalizer.
    literal = "private-transform-credential-canary"
    tool, args, state = _operation(operation, options={"schema": {"mode": "observed"}, "api_key": literal})
    context = _context()
    result = _invoke("handler", tool, args, state, context)
    if finalized:
        result = finalize_tool_result(
            result,
            tool_name=tool,
            catalog=context.catalog,
            context=context,
            prior_validation=context.catalog.validate_composition_state(state).validation,
        )
    subject = "node:after" if operation in {"upsert", "patch"} else "node:inserted"
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component) == ("credential_literal_forbidden", subject)
    assert result.data is not None and set(result.data) == {"credential_fields", "components", "repair"}
    node_id = "after" if operation in {"upsert", "patch"} else "inserted"
    assert result.data["credential_fields"] == (f"{node_id}:api_key",)
    assert result.data["components"] == ({"component_id": node_id, "component_type": "node", "fields": ("api_key",)},)
    assert result.data["repair"]["inline_form"]["example_options"] == {"api_key": {"secret_ref": "<NAME>"}}
    assert result.data["repair"]["post_hoc_form"]["tool_sequence"] == ("list_secret_refs", "validate_secret_ref", "wire_secret_ref")
    for payload in (repr(result.to_dict()), serialize_tool_result(result), repr(_allowlisted_candidate_feedback(result))):
        assert literal not in payload


@pytest.mark.parametrize("operation", ["upsert", "patch", "splice", "replay"])
def test_transform_public_credential_guard_precedes_saved_refusal(operation: Operation) -> None:
    tool, args, state = _operation(
        operation,
        options={
            "schema": {"mode": "observed"},
            "api_key": "private-transform-credential-canary",  # secret-scan: allow-this-line
        },
    )
    before = state.to_dict()
    with pytest.raises(CredentialMaterialRefused):
        _invoke("dispatch", tool, args, state, _context())
    assert state.to_dict() == before


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_splice_preserves_new_counterfactual_errors_before_standing_findings(surface: Surface) -> None:
    tool, args, state = _operation("splice", options={"schema": {"mode": "observed"}, "required_input_fields": ["ghost_field"]})
    context = _context()
    prior = context.catalog.validate_composition_state(state).validation
    assert prior.errors and prior.warnings and prior.edge_contracts and prior.semantic_contracts
    result = _invoke(surface, tool, args, state, context)
    assert not result.success and result.updated_state is state and result.affected_nodes == ()
    leading, *following = result.validation.errors
    assert (leading.error_code, leading.rejected_component) == ("splice_validation_failed", None)
    forwarded = tuple(entry for entry in following if entry.component == "rejected_mutation")
    assert forwarded and all(entry.error_code is not None and entry.plugin_identity is None for entry in forwarded)
    assert any(
        entry.error_code == "schema_contract_violation" and entry.rejected_component == "node:inserted" and "ghost_field" in entry.message
        for entry in forwarded
    )
    prior_keys = {(entry.component, entry.error_code, entry.message) for entry in prior.errors}
    assert all((entry.rejected_component, entry.error_code, entry.message) not in prior_keys for entry in forwarded)
    standing = state.validate() if surface == "handler" else prior
    assert result.validation.errors == (leading, *forwarded, *standing.errors)
    assert result.validation.warnings == standing.warnings
    normalized = normalize_tool_result_validation(result, context.catalog)
    assert normalized.validation.errors == (leading, *forwarded, *prior.errors)
    feedback = _allowlisted_candidate_feedback(normalized)["validation"]["errors"]
    assert feedback[0]["error_code"] == "splice_validation_failed"
    assert any(entry["component"] == "node:inserted" and entry["error_code"] == "schema_contract_violation" for entry in feedback[1:])


def test_splice_codeless_boundary_is_preserved_and_planner_refuses_to_invent_code() -> None:
    tool, args, state = _operation("splice", options={"schema": {"mode": "observed"}, "required_input_fields": ["ghost_field"]})
    context = _context()
    real = _invoke("handler", tool, args, state, context)
    actual = next(
        entry
        for entry in real.validation.errors
        if entry.error_code == "schema_contract_violation" and entry.rejected_component == "node:inserted"
    )
    # Explicit boundary injection, not a claim that a natural contextual
    # producer currently emits this codeless finding. Keep its real attribution.
    codeless = replace(actual, component="node:inserted", error_code=None, rejected_component=None)
    result = transforms._splice_validation_rejection(
        state, candidate_errors=(codeless,), prior_errors=context.catalog.validate_composition_state(state).validation.errors
    )
    forwarded = result.validation.errors[1]
    assert forwarded.component == "rejected_mutation" and forwarded.rejected_component == "node:inserted"
    assert forwarded.error_code is None and forwarded.message == actual.message
    assert forwarded.plugin_identity is None
    normalized = normalize_tool_result_validation(result, context.catalog)
    assert normalized.validation.errors[1].error_code is None
    with pytest.raises(AuditIntegrityError, match="producer-owned error code"):
        _allowlisted_candidate_feedback(normalized)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "patch", "splice"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
async def test_transform_outer_fault_keeps_earlier_success_and_intermediate_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: Operation, error_type: type[Exception]
) -> None:
    def available(*, model: str, **kwargs: Any) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="offline")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    tool, args, state = _operation(operation)
    before = state.to_dict()
    _, prior_args, _ = _operation("upsert")
    prior_args["description"] = "Earlier completed transform edit"
    prior_result = _invoke("dispatch", "upsert_node", prior_args, state, _context())
    assert prior_result.success and prior_result.updated_state.version == 18
    assert prior_result.updated_state.nodes != state.nodes
    attribute, original_handler = {
        "upsert_node": ("_execute_upsert_node", transforms._execute_upsert_node),
        "patch_node_options": ("_execute_patch_node_options", transforms._execute_patch_node_options),
        "splice_transform": ("_execute_splice_transform", transforms._execute_splice_transform),
    }[tool]
    original_schema = PolicyCatalogView.get_schema
    fault, reads = error_type("OWNED-TRANSFORM-OUTER-SCHEMA-FAULT"), []
    armed = False

    def schema(view, kind, name):
        if armed and (kind, name) == ("transform", "passthrough"):
            reads.append((kind, name))
            raise fault
        return original_schema(view, kind, name)

    def handler(arguments, saved, handler_context):
        nonlocal armed
        if arguments != args:
            return original_handler(arguments, saved, handler_context)
        assert saved.version == 18 and saved.metadata == state.metadata
        assert saved.nodes == prior_result.updated_state.nodes and saved.edges == prior_result.updated_state.edges
        armed = True
        try:
            return original_handler(arguments, saved, handler_context)
        finally:
            armed = False

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    monkeypatch.setattr(transforms, attribute, handler)
    response = _make_llm_response(
        tool_calls=[
            {"id": "transform_prior", "name": "upsert_node", "arguments": prior_args},
            {"id": "transform_refused", "name": "patch_node_options", "arguments": {"node_id": "absent", "patch": {}}},
            {"id": "transform_fault", "name": tool, "arguments": args},
        ]
    )
    llm = create_autospec(service._provider_gateway._call_llm, return_value=response)
    monkeypatch.setattr(service._provider_gateway, "_call_llm", llm)
    recorder = BufferingRecorder()
    monkeypatch.setattr("elspeth.web.composer.service.BufferingRecorder", lambda: recorder)
    with pytest.raises(ComposerPluginCrashError) as caught:
        await service._run_one_turn_for_test(session_id=session_id, initial_state=state)
    assert reads == [("transform", "passthrough")] and llm.await_count == 1
    assert caught.value.original_exc is fault and caught.value.__cause__ is fault
    assert caught.value.partial_state is not None and caught.value.partial_state.version == 18
    assert caught.value.partial_state.metadata == state.metadata
    assert (
        caught.value.partial_state.nodes == prior_result.updated_state.nodes
        and caught.value.partial_state.edges == prior_result.updated_state.edges
    )
    assert state.to_dict() == before
    assert caught.value.failed_turn is not None and caught.value.failed_turn.tool_calls_attempted == 3
    assert [inv.tool_call_id for inv in recorder.invocations] == ["transform_prior", "transform_refused", "transform_fault"]
    assert [inv.status for inv in recorder.invocations] == [
        ComposerToolStatus.SUCCESS,
        ComposerToolStatus.SUCCESS,
        ComposerToolStatus.PLUGIN_CRASH,
    ]
    assert [inv.version_after for inv in recorder.invocations] == [18, 18, None]
    rows = _persisted_tool_responses(service, session_id)
    assert [row["tool_call_id"] for row in rows] == ["transform_prior", "transform_refused", "transform_fault"]
    assert json.loads(rows[0]["result_canonical"])["success"] is True
    refused = json.loads(rows[1]["result_canonical"])
    assert refused["success"] is False and refused["validation"]["errors"][0]["error_code"] == "node_not_found"
    assert json.loads(rows[2]["result_canonical"])["error_class"] == error_type.__name__
