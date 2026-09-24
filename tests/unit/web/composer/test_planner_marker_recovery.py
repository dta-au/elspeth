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
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_arguments", [{}, {"_elspeth_no_arguments": False}, {"untrusted_marker_name": 1}])
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
