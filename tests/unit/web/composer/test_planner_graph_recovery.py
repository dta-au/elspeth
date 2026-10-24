"""Scripted-provider coverage of terminal recovery across planner stages."""

from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, PlannerHandoff
from elspeth.web.composer.tools._common import ToolContext
from tests.unit.web.composer.test_pipeline_planner import _invalid_pipeline, _plan, _response, _ScriptedCompletion

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("dialect", [ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT])
@pytest.mark.parametrize("action", ["park", "reset_to_turn_start", "reset_to_safe_checkpoint"])
async def test_first_call_handoff_does_not_need_discovery(
    tmp_path: Path, tool_context: ToolContext, dialect: ToolContractDialect, action: str
) -> None:
    completion = _ScriptedCompletion(_response(("handoff_to_user", {"action": action, "message": "Let's clarify the output."})))
    with pytest.raises(PlannerHandoff) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            information_aware=True,
            model_overrides={"tool_contract_dialect": dialect},
        )
    assert caught.value.intent.action == action
    assert caught.value.intent.message == "Let's clarify the output."
    assert len(completion.requests) == 1


async def test_explicit_restore_returns_exact_park_identity(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(_response(("restore_parked_graph", {"park_id": "park-one", "message": "Restore that draft."})))
    with pytest.raises(PlannerHandoff) as caught:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion)
    assert caught.value.intent.park_id == "park-one"
    assert caught.value.intent.action == "restore_parked_graph"
    assert len(completion.requests) == 1


async def test_hatch_keeps_recovery_controls_and_stops(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("emit_pipeline_proposal", {"pipeline": _invalid_pipeline(tmp_path)})),
        _response(("handoff_to_user", {"action": "reset_to_safe_checkpoint", "message": "Let's reconsider this draft."})),
    )
    with pytest.raises(PlannerHandoff):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            repair_budget=0,
            model_overrides={"escape_hatch_model": "test-advisor", "escape_hatch_provider": "test"},
        )
    assert len(completion.requests) == 2
    assert {tool["function"]["name"] for tool in completion.requests[-1]["tools"]} == {
        *pipeline_planner.PLANNER_DISCOVERY_TOOL_NAMES,
        "handoff_to_user",
        "restore_parked_graph",
        "emit_pipeline_proposal",
    }


async def test_mixed_recovery_batch_is_rejected_before_dispatch(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(
            ("list_sources", {}),
            ("handoff_to_user", {"action": "park", "message": "Let's discuss."}),
        )
    )
    with (
        patch.object(pipeline_planner, "execute_discovery_tool_with_context", autospec=True) as dispatch,
        pytest.raises(PipelinePlannerError) as caught,
    ):
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion)
    assert caught.value.code == "MALFORMED_RESPONSE"
    dispatch.assert_not_called()
    assert len(completion.requests) == 1


async def test_handoff_does_not_construct_or_capture_a_candidate(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(
        _response(("handoff_to_user", {"action": "reset_to_safe_checkpoint", "message": "Recover and discuss."}))
    )
    with (
        patch.object(pipeline_planner, "build_set_pipeline_candidate", autospec=True, side_effect=AssertionError("candidate touched")),
        pytest.raises(PlannerHandoff),
    ):
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion)
    assert len(completion.requests) == 1
