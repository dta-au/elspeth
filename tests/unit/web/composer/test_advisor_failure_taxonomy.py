"""Advisor feedback names actual failures and keeps malformed decisions blocked."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.web.composer import tool_batch
from elspeth.web.composer._compose_loop_carriers import _AdvisorCallSuccess, _AdvisorProviderFailure, _CallModelOutcome, _DispatchOutcome
from elspeth.web.composer.advisor_abandonment import AdvisorAbandonmentContextUnavailable, build_abandonment_review
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_batch import BatchAccumulator, ToolBatchContext, ToolBatchCustody, ToolBatchProvenance, run_tool_batch
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import (
    _composer_service_with_session,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)


async def _dispatch_batch(
    service: ComposerServiceImpl,
    session_id: str,
    response: Any,
    *,
    state: CompositionState,
    user_id: str,
    current_state_id: str | None = None,
    advisor_calls_used: int = 0,
    deadline_seconds: float = 60.0,
) -> tuple[_DispatchOutcome, BufferingRecorder, list[dict[str, Any]]]:
    """Dispatch one provider tool batch through the shared owner ``tool_batch.run_tool_batch``.

    The context is built from the service exactly as Continuation builds it
    from its restored runtime (``continuation_runtime.build_runtime_tool_batch_context``),
    under a real fenced COMPOSE context.
    """
    sessions = service._require_sessions_service()
    session_uuid = UUID(session_id)
    preferences = await sessions.get_composer_preferences(session_uuid)
    ceiling = ComposerMutationCeiling(session_id, "dispatch-request", preferences.trust_mode)
    snapshot, policy_catalog = service._policy_context.build(user_id)
    recorder = BufferingRecorder()
    llm_messages: list[dict[str, Any]] = []
    with fenced_operation_context(sessions._engine, session_id) as context:
        ctx = ToolBatchContext(
            session_tools=service._session_tools,
            provenance=ToolBatchProvenance(
                model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
            ),
            custody=ToolBatchCustody(
                data_dir=service._data_dir,
                session_engine=service._session_engine,
                secret_service=service._secret_service,
                secret_wiring_policy=service._secret_wiring_policy,
                max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
            ),
            redaction_telemetry=service._redaction_telemetry,
            preflight=service._preflight,
            schema_disclosure=service._schema_disclosure,
            advisor_checkpoint=service._advisor_checkpoint,
            advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
            advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
            recorder=recorder,
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache=service._preflight.new_cache(),
            session_id=session_id,
            session_operation_context=context,
            user_id=user_id,
            user_message_id=None,
            user_message_content=None,
            current_state_id=current_state_id,
            actor=f"composer-web:user:{user_id}",
            initial_version=state.version,
            deadline=asyncio.get_running_loop().time() + deadline_seconds,
            progress=None,
            session_scope=f"session:{session_id}",
            turn_sessions_service=sessions,
            turn_session_uuid=session_uuid,
            turn_preferences=preferences,
            mutation_ceiling=ceiling,
            review_required=not ceiling.permits_auto(preferences.trust_mode),
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=policy_catalog,
            tool_contract_dialect=service._planner_dialect,
            session_operation_authority=sessions.session_operation_authority,
        )
        acc = BatchAccumulator(state=state, last_validation=None, last_runtime_preflight=None, advisor_calls_used=advisor_calls_used)
        outcome, _advisor_calls = await run_tool_batch(
            call_model=_CallModelOutcome(completion=_admit_composer_llm_completion(response)), ctx=ctx, acc=acc, llm_messages=llm_messages
        )
    return outcome, recorder, llm_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["unstructured prose", "{}", '{"decision":"STOP_CONFIRMED"}'])
async def test_malformed_abandonment_reply_has_no_invented_exception_class(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    result = await _advisor_outcome(monkeypatch, reply=reply)

    assert type(result) is _AdvisorProviderFailure
    assert result.error_class is None


@pytest.mark.asyncio
async def test_unavailable_abandonment_context_names_the_caught_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = AdvisorAbandonmentContextUnavailable("synthetic unavailable evidence")
    result = await _advisor_outcome(monkeypatch, failure=failure)

    assert type(result) is _AdvisorProviderFailure
    assert result.error_class == type(failure).__name__


@pytest.mark.asyncio
async def test_valid_abandonment_reply_remains_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = json.dumps({"decision": "STOP_CONFIRMED", "reason": "Required input is unavailable.", "next_action": None, "question": None})
    result = await _advisor_outcome(monkeypatch, reply=reply)

    assert type(result) is _AdvisorCallSuccess
    assert result.abandonment_response is not None
    assert result.abandonment_response.decision == "STOP_CONFIRMED"


@pytest.mark.asyncio
@pytest.mark.parametrize("advisor_first", [False, True])
async def test_mixed_abandonment_batch_refuses_every_call_before_effects(monkeypatch: pytest.MonkeyPatch, advisor_first: bool) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
    state = _empty_state()
    before = state.to_dict()
    calls = [
        {
            "id": "stop-review",
            "name": "request_advisor_hint",
            "arguments": {"trigger": "cannot_complete", "problem_summary": "No input.", "recent_errors": [], "attempted_actions": []},
        },
        {"id": "mutate", "name": "set_metadata", "arguments": {"patch": {"name": "must-not-apply"}}},
    ]
    if not advisor_first:
        calls.reverse()

    def forbidden_tool(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Mixed abandonment batch dispatched a tool")

    async def forbidden_advisor(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Mixed abandonment batch entered the Advisor")

    monkeypatch.setattr(tool_batch, "execute_tool", forbidden_tool)
    monkeypatch.setattr(service._advisor_checkpoint, "_call_advisor_for_tool", forbidden_advisor)
    # The shared batch owner Continuation drives refuses the whole batch.
    result, recorder, _messages = await _dispatch_batch(
        service, session_id, _make_llm_response(tool_calls=calls), state=state, user_id="test-user"
    )

    assert result.state.to_dict() == before
    assert result.turn_has_mutation is False
    assert len(recorder.invocations) == 2
    for invocation in recorder.invocations:
        assert invocation.status is ComposerToolStatus.ARG_ERROR
        assert invocation.error_class == "ToolArgumentError"
        assert "every call in this batch was refused before effects" in invocation.error_message


async def _advisor_outcome(
    monkeypatch: pytest.MonkeyPatch, *, reply: str = "", failure: Exception | None = None
) -> _AdvisorCallSuccess | _AdvisorProviderFailure:
    service, _session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
    state = _empty_state()
    snapshot, _catalog = service._policy_context.build(None)
    review = build_abandonment_review(
        request="Build the requested workflow.",
        state=state,
        plugin_snapshot=snapshot,
        messages=[],
        request_identity="taxonomy-test",
        request_scope=(None, None, "taxonomy-test"),
        registration_revision=0,
    )

    async def completion(*_args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        assert kwargs["structured_output"] is True
        if failure is not None:
            raise failure
        return reply, {}

    monkeypatch.setattr(service._advisor_checkpoint, "_call_advisor_with_audit", completion)
    return await service._advisor_checkpoint._call_advisor_for_tool(
        RequestAdvisorHintArgumentsModel(
            trigger="cannot_complete", problem_summary="Required input is unavailable.", recent_errors=[], attempted_actions=[]
        ),
        recorder=None,
        abandonment_context=review,
    )
