"""Scripted-provider coverage of terminal recovery across planner stages."""

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.audit import BufferingRecorder, PrivatePlannerCandidate
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, PlannerHandoff
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import _composer_service_with_session, _empty_state, _make_settings
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


async def test_application_handoff_defers_audit_and_omits_optional_candidate_capture() -> None:
    catalog = create_catalog_service()
    service, session_id = _composer_service_with_session(catalog, _make_settings())
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    recorder = BufferingRecorder()
    intent = GraphRecoveryIntent("reset_to_safe_checkpoint", "Let's recover and discuss.", None, "recovery-call")

    def choose_recovery(**_kwargs: object) -> None:
        recorder.record_withheld_reply("planner_prose_unadmitted", "Earlier provider explanation.")
        recorder.record_private_candidate(
            PrivatePlannerCandidate("candidate-one", "candidate-call", "emit_pipeline_proposal", "{}", "{}", "{}")
        )
        raise PlannerHandoff(intent)

    with (
        fenced_operation_context(service._session_engine, session_id) as context,
        patch("elspeth.web.composer.planning_application.plan_pipeline", autospec=True, side_effect=choose_recovery),
        patch.object(service._planning_application, "_persist_pipeline_planner_audit", autospec=True) as persist,
        patch("elspeth.web.composer.diagnostic_evidence.build_private_tool_evidence", autospec=True) as capture,
    ):
        result = await service._planning_application._plan_and_stage_empty_pipeline(
            message="Recover and talk to me.",
            messages=[],
            state=_empty_state(),
            session_id=session_id,
            current_state_id=None,
            user_id="test-user",
            session_operation_context=context,
            progress=None,
            user_message_id=str(uuid4()),
            recorder=recorder,
            plugin_snapshot=snapshot,
            policy_catalog=policy,
        )
    persist.assert_not_called()
    capture.assert_not_called()
    assert result.graph_recovery_intent == intent
    assert [draft.content for draft in result.recovery_audit_drafts] == ["Earlier provider explanation."]
    assert result.llm_calls == ()
    assert result.tool_invocations == ()
