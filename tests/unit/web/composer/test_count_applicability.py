"""Count applicability is semantic admission, with inspectable attempted arguments."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml
from pydantic import ValidationError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.enums import OutputMode
from elspeth.contracts.errors import PipelineLoweringError
from elspeth.core.config import AggregationSettings
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, dispatch_with_audit, finish_success
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, NodeSpec
from elspeth.web.composer.tools import ToolResult, build_set_pipeline_candidate, execute_tool, get_tool_definitions
from elspeth.web.composer.tools import sessions as sessions_tools
from elspeth.web.composer.tools import transforms as transforms_tools
from elspeth.web.composer.yaml_generator import generate_public_yaml, generate_yaml
from elspeth.web.composer.yaml_importer import RuntimeYamlImportError, composition_state_from_runtime_yaml
from elspeth.web.sessions.engine import create_session_engine
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _trained_context

CODE = "aggregation_expected_output_count_mode_invalid"


def test_both_authoring_schemas_teach_count_applicability() -> None:
    definitions = {definition["name"]: definition for definition in get_tool_definitions()}
    incremental = definitions["upsert_node"]["parameters"]["properties"]["expected_output_count"]
    full = definitions["set_pipeline"]["parameters"]["properties"]["nodes"]["items"]["properties"]["expected_output_count"]
    for field in (incremental, full):
        assert "output_mode='transform' (the default)" in field["description"]
        assert "omit for 'passthrough'" in field["description"]


def _node(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "batch",
        "node_type": "aggregation",
        "plugin": "batch_stats",
        "input": "rows",
        "on_success": "out",
        "on_error": "discard",
        "options": {},
        "output_mode": "passthrough",
        "expected_output_count": 1,
        **overrides,
    }


def _pipeline(nodes: list[dict[str, Any]], *, blob: bool = False) -> dict[str, Any]:
    source: dict[str, Any] = {
        "plugin": "csv",
        "on_success": "rows",
        "on_validation_failure": "discard",
        "options": {"path": "/tmp/count.csv", "schema": {"mode": "observed"}},
    }
    if blob:
        source["inline_blob"] = {"filename": "input.csv", "mime_type": "text/csv", "content": "PRIVATE_INLINE_SENTINEL"}
    return {"source": source, "nodes": nodes, "outputs": [], "edges": []}


@pytest.mark.parametrize("mode", list(OutputMode))
@pytest.mark.parametrize("count", [None, -1, 0, 1, 9])
def test_owned_rule_and_runtime_settings(mode: OutputMode, count: int | None) -> None:
    arguments = {
        "name": "batch",
        "plugin": "batch_stats",
        "input": "rows",
        "on_error": "discard",
        "output_mode": mode,
        "expected_output_count": count,
    }
    error = mode.expected_output_count_error(count)
    if mode is OutputMode.PASSTHROUGH and count is not None:
        assert error is not None
        with pytest.raises(ValidationError, match="expected_output_count requires"):
            AggregationSettings.model_validate(arguments)
    else:
        assert error is None
        assert AggregationSettings.model_validate(arguments).expected_output_count == count


def test_runtime_default_and_explicit_null_stay_distinct() -> None:
    arguments = {"name": "batch", "plugin": "batch_stats", "input": "rows", "on_error": "discard", "expected_output_count": 2}
    assert AggregationSettings.model_validate(arguments).output_mode is OutputMode.TRANSFORM
    with pytest.raises(ValidationError):
        AggregationSettings.model_validate({**arguments, "output_mode": None})


@pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"])
@pytest.mark.parametrize("count", [True, "1", 1.5, 1, None])
@pytest.mark.asyncio
async def test_audited_dispatch_distinguishes_structural_and_semantic_count_failure(tool_name: str, count: object) -> None:
    state = _empty_state()
    context = _trained_context()
    node = _node(expected_output_count=count, options={"schema": {"mode": "observed"}, "value_field": "amount"})
    arguments = node if tool_name == "upsert_node" else _pipeline([node])
    recorder = BufferingRecorder()

    async def dispatch() -> ToolResult:
        return execute_tool(tool_name, arguments, state, context.catalog, plugin_snapshot=context.plugin_snapshot)

    async def audited_dispatch() -> Any:
        return await dispatch_with_audit(
            recorder=recorder,
            audit=begin_dispatch("count-audit", tool_name, arguments, version_before=state.version, actor="test"),
            do_dispatch=dispatch,
            version_after_provider=lambda result: result.updated_state.version,
            arg_error_payload_factory=lambda error: {"error": str(error)},
        )

    structurally_invalid = count is not None and type(count) is not int
    if structurally_invalid:
        with pytest.raises(ToolArgumentError):
            await audited_dispatch()
    else:
        outcome = await audited_dispatch()
        assert outcome.result.success is (count is None)
        if count is not None:
            assert outcome.result.updated_state is state
            assert [error.error_code for error in outcome.result.validation.errors] == [CODE]
    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.status is (ComposerToolStatus.ARG_ERROR if structurally_invalid else ComposerToolStatus.SUCCESS)
    _, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    stored_arguments = json.loads(envelope["invocation"]["arguments_canonical"])
    if structurally_invalid:
        assert stored_arguments["_redaction_status"] == "invalid_tool_arguments"
        assert "output_mode" not in stored_arguments
        assert "nodes" not in stored_arguments
    elif count is not None:
        stored_node = stored_arguments if tool_name == "upsert_node" else stored_arguments["nodes"][0]
        assert stored_node["output_mode"] == "passthrough"
        assert stored_node["expected_output_count"] == 1
        assert json.loads(envelope["invocation"]["result_canonical"])["success"] is False


def test_intrinsic_failure_does_not_report_unchecked_source_or_plugin_defects() -> None:
    arguments = _pipeline([_node(plugin="missing_plugin")])
    arguments["source"]["plugin"] = "missing_source"
    arguments["source"]["options"] = {"path": "unvalidated-source", "api_key": "PRIVATE_UNCHECKED_SENTINEL"}
    candidate = build_set_pipeline_candidate(arguments, _empty_state(), _trained_context())
    assert not candidate.acceptable
    assert [error.error_code for error in candidate.result.validation.errors] == [CODE]
    assert "missing_plugin" not in json.dumps(candidate.result.to_dict())
    assert "missing_source" not in json.dumps(candidate.result.to_dict())
    assert "PRIVATE_UNCHECKED_SENTINEL" not in json.dumps(candidate.result.to_dict())


def test_incremental_rejects_before_options_and_keeps_audit_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _empty_state()
    context = _trained_context()
    prevalidate = create_autospec(transforms_tools._prevalidate_transform_for_context, side_effect=AssertionError("plugin work ran"))
    normalize = create_autospec(transforms_tools._options_with_default_llm_reviews, side_effect=AssertionError("option normalization ran"))
    monkeypatch.setattr(transforms_tools, "_prevalidate_transform_for_context", prevalidate)
    monkeypatch.setattr(transforms_tools, "_options_with_default_llm_reviews", normalize)
    arguments = _node(options={"api_key": "PRIVATE_OPTION_SENTINEL"})
    result = execute_tool("upsert_node", arguments, state, context.catalog, plugin_snapshot=context.plugin_snapshot)
    assert not result.success
    assert result.updated_state is state
    assert result.updated_state.version == state.version
    assert [error.error_code for error in result.validation.errors] == [CODE]
    prevalidate.assert_not_called()
    normalize.assert_not_called()
    invocation = finish_success(
        begin_dispatch("count", "upsert_node", arguments, version_before=state.version, actor="test"),
        result_payload=result.to_dict(),
        version_after=state.version,
    )
    _, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    admitted = json.loads(persisted["arguments_canonical"])
    assert admitted["output_mode"] == "passthrough"
    assert admitted["expected_output_count"] == 1
    assert "PRIVATE_OPTION_SENTINEL" not in json.dumps(envelope)


@pytest.mark.parametrize("source_kind", ["inline", "reference"])
def test_full_candidate_reports_ordered_bounded_defects_before_custody(
    monkeypatch: pytest.MonkeyPatch, source_kind: str, tmp_path: Path
) -> None:
    state = _empty_state()
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=create_session_engine("sqlite://"),
        user_message_id="count-message",
        user_message_content="PRIVATE_INLINE_SENTINEL",
    )
    prepare = create_autospec(sessions_tools._prepare_blob_create, side_effect=AssertionError("prepared blob"))
    resolve = create_autospec(sessions_tools._resolve_source_blob, side_effect=AssertionError("resolved blob"))
    persist = create_autospec(sessions_tools._persist_prepared_blob_create, side_effect=AssertionError("persisted blob"))
    plugin = create_autospec(sessions_tools._validate_plugin_name, side_effect=AssertionError("plugin work"))
    monkeypatch.setattr(sessions_tools, "_prepare_blob_create", prepare)
    monkeypatch.setattr(sessions_tools, "_resolve_source_blob", resolve)
    monkeypatch.setattr(sessions_tools, "_persist_prepared_blob_create", persist)
    monkeypatch.setattr(sessions_tools, "_validate_plugin_name", plugin)
    limit = sessions_tools._MAX_REPORTED_COMPONENT_REJECTIONS
    arguments = _pipeline([_node(id=f"batch{i}") for i in range(limit + 2)], blob=source_kind == "inline")
    if source_kind == "reference":
        arguments["source"]["blob_id"] = "00000000-0000-4000-8000-000000000001"
    candidate = build_set_pipeline_candidate(arguments, state, context)
    assert not candidate.acceptable
    assert candidate.result.updated_state is state
    assert candidate.result.updated_state.version == state.version
    errors = candidate.result.validation.errors
    assert [error.error_code for error in errors] == [CODE] * limit
    assert [error.message.split(":", 1)[0] for error in errors] == [f"Node 'batch{i}'" for i in range(limit)]
    assert candidate.result.to_dict()["data"]["components_withheld"] == 2
    for spy in (prepare, resolve, persist, plugin):
        spy.assert_not_called()
    invocation = finish_success(
        begin_dispatch("count", "set_pipeline", arguments, version_before=state.version, actor="test"),
        result_payload=candidate.result.to_dict(),
        version_after=state.version,
    )
    _, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    admitted = json.loads(envelope["invocation"]["arguments_canonical"])
    assert admitted["nodes"][0]["output_mode"] == "passthrough"
    assert admitted["nodes"][0]["expected_output_count"] == 1
    assert "PRIVATE_INLINE_SENTINEL" not in json.dumps(envelope)


def _historical_state(mode: str | None, count: int | None) -> CompositionState:
    node = NodeSpec.from_dict(_node(output_mode=mode, expected_output_count=count))
    return replace(_empty_state(), nodes=(node,))


@pytest.mark.parametrize("authored_mode", ["omitted", None, "transform"])
def test_incremental_default_mode_preserves_authored_presence(authored_mode: str | None) -> None:
    context = _trained_context()
    arguments = _node(output_mode=authored_mode, options={"schema": {"mode": "observed"}, "value_field": "amount"})
    if authored_mode == "omitted":
        del arguments["output_mode"]
    result = execute_tool("upsert_node", arguments, _empty_state(), context.catalog, plugin_snapshot=context.plugin_snapshot)
    assert result.success, result.to_dict()
    node = result.updated_state.nodes[0]
    assert node.output_mode == (None if authored_mode == "omitted" else authored_mode)
    assert node.expected_output_count == 1
    assert ("output_mode" in result.updated_state.to_dict()["nodes"][0]) == (authored_mode == "transform")


@pytest.mark.parametrize("authored_mode", ["omitted", None, "transform"])
def test_full_candidate_default_mode_preserves_authored_presence(authored_mode: str | None) -> None:
    node = _node(output_mode=authored_mode, options={"schema": {"mode": "observed"}, "value_field": "amount"})
    if authored_mode == "omitted":
        del node["output_mode"]
    arguments = _pipeline([node])
    arguments["outputs"] = [
        {
            "sink_name": "out",
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": "/tmp/count-out.json", "schema": {"mode": "observed"}},
        }
    ]
    candidate = build_set_pipeline_candidate(arguments, _empty_state(), _trained_context())
    assert candidate.acceptable, candidate.result.to_dict()
    actual = candidate.result.updated_state.nodes[0]
    assert actual.output_mode == (None if authored_mode == "omitted" else authored_mode)
    assert actual.expected_output_count == 1


def test_historical_state_is_readable_repairable_and_cannot_export() -> None:
    state = _historical_state("passthrough", 1)
    restored = CompositionState.from_dict(state.to_dict())
    assert restored.nodes[0].expected_output_count == 1
    assert CODE in {error.error_code for error in restored.validate().errors}
    for export in (generate_yaml, generate_public_yaml):
        with pytest.raises(PipelineLoweringError, match="expected_output_count requires"):
            export(restored)
    repaired = replace(restored, nodes=(replace(restored.nodes[0], expected_output_count=None),))
    assert CODE not in {error.error_code for error in repaired.validate().errors}
    context = _trained_context()
    result = execute_tool(
        "upsert_node",
        _node(output_mode="transform", options={"schema": {"mode": "observed"}, "value_field": "amount"}),
        restored,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
    )
    assert result.success, result.to_dict()
    assert result.updated_state.nodes[0].output_mode == "transform"
    assert CODE not in {error.error_code for error in result.validation.errors}


@pytest.mark.parametrize("mode", [None, "transform", "passthrough"])
def test_yaml_mode_count_applicability(mode: str | None) -> None:
    count = None if mode == "passthrough" else 2
    state = _historical_state(mode, count)
    document = yaml.safe_load(generate_yaml(state))
    aggregation = document["aggregations"][0]
    assert ("output_mode" in aggregation) == (mode is not None)
    imported = composition_state_from_runtime_yaml(yaml.safe_dump(document))
    assert imported.nodes[0].output_mode == mode
    assert imported.nodes[0].expected_output_count == count
    aggregation["output_mode"] = "passthrough"
    aggregation["expected_output_count"] = 1
    with pytest.raises(RuntimeYamlImportError, match="expected_output_count requires"):
        composition_state_from_runtime_yaml(yaml.safe_dump(document))
