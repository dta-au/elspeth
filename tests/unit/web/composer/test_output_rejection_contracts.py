"""Incremental output refusals describe the unchanged surviving graph."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest.mock import create_autospec

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, OutputSpec, ValidationSummary
from elspeth.web.composer.tools import execute_tool, outputs
from elspeth.web.composer.tools._common import ToolContext, ToolResult, _validate_sink_path, normalize_tool_result_validation
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from tests.unit.web.composer._helpers import _composer_service_with_session, _persisted_tool_responses
from tests.unit.web.composer.test_compose_loop_audit_wiring import _make_llm_response, _make_settings
from tests.unit.web.composer.test_semantic_validator import _wardline_state
from tests.unit.web.composer.test_tools import _empty_state, _restricted_policy_pair

Surface = Literal["handler", "dispatch"]


def _context(**kwargs: Any) -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot, **kwargs)


def _options() -> dict[str, Any]:
    return {
        "path": "outputs/test-session/main.json",
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "auto_increment",
    }


def _saved_state() -> CompositionState:
    state = _wardline_state(text_separator=" ", scrape_format="text")
    return replace(
        state,
        outputs=(*state.outputs, OutputSpec(name="main", plugin="json", options=_options(), on_write_failure="discard")),
        version=17,
    )


def _invoke(surface: Surface, tool: str, args: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
    if surface == "handler":
        handler = {
            "set_output": outputs._handle_set_output,
            "patch_output_options": outputs._handle_patch_output_options,
            "remove_output": outputs._handle_remove_output,
        }[tool]
        return handler(args, state, context)
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
    assert not result.success
    assert result.updated_state is state
    assert result.updated_state.version == 17
    assert result.affected_nodes == ()
    assert result.prior_validation is None
    assert "validation_delta" not in result.to_dict()
    leading = result.validation.errors[0]
    assert (leading.component, leading.error_code, leading.rejected_component) == ("rejected_mutation", code, subject)
    assert result.validation.errors[1:] == standing.errors
    assert result.validation.warnings == standing.warnings
    assert result.validation.suggestions == standing.suggestions
    assert result.validation.edge_contracts == standing.edge_contracts
    assert result.validation.semantic_contracts == standing.semantic_contracts


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options", "remove_output"])
def test_output_refusal_retains_real_saved_findings_across_policy_snapshots(surface: Surface, tool: str) -> None:
    state = _saved_state()
    before = state.to_dict()
    context = _context()
    args = {"sink_name": "absent"}
    code = "output_not_found"
    if tool == "set_output":
        args.update(plugin="json", options=_options(), on_write_failure=None)
        code = "failure_route_required"
    elif tool == "patch_output_options":
        args["patch"] = {"encoding": "utf-8"}
    standing = state.validate() if surface == "handler" else context.catalog.validate_composition_state(state).validation
    assert standing.errors and standing.warnings and standing.edge_contracts and standing.semantic_contracts
    result = _invoke(surface, tool, args, state, context)
    _assert_refusal(result, state, code, "output:absent", standing)
    other = replace(context.plugin_snapshot, snapshot_hash="packet1b-other-policy-snapshot")
    other_view = PolicyCatalogView.for_trained_operator(create_catalog_service(), other)
    result = normalize_tool_result_validation(result, other_view)
    _assert_refusal(result, state, code, "output:absent", other_view.validate_composition_state(state).validation)
    assert state.to_dict() == before
    if surface == "dispatch":
        guidance = result.validation_guidance
        assert guidance is not None and code in guidance["codes"]
        assert explain_validation_code(code) is not None


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
def test_output_path_refusal_is_safe_in_ordinary_response_and_history(tmp_path: Path, surface: Surface, tool: str) -> None:
    private_root = tmp_path / "NON-AUTHORED-OUTPUT-ROOT-CANARY"
    options = {**_options(), "path": "/outside-session/authored.json"}
    raw_error = _validate_sink_path(options, str(private_root), session_id="test-session")
    assert raw_error is not None and str(private_root) in raw_error
    context = _context(data_dir=str(private_root), session_id="test-session")
    state = _saved_state()
    before = state.to_dict()
    args = {"sink_name": "main", "patch": {"path": options["path"]}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "json", "options": options, "on_write_failure": "discard"}
    result = _invoke(surface, tool, args, state, context)
    assert not result.success
    assert result.updated_state is state and state.to_dict() == before
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component) == ("sink_path_policy_invalid", "output:main")
    assert "authorized session-local output path" in entry.message
    assert "NON-AUTHORED-OUTPUT-ROOT-CANARY" not in repr(result.to_dict())
    assert "NON-AUTHORED-OUTPUT-ROOT-CANARY" not in serialize_tool_result(result)
    assert entry.plugin_identity is None
    assert result.plugin_schemas is None
    if surface == "dispatch":
        assert result.validation_guidance is not None
        assert "do not guess a server filesystem root" in result.validation_guidance["codes"][entry.error_code]["suggested_fix"]


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
@pytest.mark.parametrize("reason", [PluginUnavailableReason.NOT_AUTHORIZED, PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING])
def test_output_policy_refusal_retains_actual_reason_without_schema_identity(
    monkeypatch: pytest.MonkeyPatch, surface: Surface, tool: str, reason: PluginUnavailableReason
) -> None:
    catalog = create_catalog_service()
    view, snapshot = _restricted_policy_pair(catalog, PluginId("sink", "json"), reason)
    context = ToolContext(catalog=view, plugin_snapshot=snapshot)
    state = _saved_state()
    args = {"sink_name": "main", "patch": {"encoding": "utf-8"}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "json", "options": _options(), "on_write_failure": "discard"}
    original_schema = PolicyCatalogView.get_schema
    schema_calls = []

    def schema(view, kind, name):
        if (kind, name) == ("sink", "json"):
            schema_calls.append((kind, name))
            raise AssertionError("unadmitted output schema was read")
        return original_schema(view, kind, name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    result = _invoke(surface, tool, args, state, context)
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component) == (reason.value, "output:main")
    assert entry.plugin_identity is None
    assert result.plugin_schemas is None
    assert schema_calls == []
    if surface == "dispatch":
        assert result.validation_guidance is not None and reason.value in result.validation_guidance["codes"]


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
def test_output_unknown_plugin_has_real_not_installed_reason_and_no_schema_read(
    monkeypatch: pytest.MonkeyPatch,
    surface: Surface,
    tool: str,
) -> None:
    state, context = _saved_state(), _context()
    plugin = "not_installed_output_contract_sink"
    assert context.catalog.unavailable_reason(PluginId("sink", plugin)) is PluginUnavailableReason.NOT_INSTALLED
    state = replace(state, outputs=(*state.outputs[:-1], replace(state.outputs[-1], plugin=plugin)))
    before = state.to_dict()
    args = {"sink_name": "main", "patch": {"encoding": "utf-8"}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": plugin, "options": _options(), "on_write_failure": "discard"}
    original_schema = PolicyCatalogView.get_schema
    schema_calls = []

    def schema(view, kind, name):
        if (kind, name) == ("sink", plugin):
            schema_calls.append((kind, name))
            raise AssertionError("uninstalled output schema was read")
        return original_schema(view, kind, name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    result = _invoke(surface, tool, args, state, context)
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state and state.to_dict() == before
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == ("plugin_not_installed", "output:main", None)
    assert result.plugin_schemas is None and schema_calls == []


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_output_admitted_schema_fault_propagates_from_the_actual_handler(
    monkeypatch: pytest.MonkeyPatch,
    surface: Surface,
    tool: str,
    error_type: type[Exception],
) -> None:
    state, context = _saved_state(), _context()
    before = state.to_dict()
    assert context.catalog.unavailable_reason(PluginId("sink", "json")) is None
    args = {"sink_name": "main", "patch": {"encoding": "utf-8"}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "json", "options": _options(), "on_write_failure": "discard"}
    original_schema = PolicyCatalogView.get_schema
    original_handler = outputs._execute_set_output if tool == "set_output" else outputs._execute_patch_output_options
    in_handler = False
    schema_calls = []
    fault = error_type("OWNED-OUTPUT-SCHEMA-FAULT-CANARY")

    def schema(view, kind, name):
        if in_handler and (kind, name) == ("sink", "json"):
            schema_calls.append((kind, name))
            raise fault
        return original_schema(view, kind, name)

    def handler(arguments, saved, handler_context):
        nonlocal in_handler
        assert saved is state
        in_handler = True
        try:
            return original_handler(arguments, saved, handler_context)
        finally:
            in_handler = False

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    if tool == "set_output":
        monkeypatch.setattr(outputs, "_execute_set_output", handler)
    else:
        monkeypatch.setattr(outputs, "_execute_patch_output_options", handler)
    with pytest.raises(error_type) as caught:
        _invoke(surface, tool, args, state, context)
    assert caught.value is fault and schema_calls == [("sink", "json")]
    assert state.to_dict() == before and state.version == 17


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
def test_output_admitted_options_keep_authored_detail_and_exact_schema(surface: Surface, tool: str) -> None:
    state = _saved_state()
    context = _context()
    invalid = {"schema": {"mode": "AUTHORED-OUTPUT-OPTION-CANARY"}}
    args = {"sink_name": "main", "patch": invalid}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "json", "options": {**_options(), **invalid}, "on_write_failure": "discard"}
    result = _invoke(surface, tool, args, state, context)
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        "plugin_options_invalid",
        "output:main",
        ("sink", "json"),
    )
    assert "AUTHORED-OUTPUT-OPTION-CANARY" in entry.message
    if surface == "dispatch":
        assert result.to_dict()["plugin_schemas"] == {"sink/json": context.catalog.get_schema("sink", "json").model_dump(mode="json")}


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
def test_output_credential_refusal_keeps_complete_repair_payload(surface: Surface, tool: str) -> None:
    literal = "postgresql://db.example.invalid/elspeth"
    options = {"url": {"secret_ref": "DB"}, "table": "events", "schema": {"mode": "observed"}}
    state = _saved_state()
    state = replace(state, outputs=(*state.outputs[:-1], replace(state.outputs[-1], plugin="database", options=options)))
    context = _context()
    args = {"sink_name": "main", "patch": {"url": literal}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "database", "options": {**options, "url": literal}, "on_write_failure": "discard"}
    result = _invoke(surface, tool, args, state, context)
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component) == ("credential_literal_forbidden", "output:main")
    assert result.data is not None
    assert set(result.data) == {"credential_fields", "components", "repair"}
    assert result.data["credential_fields"] == ("main:url",)
    assert result.data["components"] == ({"component_id": "main", "component_type": "output", "fields": ("url",)},)
    repair = result.data["repair"]
    assert set(repair) == {"inline_form", "post_hoc_form"}
    assert repair["inline_form"]["example_options"] == {"url": {"secret_ref": "<NAME>"}}
    assert "patch_output_options" in repair["inline_form"]["instruction"]
    assert repair["post_hoc_form"]["tool_sequence"] == ("list_secret_refs", "validate_secret_ref", "wire_secret_ref")
    assert repair["post_hoc_form"]["instruction"]
    assert literal not in repr(result.to_dict())


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_output_whole_state_invariant_is_not_falsely_attributed_to_output(surface: Surface) -> None:
    state = _saved_state()
    state = replace(state, nodes=(replace(state.nodes[0], timeout_seconds=5.0), *state.nodes[1:]))
    args = {"sink_name": "main", "plugin": "json", "options": _options(), "on_write_failure": "discard"}
    result = _invoke(surface, "set_output", args, state, _context())
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert entry.error_code == "node_timeout_unsupported"
    assert entry.rejected_component is None


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_output_successes_keep_authorized_paths_update_semantics_and_explicit_failure_choice(tmp_path: Path, surface: Surface) -> None:
    context = _context(data_dir=str(tmp_path), session_id="test-session")
    original = _empty_state()
    created = _invoke(
        surface,
        "set_output",
        {"sink_name": "main", "plugin": "json", "options": _options(), "on_write_failure": "discard"},
        original,
        context,
    )
    assert created.success and original.outputs == ()
    replaced = _invoke(
        surface, "set_output", {"sink_name": "main", "plugin": "json", "options": _options()}, created.updated_state, context
    )
    assert replaced.success and replaced.updated_state.outputs[0].on_write_failure == "discard"
    patched = _invoke(
        surface, "patch_output_options", {"sink_name": "main", "patch": {"encoding": "utf-8"}}, replaced.updated_state, context
    )
    assert patched.success
    assert patched.updated_state.outputs[0].options["path"] == _options()["path"]
    assert patched.updated_state.outputs[0].on_write_failure == "discard"
    removed = _invoke(surface, "remove_output", {"sink_name": "main"}, patched.updated_state, context)
    assert removed.success and removed.updated_state.outputs == ()
    assert [r.updated_state.version for r in (created, replaced, patched, removed)] == [2, 3, 4, 5]
    assert all(r.affected_nodes == ("main",) for r in (created, replaced, patched, removed))


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
def test_output_collision_policy_refusal_has_its_own_code(surface: Surface, tool: str) -> None:
    state = _saved_state()
    context = _context(data_dir="/data", session_id="test-session")
    options = _options()
    del options["collision_policy"]
    args = {"sink_name": "main", "patch": {"collision_policy": None}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "json", "options": options, "on_write_failure": "discard"}
    result = _invoke(surface, tool, args, state, context)
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component) == ("file_sink_write_policy_invalid", "output:main")
    assert "collision_policy" in entry.message


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["set_output", "patch_output_options"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
async def test_output_outer_schema_fault_preserves_earlier_completed_state_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    error_type: type[Exception],
) -> None:
    def available(*, model: str, **kwargs: Any) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="offline")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    state = _saved_state()
    before = state.to_dict()
    args = {"sink_name": "main", "patch": {"encoding": "utf-8"}}
    if tool == "set_output":
        args = {"sink_name": "main", "plugin": "json", "options": _options(), "on_write_failure": "discard"}
    original_schema = PolicyCatalogView.get_schema
    original_handler = outputs._execute_set_output if tool == "set_output" else outputs._execute_patch_output_options
    fault = error_type("OWNED-OUTPUT-SCHEMA-FAULT-CANARY")
    in_handler = False
    schema_calls = []

    def schema(view, kind, name):
        if in_handler and (kind, name) == ("sink", "json"):
            schema_calls.append((kind, name))
            raise fault
        return original_schema(view, kind, name)

    def handler(arguments, saved, handler_context):
        nonlocal in_handler
        assert saved.version == 18 and saved.metadata.name == "Earlier completed output edit"
        assert saved.outputs == state.outputs
        assert handler_context.catalog.unavailable_reason(PluginId("sink", "json")) is None
        in_handler = True
        try:
            return original_handler(arguments, saved, handler_context)
        finally:
            in_handler = False

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    if tool == "set_output":
        monkeypatch.setattr(outputs, "_execute_set_output", handler)
    else:
        monkeypatch.setattr(outputs, "_execute_patch_output_options", handler)
    response = _make_llm_response(
        tool_calls=[
            {"id": "output_prior", "name": "set_metadata", "arguments": {"patch": {"name": "Earlier completed output edit"}}},
            {"id": "output_fault", "name": tool, "arguments": args},
        ]
    )
    llm = create_autospec(service._provider_gateway._call_llm, return_value=response)
    monkeypatch.setattr(service._provider_gateway, "_call_llm", llm)
    recorder = BufferingRecorder()
    monkeypatch.setattr("elspeth.web.composer.service.BufferingRecorder", lambda: recorder)
    with pytest.raises(ComposerPluginCrashError) as caught:
        await service._run_one_turn_for_test(session_id=session_id, initial_state=state)
    assert schema_calls == [("sink", "json")] and llm.await_count == 1
    assert caught.value.original_exc is fault and caught.value.__cause__ is fault
    assert caught.value.partial_state is not None
    assert caught.value.partial_state.version == 18 and caught.value.partial_state.metadata.name == "Earlier completed output edit"
    assert caught.value.partial_state.outputs == state.outputs and state.to_dict() == before
    assert caught.value.failed_turn is not None and caught.value.failed_turn.tool_calls_attempted == 2
    invocations = recorder.invocations
    assert [r.tool_call_id for r in invocations] == ["output_prior", "output_fault"]
    assert [r.status for r in invocations] == [ComposerToolStatus.SUCCESS, ComposerToolStatus.PLUGIN_CRASH]
    assert [r.version_after for r in invocations] == [18, None]
    assert invocations[1].error_class == error_type.__name__
    rows = _persisted_tool_responses(service, session_id)
    assert [row["tool_call_id"] for row in rows] == ["output_prior", "output_fault"]
    assert json.loads(rows[0]["result_canonical"])["success"] is True
    assert json.loads(rows[1]["result_canonical"])["error_class"] == error_type.__name__
