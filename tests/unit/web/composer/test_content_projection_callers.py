"""Real Composer callers bind diagnostic content to their fenced session."""

import json
from dataclasses import replace
from uuid import UUID

import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX
from elspeth.web.composer.state import PipelineMetadata
from elspeth.web.composer.turn_effects import TurnEvidence
from tests.unit.web.composer._helpers import _empty_state
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_wiring import _Client, _guarded_service

pytest_plugins = ("tests.unit.web.composer.test_prompt_guard_wiring",)


def test_primary_view_uses_real_session_and_current_graph(invocation, monkeypatch):
    _engine, _sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch)
    state = replace(_empty_state(), metadata=PipelineMetadata(description="x" * 3_000))
    messages = service._build_messages(
        [],
        state,
        "Inspect the current graph.",
        session_id=operation.fence.session_id,
        user_id="alice",
        session_operation_context=operation,
    )
    context = next(message["content"] for message in messages if message["content"].startswith(STATE_CONTEXT_PREFIX))
    view = json.loads(context.split("\n", 1)[1])["current_state"]
    assert view["content_manifest"]["session_id"] == operation.fence.session_id
    assert view["content_manifest"]["composition_content_hash"] == composition_content_hash(state)
    assert state.metadata.description not in context
    assert view["content_manifest"]["values"]
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_small_graph_review_uses_real_current_scope_and_fresh_audited_request(invocation, monkeypatch):
    engine, sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    client = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    calls = []
    reply = json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"})

    async def completion(**kwargs):
        calls.append(kwargs)
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": reply}}],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    state = replace(_empty_state(), metadata=PipelineMetadata(description="A neutral current description."))
    user_row = await sessions.add_message(
        UUID(operation.fence.session_id),
        "user",
        "Review the complete graph.",
        writer_principal="route_user_message",
        session_operation_context=operation,
    )
    evidence = TurnEvidence(
        session_id=operation.fence.session_id,
        session_operation_context=operation,
        request_id=str(user_row.id),
        request="Review the complete graph.",
        initial_state_id=None,
        initial_state=state,
        actor_user_id="alice",
    )
    review = evidence.review_candidate(
        candidate="The supplied neutral graph evidence was reviewed.",
        request="Review the complete graph.",
        current_state=state,
    )
    recorder = BufferingRecorder()
    with prompt_guard_user_scope("Review the complete graph."):
        for _ in range(2):
            verdict = await service._advisor_checkpoint._run_advisor_checkpoint(
                phase="end",
                state=state,
                session_id=operation.fence.session_id,
                recorder=recorder,
                user_message="Review the complete graph.",
                session_operation_context=operation,
                provider_owner=owner,
                publication_review=review,
            )
            assert verdict.ok and not verdict.blocking
    checks = _checks(engine)
    assert len(calls) == len(client.requests) == len(checks) == len(recorder.llm_calls) == 2
    assert client.requests[0] is not client.requests[1]
    for call, record, check, request in zip(calls, recorder.llm_calls, checks, client.requests, strict=True):
        assert operation.fence.session_id in str(call["messages"])
        assert composition_content_hash(state) in str(call["messages"])
        assert state.metadata.description in str(call["messages"])
        assert record.messages_hash == stable_hash(call["messages"])
        assert check["request_hash"] == request.request_hash
    coordinator.assert_completed()


def test_primary_view_refuses_a_different_session_before_rendering(invocation, monkeypatch):
    _engine, _sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch)
    with pytest.raises(AuditIntegrityError, match="session"):
        service._build_messages(
            [],
            _empty_state(),
            "Inspect the graph.",
            session_id="00000000-0000-0000-0000-000000000001",
            user_id="alice",
            session_operation_context=operation,
        )
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_advisor_does_not_accept_clean_for_unseen_graph_values(invocation, monkeypatch):
    _engine, _sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": '{"verdict":"CLEAN"}'}}],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    state = replace(_empty_state(), metadata=PipelineMetadata(description="x" * 3_000))
    verdict = await service._advisor_checkpoint._run_advisor_checkpoint(
        phase="end",
        state=state,
        session_id=operation.fence.session_id,
        recorder=BufferingRecorder(),
        user_message="Review the complete graph.",
        session_operation_context=operation,
        provider_owner=owner,
    )
    assert not verdict.ok and verdict.failure_class == "unavailable"
    assert verdict.publication_binding is None
    assert calls == []
    coordinator.assert_completed()
