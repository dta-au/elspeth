"""Actual planner delivery controls for emergency information-boundary repair."""

import json
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from elspeth.core.canonical import canonical_json
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.anti_anchor import build_drift_hint
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY
from elspeth.web.composer.request_history import (
    authoritative_conversation_context as _freeform_planner_conversation_context,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_pipeline_planner import _budget, _invalid_pipeline, _pipeline, _plan, _response, _ScriptedCompletion


@pytest.mark.parametrize("message", ["Build the requested pipeline.", "Build a pipeline that reads CSV and writes JSON."])
def test_every_authenticated_amendment_reaches_planner(message: str) -> None:
    messages = [
        {"role": "user", "content": f"mandatory amendment {index}", COMPOSER_HISTORY_USER_AUTHORED_KEY: True} for index in range(12)
    ]
    context = _freeform_planner_conversation_context(message, messages)
    assert context is not None
    assert [request.content for request in context.prior_user_requests] == [row["content"] for row in messages]
    assert context.additional_prior_user_requests_omitted == 0


@pytest.mark.asyncio
async def test_successful_reread_delivers_actual_result(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("list_sources", {})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)
    results = [json.loads(message["content"]) for message in completion.requests[-1]["messages"] if message["role"] == "tool"]
    assert len(results) == 2
    assert all(result["success"] and result["data"] for result in results)
    assert [invocation.tool_name for invocation in recorder.invocations] == ["list_sources", "list_sources"]
    assert "list_sources" in {tool["function"]["name"] for tool in completion.requests[-1]["tools"]}


@pytest.mark.asyncio
async def test_all_amendments_reach_the_actual_prepared_planner_request(tmp_path: Path, tool_context: ToolContext) -> None:
    intent = "Build a pipeline that reads CSV and writes JSON."
    requirements = [f"mandatory amendment {index}" for index in range(12)]
    context = _freeform_planner_conversation_context(
        intent,
        [{"role": "user", "content": requirement, COMPOSER_HISTORY_USER_AUTHORED_KEY: True} for requirement in requirements],
    )
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, intent=intent, conversation_context=context)
    request = json.loads(completion.requests[0]["messages"][-1]["content"])
    assert request["intent"] == intent
    assert [row["content"] for row in request["conversation_context"]["prior_user_requests"]] == requirements
    assert request["conversation_context"]["additional_prior_user_requests_omitted"] == 0


@pytest.mark.asyncio
async def test_senior_hatch_reads_then_proposes_with_same_provider_accounting(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("list_sinks", {})),
        _response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        model_overrides={"max_discovery_turns": 1, "escape_hatch_model": "openrouter/senior", "escape_hatch_provider": "openrouter"},
    )
    assert [call.model_requested for call in recorder.llm_calls] == ["anthropic/claude-planner"] * 2 + ["openrouter/senior"] * 2
    assert [invocation.tool_name for invocation in recorder.invocations] == ["list_sources", "get_plugin_schema"]
    senior_results = [json.loads(message["content"]) for message in completion.requests[-1]["messages"] if message["role"] == "tool"]
    assert senior_results[-1]["success"] is True
    assert "json_schema" in senior_results[-1]["data"]


def test_changed_arguments_hint_does_not_invent_measured_nonprogress() -> None:
    hint = build_drift_hint(deque([("set_pipeline", "a"), ("set_pipeline", "b"), ("set_pipeline", "c")], maxlen=5))
    assert "different arguments" in hint
    assert "do not repeat" in hint.lower()
    assert "not compared" in hint
    assert "drift without convergence" not in hint
    assert "choose a new repair strategy" not in hint


@pytest.mark.parametrize("compact_supported", [False, True])
@pytest.mark.asyncio
async def test_schema_fallback_delivers_full_admitted_contract_and_can_be_reread(
    tmp_path: Path, tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch, compact_supported: bool
) -> None:
    original = pipeline_planner.execute_discovery_tool_with_context
    sentinel = "schema-tail-remains-available"

    def schema_with_large_value(name: str, arguments: Mapping[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
        result = original(name, arguments, state, context)
        if name != "get_plugin_schema":
            return result
        return replace(
            result,
            data=PluginSchemaInfo(
                name="csv",
                plugin_type="source",
                description="Admitted full schema fallback.",
                json_schema={"type": "object", "description": "x" * (49 * 1024) + sentinel}
                if not compact_supported
                else {"type": "object"},
                knob_schema={"fields": []},
            ),
        )

    monkeypatch.setattr(pipeline_planner, "execute_discovery_tool_with_context", schema_with_large_value)
    if compact_supported:
        monkeypatch.setattr(pipeline_planner, "_SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES", 1)
    completion = _ScriptedCompletion(
        _response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})),
        _response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    loaded: list[tuple[str, str]] = []
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        mark_schema_loaded=lambda kind, name: loaded.append((kind, name)),
        information_aware=True,
    )
    payloads = [json.loads(message["content"]) for message in completion.requests[-1]["messages"] if message["role"] == "tool"]
    assert len(payloads) == 2
    assert all(payload["success"] and payload["data"]["name"] == "csv" for payload in payloads)
    assert all(payload["data"]["json_schema"]["type"] == "object" for payload in payloads)
    if not compact_supported:
        assert all(payload["data"]["json_schema"]["description"].endswith(sentinel) for payload in payloads)
    assert loaded == [("source", "csv"), ("source", "csv")]


@pytest.mark.asyncio
async def test_senior_discovery_does_not_extend_total_provider_allowance(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("list_sinks", {})),
        _response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})),
    )
    recorder = BufferingRecorder()
    with pytest.raises(PipelinePlannerError) as failure:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            budget=replace(_budget(), max_total_provider_calls=3),
            model_overrides={"max_discovery_turns": 1, "escape_hatch_model": "openrouter/senior", "escape_hatch_provider": "openrouter"},
        )
    assert failure.value.code == "DISCOVERY_EXHAUSTED"
    assert len(completion.requests) == len(recorder.llm_calls) == 3


@pytest.mark.asyncio
async def test_oversized_authored_history_refuses_before_any_provider_call(tmp_path: Path, tool_context: ToolContext) -> None:
    context = _freeform_planner_conversation_context(
        "Build a CSV pipeline.",
        [{"role": "user", "content": "A" * 1_100_000, COMPOSER_HISTORY_USER_AUTHORED_KEY: True}],
    )
    completion = _ScriptedCompletion()
    with pytest.raises(PipelinePlannerError) as failure:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, conversation_context=context)
    assert failure.value.code == "REQUEST_BYTES_EXHAUSTED"
    assert completion.requests == []


@pytest.mark.asyncio
async def test_schema_not_marked_loaded_when_whole_request_refuses_delivery(
    tmp_path: Path, tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=probe)
    initial_bytes = len(canonical_json({"messages": probe.requests[0]["messages"], "tools": probe.requests[0]["tools"]}).encode("utf-8"))
    original = pipeline_planner.execute_discovery_tool_with_context

    def large_schema(name: str, arguments: Mapping[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
        result = original(name, arguments, state, context)
        return replace(
            result,
            data=PluginSchemaInfo(
                name="csv",
                plugin_type="source",
                description="Complete admitted contract exceeding remaining request allowance.",
                json_schema={"type": "object", "description": "x" * (49 * 1024)},
                knob_schema={"fields": []},
            ),
        )

    monkeypatch.setattr(pipeline_planner, "execute_discovery_tool_with_context", large_schema)
    loaded: list[tuple[str, str]] = []
    completion = _ScriptedCompletion(_response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})))
    with pytest.raises(PipelinePlannerError) as failure:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            budget=replace(_budget(), max_request_bytes=initial_bytes + 10_000),
            mark_schema_loaded=lambda kind, name: loaded.append((kind, name)),
        )
    assert failure.value.code == "REQUEST_BYTES_EXHAUSTED"
    assert len(completion.requests) == 1
    assert loaded == []


@pytest.mark.asyncio
async def test_candidate_rejection_separates_discovery_argument_failure_windows(tmp_path: Path, tool_context: ToolContext) -> None:
    invalid_read = ("get_plugin_schema", {"plugin_type": "node", "name": "csv"})
    completion = _ScriptedCompletion(
        *[_response(invalid_read) for _ in range(3)],
        _response(("emit_pipeline_proposal", {"pipeline": _invalid_pipeline(tmp_path)})),
        *[_response(invalid_read) for _ in range(3)],
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        budget=replace(_budget(), max_total_provider_calls=10),
        model_overrides={"max_discovery_turns": 10},
    )
    assert len(completion.requests) == 8


@pytest.mark.asyncio
async def test_full_schema_fallback_does_not_bypass_credential_refusal(
    tmp_path: Path, tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = pipeline_planner.execute_discovery_tool_with_context
    credential = "ghp_" + "a" * 36

    def schema_with_credential(name: str, arguments: Mapping[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
        result = original(name, arguments, state, context)
        return replace(
            result,
            data=PluginSchemaInfo(
                name="csv",
                plugin_type="source",
                description="Schema from the admitted producer.",
                json_schema={"type": "object", "description": "x" * (49 * 1024) + credential},
                knob_schema={"fields": []},
            ),
        )

    monkeypatch.setattr(pipeline_planner, "execute_discovery_tool_with_context", schema_with_credential)
    loaded: list[tuple[str, str]] = []
    completion = _ScriptedCompletion(_response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})))
    with pytest.raises(CredentialMaterialRefused):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            mark_schema_loaded=lambda kind, name: loaded.append((kind, name)),
        )
    assert len(completion.requests) == 1
    assert credential not in canonical_json(completion.requests)
    assert loaded == []
