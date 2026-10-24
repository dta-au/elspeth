"""Composer discovery delegation, tool schemas, and registry contract tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest.mock import call, create_autospec

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.plugins.infrastructure.manager import PluginManager, PluginNotFoundError
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
from elspeth.web.composer.protocol import ComposerPluginCrashError, ToolArgumentError
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import generation, get_expression_grammar, get_tool_definitions
from elspeth.web.composer.tools._common import ToolContext, ToolResult, normalize_tool_result_validation
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context
from elspeth.web.composer.tools._dispatch import execute_tool as dispatch_tool
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginId, PluginUnavailableReason
from tests.unit.web.composer._helpers import _composer_service_with_session, _persisted_tool_responses
from tests.unit.web.composer.test_compose_loop_audit_wiring import _make_llm_response, _make_settings
from tests.unit.web.composer.test_output_rejection_contracts import _context, _saved_state
from tests.unit.web.composer.test_tools import (
    _empty_state,
    _mock_catalog,
    _restricted_policy_pair,
    execute_tool,
)
from tests.unit.web.composer.test_transform_rejection_contracts import _assert_refusal


class TestDiscoveryTools:
    def test_list_sources_delegates(self) -> None:
        catalog = _mock_catalog()
        result = execute_tool("list_sources", {}, _empty_state(), catalog)
        assert result.success is True
        assert catalog.list_sources.call_count == 2  # snapshot construction + dispatch

    def test_list_transforms_delegates(self) -> None:
        catalog = _mock_catalog()
        result = execute_tool("list_transforms", {}, _empty_state(), catalog)
        assert result.success is True
        assert catalog.list_transforms.call_count == 2  # snapshot construction + dispatch

    def test_list_sinks_delegates(self) -> None:
        catalog = _mock_catalog()
        result = execute_tool("list_sinks", {}, _empty_state(), catalog)
        assert result.success is True
        assert catalog.list_sinks.call_count == 2  # snapshot construction + dispatch

    def test_get_plugin_schema_delegates(self) -> None:
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_schema",
            {"plugin_type": "source", "name": "csv"},
            _empty_state(),
            catalog,
        )
        assert result.success is True
        assert catalog.get_schema.call_args_list == [call("source", "csv")]

    def test_list_transforms_hides_snapshot_unavailable_plugin(self) -> None:
        catalog = _mock_catalog()
        policy_catalog, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("transform", "azure_prompt_shield"),
            PluginUnavailableReason.CREDENTIAL_MISSING,
        )

        result = execute_tool(
            "list_transforms",
            {},
            _empty_state(),
            policy_catalog,
            plugin_snapshot=snapshot,
        )

        assert result.success is True
        names = {item.name for item in result.data["available"]}
        assert "passthrough" in names
        assert "azure_prompt_shield" not in names

    def test_list_transforms_shows_snapshot_available_plugin(self) -> None:
        catalog = _mock_catalog()

        result = execute_tool(
            "list_transforms",
            {},
            _empty_state(),
            catalog,
        )

        assert result.success is True
        names = {item.name for item in result.data["available"]}
        assert "azure_prompt_shield" in names

    def test_get_plugin_schema_rejects_snapshot_unavailable_plugin(self) -> None:
        catalog = _mock_catalog()
        policy_catalog, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("transform", "azure_prompt_shield"),
            PluginUnavailableReason.CREDENTIAL_MISSING,
        )

        result = execute_tool(
            "get_plugin_schema",
            {"plugin_type": "transform", "name": "azure_prompt_shield"},
            _empty_state(),
            policy_catalog,
            plugin_snapshot=snapshot,
        )

        assert result.success is False
        assert result.validation.errors[0].error_code == "credential_unavailable"
        assert "azure_prompt_shield" not in result.validation.errors[0].message
        assert result.data is None

    def test_get_plugin_assistance_rejects_snapshot_unavailable_plugin(self) -> None:
        catalog = _mock_catalog()
        policy_catalog, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("transform", "azure_prompt_shield"),
            PluginUnavailableReason.CREDENTIAL_MISSING,
        )

        result = execute_tool(
            "get_plugin_assistance",
            {"plugin_type": "transform", "plugin_name": "azure_prompt_shield"},
            _empty_state(),
            policy_catalog,
            plugin_snapshot=snapshot,
        )

        assert result.success is False
        assert result.validation.errors[0].error_code == "credential_unavailable"
        assert "azure_prompt_shield" not in result.validation.errors[0].message
        assert result.data is None

    def test_get_expression_grammar_is_static(self) -> None:
        grammar = get_expression_grammar()
        assert "row" in grammar
        assert isinstance(grammar, str)

    def test_expression_grammar_documents_every_operator(self) -> None:
        """Text↔parser doc-parity tripwire: every operator the parser accepts
        has a token in the served Operators section (A4).

        The operator sets map AST types to callables, so the token table here
        is hand-maintained — the completeness assertion runs FIRST so an
        operator added to the parser without a table entry fails loudly
        before any text check.
        """
        import ast as _ast

        from elspeth.core.expression_parser import (
            _BINARY_OPS,
            _BOOL_OPS,
            _COMPARISON_OPS,
            _SAFE_CONSTANTS,
            _UNARY_OPS,
        )

        token_table: dict[type, str] = {
            _ast.Eq: "==",
            _ast.NotEq: "!=",
            _ast.Lt: "<",
            _ast.LtE: "<=",
            _ast.Gt: ">",
            _ast.GtE: ">=",
            _ast.Is: "is",
            _ast.IsNot: "is not",
            _ast.In: "in",
            _ast.NotIn: "not in",
            _ast.Add: "+",
            _ast.Sub: "-",
            _ast.Mult: "*",
            _ast.Div: "/",
            _ast.FloorDiv: "//",
            _ast.Mod: "%",
            _ast.Not: "not",
            _ast.USub: "-",
            _ast.UAdd: "+",
            _ast.And: "and",
            _ast.Or: "or",
        }
        # Completeness before text: an unmapped operator must fail here, not
        # silently skip the documentation check.
        assert set(token_table) == set(_COMPARISON_OPS) | set(_BINARY_OPS) | set(_UNARY_OPS) | set(_BOOL_OPS)

        grammar = get_expression_grammar()
        operators_section = grammar.split("Operators:")[1].split("Built-in functions")[0]
        for op_type, token in token_table.items():
            assert token in operators_section, f"{op_type.__name__} token {token!r} missing from Operators section"

        # Safe constants are part of the accepted grammar too.
        for constant in _SAFE_CONSTANTS:
            assert constant in grammar, f"constant {constant!r} undocumented"

    def test_expression_grammar_documents_every_builtin_bidirectionally(self) -> None:
        """Text↔parser doc-parity for builtins, both directions (A4).

        Every key of _SAFE_BUILTINS appears in the Built-in functions
        section, and nothing is documented there that is not in the set —
        so growing _SAFE_BUILTINS without the text update (or vice versa)
        fails here, inside the same commit.
        """
        import re as _re

        from elspeth.core.expression_parser import _SAFE_BUILTINS

        grammar = get_expression_grammar()
        builtins_section = grammar.split("Built-in functions")[1].split("Type coercion functions")[0]

        for name in _SAFE_BUILTINS:
            assert f"{name}(" in builtins_section, f"builtin {name!r} undocumented"

        documented = set(_re.findall(r"^\s*([a-z_]+)\(", builtins_section, flags=_re.MULTILINE))
        assert documented == set(_SAFE_BUILTINS), f"documented builtins {sorted(documented)} != parser set {sorted(_SAFE_BUILTINS)}"


DiscoverySurface = Literal["handler", "dispatch", "readonly"]


def _invoke_discovery(
    surface: DiscoverySurface, tool: str, arguments: dict[str, Any], state: CompositionState, context: ToolContext
) -> ToolResult:
    if surface == "handler":
        return {
            "get_plugin_schema": generation._handle_get_plugin_schema,
            "get_plugin_assistance": generation._execute_get_plugin_assistance,
            "diff_pipeline": generation._execute_diff_pipeline,
        }[tool](arguments, state, context)
    if surface == "readonly":
        return execute_discovery_tool_with_context(tool, arguments, state, context)
    return dispatch_tool(
        tool,
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        baseline=context.baseline,
        prior_validation=context.current_validation,
        validate_arguments=True,
        raise_schema_argument_errors=True,
    )


def _assert_discovery_refusal(
    result: ToolResult, state: CompositionState, context: ToolContext, surface: DiscoverySurface, tool: str, code: str
) -> None:
    standing = state.validate() if surface == "handler" else context.catalog.validate_composition_state(state).validation
    assert standing.errors and standing.warnings and standing.edge_contracts and standing.semantic_contracts
    _assert_refusal(result, state, code, None, standing)
    assert result.validation.errors[0].plugin_identity is None
    assert result.plugin_schemas is None and result.validation_guidance is None
    # Canonical actual policy change, not merely another hash of identical facts.
    output = state.outputs[0]
    other, snapshot = _restricted_policy_pair(create_catalog_service(), PluginId("sink", output.plugin))
    assert snapshot.snapshot_hash != context.plugin_snapshot.snapshot_hash
    other_standing = other.validate_composition_state(state).validation
    assert other_standing != context.catalog.validate_composition_state(state).validation
    assert any((entry.component, entry.error_code) == (f"output:{output.name}", "plugin_not_enabled") for entry in other_standing.errors)
    normalized = normalize_tool_result_validation(result, other)
    _assert_refusal(normalized, state, code, None, other_standing)
    admitted = admit_discovery_result(tool, result)
    assert admitted.response is None
    wire = json.loads(serialize_admitted_discovery_result(admitted))
    assert wire == admitted.to_dict() and admitted.to_tool_result().to_dict() == result.to_dict()
    assert wire["success"] is False and wire["version"] == state.version
    assert wire["validation"]["errors"][0]["error_code"] == code
    assert not {"data", "plugin_schemas", "validation_guidance", "prior_validation", "validation_delta"} & wire.keys()
    feedback = _allowlisted_candidate_feedback(result)["validation"]["errors"]
    assert len(feedback) == 1 and feedback[0]["error_code"] == code
    assert feedback[0]["component"] == "rejected_mutation" and "detail" not in feedback[0]


@pytest.mark.parametrize("surface", ["handler", "dispatch", "readonly"])
@pytest.mark.parametrize("tool", ["get_plugin_schema", "get_plugin_assistance"])
@pytest.mark.parametrize(
    "reason",
    [
        PluginUnavailableReason.NOT_AUTHORIZED,
        PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING,
        PluginUnavailableReason.CREDENTIAL_MISSING,
        None,
    ],
    ids=["not-authorized", "local-requirement", "credential", "not-installed"],
)
def test_discovery_denial_precedes_requested_access_and_preserves_saved_findings(
    monkeypatch: pytest.MonkeyPatch, surface: DiscoverySurface, tool: str, reason: PluginUnavailableReason | None
) -> None:
    state, full = _saved_state(), create_catalog_service()
    name = "private_discovery_plugin_canary" if reason is None else "azure_prompt_shield"
    plugin_id = PluginId("transform", name)
    if reason is None:
        context = _context()
        expected_reason = PluginUnavailableReason.NOT_INSTALLED
    else:
        view, snapshot = _restricted_policy_pair(full, plugin_id, reason)
        context, expected_reason = ToolContext(catalog=view, plugin_snapshot=snapshot), reason
    assert context.catalog.unavailable_reason(plugin_id) is expected_reason
    before, reads, managers = state.to_dict(), [], []
    original = PolicyCatalogView.get_schema

    def schema(view, kind, requested):
        if view is context.catalog and (kind, requested) == (plugin_id.kind, plugin_id.name):
            reads.append((kind, requested))
            raise AssertionError("denied discovery schema was read")
        return original(view, kind, requested)

    def manager():
        managers.append("requested discovery manager")
        raise AssertionError("denied discovery manager was read")

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    monkeypatch.setattr(generation, "get_shared_plugin_manager", manager)
    arguments = {"plugin_type": "transform", "name" if tool == "get_plugin_schema" else "plugin_name": name}
    result = _invoke_discovery(surface, tool, arguments, state, context)
    assert reads == [] and managers == []
    _assert_discovery_refusal(result, state, context, surface, tool, expected_reason.value)
    assert "private_discovery_plugin_canary" not in repr(result.to_dict())
    assert state.to_dict() == before


@pytest.mark.parametrize("surface", ["handler", "dispatch", "readonly"])
@pytest.mark.parametrize("tool", ["get_plugin_schema", "get_plugin_assistance"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_discovery_admitted_schema_fault_escapes_exact_owner(
    monkeypatch: pytest.MonkeyPatch, surface: DiscoverySurface, tool: str, error_type: type[Exception]
) -> None:
    state, context = _saved_state(), _context()
    assert generation._plugin_selection_violation(context, "source", "json") is None
    assert context.catalog.get_schema("source", "json").name == "json"
    fault, calls, before = error_type("PRIVATE-DISCOVERY-SCHEMA-FAULT"), [], state.to_dict()
    original = PolicyCatalogView.get_schema

    def schema(view, kind, name):
        if view is context.catalog and (kind, name) == ("source", "json"):
            calls.append((kind, name))
            raise fault
        return original(view, kind, name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    arguments = {"plugin_type": "source", "name" if tool == "get_plugin_schema" else "plugin_name": "json"}
    with pytest.raises(error_type) as caught:
        _invoke_discovery(surface, tool, arguments, state, context)
    assert caught.value is fault and calls == [("source", "json")]
    assert state.to_dict() == before


@pytest.mark.parametrize("surface", ["handler", "dispatch", "readonly"])
@pytest.mark.parametrize(("kind", "name"), [("source", "json"), ("transform", "passthrough"), ("sink", "csv")])
def test_discovery_admitted_manager_inconsistency_escapes_actual_lookup(
    monkeypatch: pytest.MonkeyPatch, surface: DiscoverySurface, kind: str, name: str
) -> None:
    state, context = _saved_state(), _context()
    assert generation._plugin_selection_violation(context, kind, name) is None
    assert context.catalog.get_schema(kind, name).name == name
    # A separate empty owned manager is deliberately inconsistent with the
    # admitted full catalog. Its actual lookup produces the exception.
    isolated = PluginManager()
    attribute = {"source": "get_source_by_name", "transform": "get_transform_by_name", "sink": "get_sink_by_name"}[kind]
    lookup_owner = {"source": isolated.get_source_by_name, "transform": isolated.get_transform_by_name, "sink": isolated.get_sink_by_name}[
        kind
    ]
    original_schema, events, faults, before = PolicyCatalogView.get_schema, [], [], state.to_dict()

    def schema(view, requested_kind, requested_name):
        if view is context.catalog and (requested_kind, requested_name) == (kind, name):
            events.append("schema")
        return original_schema(view, requested_kind, requested_name)

    def manager():
        assert events == ["schema"]
        events.append("manager")
        return isolated

    def lookup(requested):
        assert requested == name and events == ["schema", "manager"]
        events.append("lookup")
        try:
            return lookup_owner(requested)
        except PluginNotFoundError as exc:
            faults.append(exc)
            raise

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    monkeypatch.setattr(generation, "get_shared_plugin_manager", manager)
    monkeypatch.setattr(isolated, attribute, lookup)
    with pytest.raises(PluginNotFoundError) as caught:
        _invoke_discovery(surface, "get_plugin_assistance", {"plugin_type": kind, "plugin_name": name}, state, context)
    assert events == ["schema", "manager", "lookup"] and faults == [caught.value]
    assert "Available" in str(caught.value) and state.to_dict() == before


@pytest.mark.parametrize("surface", ["handler", "dispatch", "readonly"])
@pytest.mark.parametrize(("kind", "name"), [("source", "csv"), ("transform", "web_scrape"), ("sink", "json")])
def test_discovery_healthy_schema_and_assistance_keep_one_admitted_read(
    monkeypatch: pytest.MonkeyPatch, surface: DiscoverySurface, kind: str, name: str
) -> None:
    context, state = _context(), _empty_state()
    expected, calls, original = context.catalog.get_schema(kind, name), [], PolicyCatalogView.get_schema

    def schema(view, requested_kind, requested_name):
        if view is context.catalog:
            calls.append((requested_kind, requested_name))
        return original(view, requested_kind, requested_name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    result = _invoke_discovery(surface, "get_plugin_schema", {"plugin_type": kind, "name": name}, state, context)
    assert calls == [(kind, name)] and result.success and result.updated_state is state
    assert admit_discovery_result("get_plugin_schema", result).response.to_wire() == expected.model_dump(mode="json")
    calls.clear()
    assistance = _invoke_discovery(surface, "get_plugin_assistance", {"plugin_type": kind, "plugin_name": name}, state, context)
    assert calls == [(kind, name)] and assistance.success and assistance.updated_state is state
    payload = admit_discovery_result("get_plugin_assistance", assistance).response.to_wire()
    assert (payload["plugin_type"], payload["plugin_name"], payload["issue_code"]) == (kind, name, None)
    assert payload["summary"] and payload["composer_hints"]


@pytest.mark.parametrize("surface", ["handler", "dispatch", "readonly"])
@pytest.mark.parametrize("tool", ["get_plugin_schema", "get_plugin_assistance"])
@pytest.mark.parametrize("bad", ["kind", "missing-name"])
def test_discovery_invalid_arguments_remain_typed_before_requested_access(
    monkeypatch: pytest.MonkeyPatch, surface: DiscoverySurface, tool: str, bad: str
) -> None:
    context, state = _context(), _saved_state()
    arguments = {"plugin_type": "source"}
    if bad == "kind":
        arguments = {"plugin_type": "not_a_plugin_kind", "name" if tool == "get_plugin_schema" else "plugin_name": "json"}
    calls = []

    def selection(*args):
        calls.append(args)
        raise AssertionError("invalid discovery arguments reached selection")

    monkeypatch.setattr(generation, "_plugin_selection_violation", selection)
    with pytest.raises(ToolArgumentError):
        _invoke_discovery(surface, tool, arguments, state, context)
    assert calls == []


def test_discovery_feedback_excludes_private_saved_diagnostics_but_authenticated_raw_keeps_them() -> None:
    state, context = _saved_state(), _context()
    canary = "PRIVATE-DISCOVERY-SAVED-ROUTE-CANARY"
    source = state.sources["source"]
    state = replace(state, sources={"source": replace(source, on_success=canary)})
    result = generation._execute_diff_pipeline({}, state, context)
    assert not result.success and result.updated_state is state
    assert any(entry.component == "source" and canary in entry.message for entry in result.validation.warnings)
    raw = serialize_admitted_discovery_result(admit_discovery_result("diff_pipeline", result))
    assert canary in raw
    feedback = _allowlisted_candidate_feedback(result)
    assert canary not in repr(feedback)
    assert len(feedback["validation"]["errors"]) == 1 and "detail" not in feedback["validation"]["errors"][0]
    redacted = redact_tool_call_response("diff_pipeline", result.to_dict(), telemetry=NoopRedactionTelemetry())
    assert canary not in repr(redacted)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_plugin_schema", "get_plugin_assistance"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
async def test_discovery_outer_fault_preserves_prior_success_and_completed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, error_type: type[Exception]
) -> None:
    def available(*, model: str, **kwargs: Any) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="offline")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    state, context = _saved_state(), _context()
    prior_args = {"patch": {"description": "Earlier completed discovery-turn edit"}}
    prior = dispatch_tool("set_metadata", prior_args, state, context.catalog, plugin_snapshot=context.plugin_snapshot)
    assert prior.success and prior.updated_state.version == 18 and prior.updated_state.metadata != state.metadata
    args = {"plugin_type": "source", "name" if tool == "get_plugin_schema" else "plugin_name": "json"}
    fault, reads, before = error_type("PRIVATE-DISCOVERY-OUTER-FAULT"), [], state.to_dict()
    original_selection, original_schema = generation._plugin_selection_violation, PolicyCatalogView.get_schema
    recorder, armed = BufferingRecorder(), False

    def selection(handler_context, kind, name):
        nonlocal armed
        outcome = original_selection(handler_context, kind, name)
        if (kind, name) == ("source", "json"):
            assert outcome is None
            assert [inv.tool_call_id for inv in recorder.invocations] == ["discovery_prior", "discovery_refused"]
            assert [inv.version_after for inv in recorder.invocations] == [18, 18]
            armed = True
        return outcome

    def schema(view, kind, name):
        nonlocal armed
        if armed and (kind, name) == ("source", "json"):
            reads.append((kind, name))
            armed = False
            raise fault
        return original_schema(view, kind, name)

    monkeypatch.setattr(generation, "_plugin_selection_violation", selection)
    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    response = _make_llm_response(
        tool_calls=[
            {"id": "discovery_prior", "name": "set_metadata", "arguments": prior_args},
            {
                "id": "discovery_refused",
                "name": "get_plugin_schema",
                "arguments": {"plugin_type": "transform", "name": "not_installed_discovery_plugin"},
            },
            {"id": "discovery_fault", "name": tool, "arguments": args},
        ]
    )
    llm = create_autospec(service._provider_gateway._call_llm, return_value=response)
    monkeypatch.setattr(service._provider_gateway, "_call_llm", llm)
    monkeypatch.setattr("elspeth.web.composer.service.BufferingRecorder", lambda: recorder)
    with pytest.raises(ComposerPluginCrashError) as caught:
        await service._run_one_turn_for_test(session_id=session_id, initial_state=state)
    assert reads == [("source", "json")] and llm.await_count == 1
    assert caught.value.original_exc is fault and caught.value.__cause__ is fault
    assert caught.value.partial_state is not None and caught.value.partial_state.version == 18
    assert caught.value.partial_state.metadata == prior.updated_state.metadata
    assert caught.value.partial_state.nodes == state.nodes and state.to_dict() == before
    assert caught.value.failed_turn is not None and caught.value.failed_turn.tool_calls_attempted == 3
    assert [inv.tool_call_id for inv in recorder.invocations] == ["discovery_prior", "discovery_refused", "discovery_fault"]
    assert [inv.status for inv in recorder.invocations] == [
        ComposerToolStatus.SUCCESS,
        ComposerToolStatus.SUCCESS,
        ComposerToolStatus.PLUGIN_CRASH,
    ]
    assert [inv.version_after for inv in recorder.invocations] == [18, 18, None]
    rows = _persisted_tool_responses(service, session_id)
    assert [row["tool_call_id"] for row in rows] == ["discovery_prior", "discovery_refused", "discovery_fault"]
    refused = json.loads(rows[1]["result_canonical"])
    assert refused["success"] is False and refused["validation"]["errors"][0]["error_code"] == "plugin_not_installed"
    assert json.loads(rows[2]["result_canonical"])["error_class"] == error_type.__name__


class TestToolDefinitions:
    def test_all_have_json_schema(self) -> None:
        for defn in get_tool_definitions():
            assert "name" in defn
            assert "description" in defn
            assert "parameters" in defn

    def test_array_schemas_declare_items(self) -> None:
        """Provider contract: every JSON-Schema array must declare items.

        OpenAI rejects tool schemas with bare ``{"type": "array"}`` at
        request validation time. This walks the full composer tool surface so
        one malformed nested schema cannot make every new session fail before
        the model sees the prompt.
        """
        for defn in get_tool_definitions():
            self._assert_arrays_have_items(defn["parameters"], defn["name"], ("parameters",))

    def test_on_validation_failure_has_no_enum_constraint(self) -> None:
        """Regression: on_validation_failure must accept any sink name, not just enum values.

        The runtime accepts 'discard' or any valid sink name for source
        validation failure routing.  A hard-coded enum blocks LLMs from
        building source-level failsink pipelines.
        """
        for defn in get_tool_definitions():
            self._assert_no_enum_on_validation_failure(defn.get("parameters", {}), defn["name"])

    def test_on_validation_failure_descriptions_match_runtime_contract(self) -> None:
        """Tool docs must not advertise a non-existent built-in quarantine sink."""
        descriptions: list[tuple[str, str]] = []
        for defn in get_tool_definitions():
            self._collect_on_validation_failure_descriptions(defn.get("parameters", {}), defn["name"], descriptions)

        assert descriptions, "expected at least one on_validation_failure schema description"
        for tool_name, description in descriptions:
            lowered = description.lower()
            assert "built-in quarantine" not in lowered, tool_name
            assert "discard" in description, tool_name
            assert "explicit non-empty" in lowered, tool_name
            assert "null/empty is invalid" in lowered, tool_name
            assert "Any other value, including 'quarantine', must match a configured output/sink name." in description, tool_name

    @pytest.mark.parametrize(
        "tool_name",
        ["list_blobs", "list_composer_blobs", "get_blob_metadata", "inspect_source"],
    )
    def test_blob_discovery_schemas_forbid_extra_arguments(self, tool_name: str) -> None:
        """Blob discovery tools persist arguments; their LLM-facing schemas must reject extra keys."""
        definition = next(defn for defn in get_tool_definitions() if defn["name"] == tool_name)

        assert definition["parameters"]["additionalProperties"] is False

    def test_upsert_node_trigger_schema_documents_end_of_source_only_shape(self) -> None:
        """Aggregation trigger schema must expose the end-of-source-only shape."""
        upsert_node = next(defn for defn in get_tool_definitions() if defn["name"] == "upsert_node")
        trigger_schema = upsert_node["parameters"]["properties"]["trigger"]

        assert "null" in trigger_schema["type"]
        assert trigger_schema["additionalProperties"] is False
        assert set(trigger_schema["properties"]) == {"count", "timeout_seconds", "condition"}
        assert "end-of-source-only" in trigger_schema["description"]
        assert "do not use end_of_source" in trigger_schema["properties"]["condition"]["description"]

    def test_upsert_node_expected_output_count_warns_about_group_by(self) -> None:
        """Aggregation schema must not steer grouped rollups toward fixed cardinality."""
        upsert_node = next(defn for defn in get_tool_definitions() if defn["name"] == "upsert_node")
        expected_schema = upsert_node["parameters"]["properties"]["expected_output_count"]

        assert "omit when output count depends on group_by" in expected_schema["description"]

    def test_upsert_edge_schema_includes_llm_failure_sink_routing_example(self) -> None:
        """The tool surface must show how to patch LLM failure routing."""
        upsert_edge = next(defn for defn in get_tool_definitions() if defn["name"] == "upsert_edge")
        examples = upsert_edge["parameters"]["examples"]

        assert {
            "id": "e_judge_layers_error",
            "from_node": "judge_layers",
            "to_node": "llm_failures",
            "edge_type": "on_error",
            "label": "LLM failures",
        } in examples

    def test_get_pipeline_state_component_schema_documents_full_state_aliases(self) -> None:
        """Tool schema must expose full-state spellings instead of relying only on omission."""
        get_pipeline_state = next(defn for defn in get_tool_definitions() if defn["name"] == "get_pipeline_state")
        component_schema = get_pipeline_state["parameters"]["properties"]["component"]

        assert "Accepted full-state aliases" in component_schema["description"]
        assert "full" in component_schema["description"]
        assert "all" in component_schema["description"]
        assert "pipeline" in component_schema["description"]
        assert "empty string" in component_schema["description"]

    def _assert_no_enum_on_validation_failure(self, schema: object, tool_name: str) -> None:
        """Recursively walk a JSON schema and assert no on_validation_failure has enum."""
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key == "on_validation_failure" and isinstance(value, dict):
                    assert "enum" not in value, (
                        f"Tool {tool_name!r} constrains on_validation_failure to enum {value.get('enum')} — runtime accepts any sink name"
                    )
                elif isinstance(value, (dict, list)):
                    self._assert_no_enum_on_validation_failure(value, tool_name)
        elif isinstance(schema, list):
            for item in schema:
                self._assert_no_enum_on_validation_failure(item, tool_name)

    def _collect_on_validation_failure_descriptions(
        self,
        schema: object,
        tool_name: str,
        descriptions: list[tuple[str, str]],
    ) -> None:
        """Collect on_validation_failure descriptions from nested tool schemas."""
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key == "on_validation_failure" and isinstance(value, dict):
                    description = value.get("description")
                    assert isinstance(description, str), f"Tool {tool_name!r} on_validation_failure lacks description"
                    descriptions.append((tool_name, description))
                elif isinstance(value, (dict, list)):
                    self._collect_on_validation_failure_descriptions(value, tool_name, descriptions)
        elif isinstance(schema, list):
            for item in schema:
                self._collect_on_validation_failure_descriptions(item, tool_name, descriptions)

    def _assert_arrays_have_items(self, schema: object, tool_name: str, path: tuple[str, ...]) -> None:
        """Recursively walk a JSON schema and assert all arrays declare items."""
        if isinstance(schema, dict):
            schema_type = schema.get("type")
            has_array_type = schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type)
            assert not has_array_type or "items" in schema, f"Tool {tool_name!r} has array schema without items at {'.'.join(path)}"
            for key, value in schema.items():
                if isinstance(value, (dict, list)):
                    self._assert_arrays_have_items(value, tool_name, (*path, str(key)))
        elif isinstance(schema, list):
            for index, item in enumerate(schema):
                self._assert_arrays_have_items(item, tool_name, (*path, f"[{index}]"))


class TestToolRegistry:
    """Tests for the tool registry pattern — two dicts + cacheable frozenset."""

    def test_discovery_tools_membership(self) -> None:
        """``_DISCOVERY_TOOLS`` keys equal the DISCOVERY-kind declaration name set.

        Step 6 falsification recovery (elspeth-6c9972ccbf, surface 1 of 2):
        the literal name enumeration previously here was a per-tool growth
        surface — every new discovery tool required updating this test in
        addition to its declaration. The literal was vestigial: it had no
        docstring framing it as a deliberate design-review gate and only
        duplicated the derivation pipeline that ``_DISCOVERY_TOOLS`` already
        runs at import time in ``_registry.py``.

        Retargeted at ``derive_name_set_for(_REGISTERED_TOOLS,
        ToolKind.DISCOVERY)`` so the assertion sources truth from the
        declaration tuple. The test still smoke-checks that
        ``_dispatch.py``'s consumed handler-map agrees with ``_registry.py``'s
        derivation, but no longer re-enumerates the names. The
        design-review gate for stateful discovery tools (the three
        ``diff_pipeline`` / ``get_pipeline_state`` / ``preview_pipeline``
        names) lives unchanged in
        ``test_cacheable_discovery_is_opt_in_with_named_mutable_complement``
        below, which retains its literal enumeration deliberately.
        """
        from elspeth.web.composer.tools import _DISCOVERY_TOOLS
        from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS
        from elspeth.web.composer.tools.declarations import (
            ToolKind,
            derive_name_set_for,
        )

        expected = derive_name_set_for(_REGISTERED_TOOLS, ToolKind.DISCOVERY)
        assert set(_DISCOVERY_TOOLS.keys()) == expected

    def test_mutation_tools_membership(self) -> None:
        """``_MUTATION_TOOLS`` keys equal the MUTATION-kind declaration name set.

        See ``test_discovery_tools_membership`` for the rationale behind
        retargeting away from a literal enumeration. The plain-mutation kind
        has no design-review gate — every new mutation tool is just another
        declaration, and the literal here served only as accidental
        documentation that grew with every addition.
        """
        from elspeth.web.composer.tools import _MUTATION_TOOLS
        from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS
        from elspeth.web.composer.tools.declarations import (
            ToolKind,
            derive_name_set_for,
        )

        expected = derive_name_set_for(_REGISTERED_TOOLS, ToolKind.MUTATION)
        assert set(_MUTATION_TOOLS.keys()) == expected

    def test_no_overlap_between_registries(self) -> None:
        from elspeth.web.composer.tools import _DISCOVERY_TOOLS, _MUTATION_TOOLS

        overlap = set(_DISCOVERY_TOOLS.keys()) & set(_MUTATION_TOOLS.keys())
        assert overlap == set(), f"Registry overlap: {overlap}"

    def test_cacheable_discovery_is_opt_in_with_named_mutable_complement(self) -> None:
        """Pin the *property* of the opt-in design, not the *result* of the
        subtraction the production code abandoned.

        ``_SESSION_MUTABLE_DISCOVERY_TOOL_NAMES`` is derived in
        ``_registry.py`` as ``_DISCOVERY - _CACHEABLE``. That makes it
        disjoint from the cacheable set for EVERY possible declaration
        content, so "the two sets are disjoint" is not an invariant, it is
        set algebra: an assertion over it (this test used to carry one, and
        ``_registry.py`` used to raise on it at import) can never fail and
        polices nothing (elspeth-235861ee32). Both were deleted rather than
        reworded.

        What this test DOES pin, and what would actually go red:

        1. The forbidden set exists as named data, not a comment.
        2. Its contents name exactly the four stateful discovery tools
           (``diff_pipeline``, ``get_complete_pipeline_value``,
           ``get_pipeline_state``, ``preview_pipeline``).
           This is the load-bearing line: a new DISCOVERY-kind declaration
           that omits ``cacheable=True`` lands in the complement and fails
           here, forcing a deliberate classification instead of a silent
           default; a declaration that sets ``cacheable=True`` on one of the
           four named tools removes it from the complement and fails here
           too.

        Neither import-time guard reaches this set.
        ``ToolDeclaration.__post_init__`` constrains only the kind axis (no
        non-DISCOVERY tool of any kind may be cacheable); every member here
        is DISCOVERY kind, so it accepts ``cacheable=True`` on
        ``get_pipeline_state`` without complaint. The ``_CACHEABLE <=
        _DISCOVERY`` subset check in ``_registry.py`` (pinned by
        ``test_cacheable_is_subset_of_discovery``) bounds the cacheable set
        from the other side. Item 2 above, together with the ``not in
        cacheable`` assertions in
        ``test_tool_declarations.py::test_cacheable_subset_is_correct``, is
        the ONLY thing keeping the four stateful tools out of the cache.

        Replaces the prior subtraction-shape assertion which mirrored
        the opt-OUT pattern the production code deliberately moved away
        from in commit e34f53c30.
        """
        from elspeth.web.composer.tools._registry import (
            _SESSION_MUTABLE_DISCOVERY_TOOL_NAMES,
        )

        # 1. the forbidden set exists as named data.
        assert isinstance(_SESSION_MUTABLE_DISCOVERY_TOOL_NAMES, frozenset)

        # 2. the forbidden set names exactly the four stateful tools. A
        # fifth, or a reclassification of one of these, must land here.
        assert (
            frozenset({"diff_pipeline", "get_complete_pipeline_value", "get_pipeline_state", "preview_pipeline"})
            == _SESSION_MUTABLE_DISCOVERY_TOOL_NAMES
        )

    def test_cacheable_is_subset_of_discovery(self) -> None:
        from elspeth.web.composer.tools import (
            _CACHEABLE_DISCOVERY_TOOL_NAMES,
            _DISCOVERY_TOOLS,
        )

        assert set(_DISCOVERY_TOOLS.keys()) >= _CACHEABLE_DISCOVERY_TOOL_NAMES

    def test_is_discovery_tool(self) -> None:
        from elspeth.web.composer.tools import is_discovery_tool

        assert is_discovery_tool("list_sources") is True
        assert is_discovery_tool("get_expression_grammar") is True
        assert is_discovery_tool("set_source") is False
        assert is_discovery_tool("nonexistent") is False

    def test_is_cacheable_discovery_tool(self) -> None:
        from elspeth.web.composer.tools import is_cacheable_discovery_tool

        assert is_cacheable_discovery_tool("list_sources") is True
        assert is_cacheable_discovery_tool("get_plugin_schema") is True
        assert is_cacheable_discovery_tool("set_source") is False

    def test_registry_dispatch_matches_original_behaviour(self) -> None:
        """Every tool in the registries dispatches correctly via execute_tool."""
        state = _empty_state()
        catalog = _mock_catalog()

        # All discovery tools should succeed
        for tool_name in [
            "list_sources",
            "list_transforms",
            "list_sinks",
            "get_expression_grammar",
        ]:
            result = execute_tool(tool_name, {}, state, catalog)
            assert result.success is True, f"{tool_name} failed"

        # get_plugin_schema needs arguments
        result = execute_tool(
            "get_plugin_schema",
            {"plugin_type": "source", "name": "csv"},
            state,
            catalog,
        )
        assert result.success is True

        # Mutation tools that work on empty state
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert result.success is True

        result = execute_tool(
            "set_metadata",
            {"patch": {"name": "Test"}},
            state,
            catalog,
        )
        assert result.success is True

        # Unknown tool returns failure
        result = execute_tool("nonexistent", {}, state, catalog)
        assert result.success is False

    def test_module_level_assertion_no_overlap(self) -> None:
        """Importing the module should not raise — the overlap assertion passes."""
        import importlib

        import elspeth.web.composer.tools as mod

        importlib.reload(mod)  # Force re-evaluation of module-level assertion
