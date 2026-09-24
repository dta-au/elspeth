"""Strict parameterless discovery repairs stay outside information accounting."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.tools._common import ToolContext
from tests.unit.web.composer.test_pipeline_planner import _budget, _invalid_pipeline, _pipeline, _plan, _response, _ScriptedCompletion


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_arguments", [{}, {"_elspeth_no_arguments": False}, {"untrusted_marker_name": "replay_value_canary"}])
async def test_rejected_marker_then_corrected_same_discovery_executes_once(
    tmp_path: Path, tool_context: ToolContext, bad_arguments: dict[str, object]
) -> None:
    completion = _ScriptedCompletion(
        _response(("list_sources", bad_arguments), ("get_expression_grammar", {"_elspeth_no_arguments": True})),
        _response(("list_sources", {"_elspeth_no_arguments": True})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    with patch.object(
        pipeline_planner, "execute_discovery_tool_with_context", wraps=pipeline_planner.execute_discovery_tool_with_context
    ) as execute:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            model_overrides={"tool_contract_dialect": ToolContractDialect.OPENAI_STRICT},
        )
    assert [(call.args[0], call.args[1]) for call in execute.call_args_list] == [("get_expression_grammar", {}), ("list_sources", {})]
    first_tool_messages = [message for message in completion.requests[1]["messages"] if message["role"] == "tool"]
    error = json.loads(first_tool_messages[0]["content"])
    assert error["success"] is False
    assert '{"_elspeth_no_arguments": true}' in error["message"]
    assert "untrusted_marker_name" not in first_tool_messages[0]["content"]
    second_tool_messages = [message for message in completion.requests[2]["messages"] if message["role"] == "tool"]
    assert json.loads(second_tool_messages[-1]["content"])["success"] is True
    rejected, sibling, corrected = recorder.invocations
    assert rejected.status.value == "arg_error"
    assert rejected.wire_conformant is False
    assert sibling.status.value == corrected.status.value == "success"
    assert "untrusted_marker_name" not in repr(recorder.invocations)
    for request in completion.requests[1:]:
        rendered = json.dumps(request)
        assert "untrusted_marker_name" not in rendered
        assert "replay_value_canary" not in rendered
    replayed = next(message for message in completion.requests[1]["messages"] if message["role"] == "assistant")
    assert json.loads(replayed["tool_calls"][0]["function"]["arguments"]) == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "ToolArgumentError",
    }
    assert replayed["tool_calls"][1]["function"]["arguments"] == json.dumps({"_elspeth_no_arguments": True})


@pytest.mark.asyncio
async def test_repeated_invalid_marker_stops_at_discovery_budget_without_dispatch(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(*[_response(("list_sources", {})) for _ in range(3)])
    recorder = BufferingRecorder()
    with (
        patch.object(
            pipeline_planner, "execute_discovery_tool_with_context", wraps=pipeline_planner.execute_discovery_tool_with_context
        ) as execute,
        pytest.raises(PipelinePlannerError) as caught,
    ):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            model_overrides={
                "tool_contract_dialect": ToolContractDialect.OPENAI_STRICT,
                "max_discovery_turns": 2,
                "escape_hatch_model": None,
            },
        )
    assert caught.value.code == "DISCOVERY_EXHAUSTED"
    assert len(completion.requests) == 3
    execute.assert_not_called()
    assert len(recorder.invocations) == 2
    assert all(invocation.status.value == "arg_error" for invocation in recorder.invocations)


@pytest.mark.asyncio
async def test_three_argument_rejections_hint_before_corrected_retry(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        *[_response(("list_sources", {"untrusted_marker_name": "replay_value_canary"})) for _ in range(3)],
        _response(("list_sources", {"_elspeth_no_arguments": True})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        budget=_budget(max_total_provider_calls=12),
        model_overrides={"tool_contract_dialect": ToolContractDialect.OPENAI_STRICT, "max_discovery_turns": 10},
    )

    hints = [
        message["content"]
        for message in completion.requests[3]["messages"]
        if message["role"] == "user" and "[ELSPETH-SYSTEM-HINT]" in message["content"]
    ]
    assert len(hints) == 1
    assert "last 3 calls" in hints[0]
    assert "list_sources" in hints[0]
    for request in completion.requests[1:]:
        rendered = json.dumps(request)
        assert "untrusted_marker_name" not in rendered
        assert "replay_value_canary" not in rendered
    assert [invocation.status.value for invocation in recorder.invocations[:4]] == ["arg_error", "arg_error", "arg_error", "success"]


@pytest.mark.asyncio
async def test_six_identical_argument_rejections_stop_before_global_budget(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        *[_response(("list_sources", {})) for _ in range(6)],
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            budget=_budget(max_total_provider_calls=12),
            model_overrides={
                "tool_contract_dialect": ToolContractDialect.OPENAI_STRICT,
                "max_discovery_turns": 10,
                "escape_hatch_model": None,
            },
        )

    assert caught.value.code == "DISCOVERY_CYCLE"
    assert len(completion.requests) == len(recorder.invocations) == 6
    assert all(invocation.status.value == "arg_error" for invocation in recorder.invocations)


@pytest.mark.asyncio
async def test_successful_sibling_breaks_consecutive_argument_rejections(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("list_sources", {})),
        _response(("list_sources", {}), ("get_expression_grammar", {"_elspeth_no_arguments": True})),
        *[_response(("list_sources", {})) for _ in range(3)],
        _response(("list_sources", {"_elspeth_no_arguments": True})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        budget=_budget(max_total_provider_calls=12),
        model_overrides={"tool_contract_dialect": ToolContractDialect.OPENAI_STRICT, "max_discovery_turns": 10},
    )

    assert [invocation.status.value for invocation in recorder.invocations[:8]] == [
        "arg_error",
        "arg_error",
        "arg_error",
        "success",
        "arg_error",
        "arg_error",
        "arg_error",
        "success",
    ]
    # Results are processed in declared call order. The successful sibling
    # breaks the first run before the next request, so its hint is unnecessary.
    assert not any(
        message["role"] == "user" and "[ELSPETH-SYSTEM-HINT]" in message["content"] for message in completion.requests[3]["messages"]
    )


@pytest.mark.asyncio
async def test_different_argument_rejections_do_not_suggest_unavailable_mutation_tools(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("list_sources", {"_elspeth_no_arguments": False})),
        _response(("list_sources", {"_elspeth_no_arguments": "true"})),
        _response(("list_sources", {"_elspeth_no_arguments": True})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        budget=_budget(max_total_provider_calls=12),
        model_overrides={"tool_contract_dialect": ToolContractDialect.OPENAI_STRICT, "max_discovery_turns": 10},
    )

    assert not any(
        message["role"] == "user" and "[ELSPETH-SYSTEM-HINT]" in message["content"] for message in completion.requests[3]["messages"]
    )


@pytest.mark.asyncio
async def test_rejected_candidate_breaks_consecutive_argument_rejections(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        *[_response(("list_sources", {})) for _ in range(3)],
        _response(("emit_pipeline_proposal", {"pipeline": _invalid_pipeline(tmp_path)})),
        *[_response(("list_sources", {})) for _ in range(3)],
        _response(("list_sources", {"_elspeth_no_arguments": True})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        budget=_budget(max_total_provider_calls=12),
        model_overrides={"tool_contract_dialect": ToolContractDialect.OPENAI_STRICT, "max_discovery_turns": 10},
    )

    assert len(completion.requests) == 9
    assert [invocation.status.value for invocation in recorder.invocations if invocation.tool_name == "list_sources"] == [
        "arg_error",
        "arg_error",
        "arg_error",
        "arg_error",
        "arg_error",
        "arg_error",
        "success",
    ]
