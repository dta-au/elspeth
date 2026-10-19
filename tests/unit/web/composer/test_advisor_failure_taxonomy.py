"""Advisor feedback names actual failures and keeps malformed decisions blocked."""

from __future__ import annotations

import json
from typing import Any

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.web.composer import tool_batch
from elspeth.web.composer._compose_loop_carriers import _AdvisorCallSuccess, _AdvisorProviderFailure
from elspeth.web.composer.advisor_abandonment import AdvisorAbandonmentContextUnavailable, build_abandonment_review
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)


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
    responses = iter([_make_llm_response(tool_calls=calls), _make_llm_response(content="No changes were saved.")])

    async def completion(**_kwargs: Any) -> Any:
        return next(responses)

    def forbidden_tool(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Mixed abandonment batch dispatched a tool")

    async def forbidden_advisor(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Mixed abandonment batch entered the Advisor")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(tool_batch, "execute_tool", forbidden_tool)
    monkeypatch.setattr(service._advisor_checkpoint, "_call_advisor_for_tool", forbidden_advisor)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    result = await service.compose("Discuss what input is required.", [], state, session_id=session_id)

    assert result.state.to_dict() == before
    assert len(result.tool_invocations) == 2
    for invocation in result.tool_invocations:
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
