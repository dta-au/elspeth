"""Actual tool feedback keeps exact JSON wire and complete decoded guard input."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import litellm
import pytest
import pytest_asyncio
from sqlalchemy import select

from elspeth.contracts.composer_audit import ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer import provider_gateway, tool_batch
from elspeth.web.composer import service as service_module
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome, _DispatchOutcome
from elspeth.web.composer.advisor_request import advisor_feedback_payload
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl, _reply_only_messages
from elspeth.web.composer.state import SourceSpec
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.composer._helpers import _empty_state, _make_settings, _mock_catalog

_USER = "Explain the neutral schema fields."
_TOOL_ID = "feedback-call"
_PRIMARY = "openai/gpt-4o"
_ADVISOR = "openai/o3"
_GUARD_METADATA = {PROMPT_GUARD_CONTENT_PARTS_KEY, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY, PROMPT_GUARD_TRUST_KEY}


@dataclass
class _Invocation:
    service: ComposerServiceImpl
    sessions: SessionServiceImpl
    context: SessionOperationContext
    owner: ProviderInvocationOwner
    coordinator: RequiredWorkCoordinator
    user_message_id: str
    detector_text: str | None = None
    azure_requests: list[dict[str, Any]] = field(default_factory=list)
    sdk_requests: list[dict[str, Any]] = field(default_factory=list)
    histories: list[list[dict[str, Any]]] = field(default_factory=list)
    dispatches: list[_DispatchOutcome] = field(default_factory=list)

    def guard_checks(self) -> list[dict[str, Any]]:
        with self.sessions._engine.connect() as connection:
            rows = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == self.context.fence.session_id, chat_messages_table.c.role == "audit")
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        return [row.tool_calls[0]["check"] for row in rows if row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"]

    def assert_settled_attempts(self, expected: int) -> None:
        with self.sessions._engine.connect() as connection:
            attempts = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == self.context.fence.session_id)
            ).all()
        assert len(attempts) == expected
        assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
        self.coordinator.settlement_failure_receipts()


@asynccontextmanager
async def _owned_turn(
    sessions: SessionServiceImpl, session_id: UUID
) -> AsyncIterator[tuple[SessionOperationContext, RequiredWorkCoordinator, ProviderInvocationOwner]]:
    lease = await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    async with lease:
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, lease.context, invocation_id=str(uuid4()))
        )
        lease.bind_required_work(coordinator)
        binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
        owner = ProviderInvocationOwner(service=sessions, required_work=binding)
        try:
            yield lease.context, coordinator, owner
        finally:
            coordinator.settlement_failure_receipts()
    coordinator.assert_completed()


async def _prepare_invocation(
    sessions: SessionServiceImpl,
    context: SessionOperationContext,
    coordinator: RequiredWorkCoordinator,
    owner: ProviderInvocationOwner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> _Invocation:
    original_batch = tool_batch.run_tool_batch
    holder: list[_Invocation] = []

    def inspect(request: httpx.Request) -> httpx.Response:
        current = holder[0]
        body = json.loads(request.content)
        current.azure_requests.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [
                    {"attackDetected": current.detector_text is not None and current.detector_text in document}
                    for document in body["documents"]
                ],
            },
        )

    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="synthetic-guard-credential"),
            http_transport=httpx.MockTransport(inspect),
        )
    )
    monkeypatch.setattr(provider_gateway, "build_prompt_guard_mount", lambda _settings: mount)
    monkeypatch.setattr(service_module, "build_prompt_guard_mount", lambda _settings: mount)
    settings = _make_settings(
        data_dir=tmp_path,
        composer_model=_PRIMARY,
        composer_advisor_model=_ADVISOR,
        composer_prompt_guard_mode="required",
    )
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(), settings=settings, sessions_service=sessions, session_engine=sessions._engine
    )
    user_row = await sessions.add_message(
        UUID(context.fence.session_id), "user", _USER, writer_principal="route_user_message", session_operation_context=context
    )
    current = _Invocation(service, sessions, context, owner, coordinator, str(user_row.id))
    holder.append(current)

    async def observe_batch(
        *,
        call_model: _CallModelOutcome,
        ctx: tool_batch.ToolBatchContext,
        acc: tool_batch.BatchAccumulator,
        llm_messages: list[dict[str, Any]],
    ) -> tuple[_DispatchOutcome, int]:
        result = await original_batch(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages)
        current.histories.append(deepcopy(llm_messages))
        current.dispatches.append(result[0])
        return result

    monkeypatch.setattr(tool_batch, "run_tool_batch", observe_batch)
    return current


@pytest_asyncio.fixture
async def invocation(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[_Invocation]:
    sessions = composer_service_with_real_sessions._sessions_service
    assert sessions is not None
    async with _owned_turn(sessions, UUID(result_session_id)) as (context, coordinator, owner):
        yield await _prepare_invocation(sessions, context, coordinator, owner, monkeypatch, tmp_path)


def _completion(*, content: str | None = None, name: str | None = None, arguments: str = "{}", model: str) -> litellm.ModelResponse:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if name is not None:
        message["tool_calls"] = [{"id": _TOOL_ID, "type": "function", "function": {"name": name, "arguments": arguments}}]
    return litellm.ModelResponse(
        model=model,
        choices=[{"index": 0, "message": message}],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


async def _produce_feedback(
    current: _Invocation, monkeypatch: pytest.MonkeyPatch, producer: Literal["advisor", "argument_error"]
) -> tuple[dict[str, Any], str, int]:
    # Persisted empty sessions enter the planner. Exercise the normal tool loop
    # against an ordinary existing draft while retaining its physical custody.
    state = _empty_state().with_source(
        SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": str(Path(current.service._data_dir) / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )
    )
    guidance = "GUIDANCE_START " + "界" * 1500 + '\n"quoted field" https://catalog.example.test GUIDANCE_END'
    name = "request_advisor_hint" if producer == "advisor" else "未知の道具"
    error = f"Tool '{name}' arguments must be a JSON object, got list."
    needle = guidance if producer == "advisor" else error
    current.detector_text = needle
    arguments = (
        json.dumps(
            {
                "trigger": "proactive_security_safety",
                "problem_summary": "Explain the neutral field contracts.",
                "recent_errors": [],
                "attempted_actions": [],
            }
        )
        if producer == "advisor"
        else "[]"
    )

    async def complete(**kwargs: Any) -> litellm.ModelResponse:
        current.sdk_requests.append(deepcopy(kwargs))
        if len(current.sdk_requests) == 1:
            assert kwargs["model"] == _PRIMARY
            return _completion(name=name, arguments=arguments, model=_PRIMARY)
        if producer == "advisor" and len(current.sdk_requests) == 2:
            assert kwargs["model"] == _ADVISOR
            return _completion(content=guidance, model=_ADVISOR)
        raise AssertionError("Complete decoded tool feedback must be blocked before the next model SDK entry")

    monkeypatch.setattr(litellm, "acompletion", complete)
    with pytest.raises(PromptGuardRefused) as caught:
        await current.service.compose(
            _USER,
            [],
            state,
            session_id=current.context.fence.session_id,
            user_id="phase3-test-user",
            user_message_id=current.user_message_id,
            session_operation_context=current.context,
            required_work=current.owner.required_work,
            provider_owner=current.owner,
        )
    assert caught.value.disposition == "blocked"
    checks = current.guard_checks()
    assert checks[-1]["reason"] == "policy_blocked"
    assert [check["role"] for check in checks] == (["composer", "advisor", "composer"] if producer == "advisor" else ["composer"] * 2)
    assert [check["disposition"] for check in checks] == (["allow"] * (len(checks) - 1) + ["blocked"])
    assert all(request["userPrompt"] == _USER for request in current.azure_requests)
    documents = "\n".join(document for request in current.azure_requests for document in request["documents"])
    assert needle in documents
    assert len(current.histories) == len(current.dispatches) == 1
    tool = next(message for message in current.histories[0] if message["role"] == "tool")
    assert tool["tool_call_id"] == _TOOL_ID
    parts = tool[PROMPT_GUARD_CONTENT_PARTS_KEY]
    assert "".join(part.text for part in parts) == tool["content"]
    assert all(part.trust is None for part in parts)
    dispatch = current.dispatches[0]
    assert dispatch.state == state
    assert dispatch.plugin_crash is None and dispatch.advisor_failure is None
    outcome = dispatch.tool_outcomes[0]
    if producer == "advisor":
        payload = deep_thaw(outcome.response)
        assert payload["guidance"] == guidance
        assert payload["status"] == "SUCCESS"
        assert payload["budget_used"] == 1 and payload["budget_remaining"] == 3
        assert payload["model"] == _ADVISOR
        assert payload["prompt_tokens"] == 3 and payload["completion_tokens"] == 2
        assert tool["content"] == json.dumps(advisor_feedback_payload(payload))
        assert {"budget_used", "budget_remaining"}.isdisjoint(json.loads(tool["content"]))
        assert not dispatch.turn_has_mutation and not dispatch.turn_has_discovery
        assert dispatch.private_invocations[0].status is ComposerToolStatus.SUCCESS
    else:
        assert tool["content"] == json.dumps({"error": error})
        assert outcome.error_class == "ToolArgumentError"
        assert outcome.error_category is ToolArgumentErrorCategory.WIRE_NOT_OBJECT
        assert dispatch.turn_has_mutation and not dispatch.turn_has_discovery
        assert dispatch.private_invocations[0].status is ComposerToolStatus.ARG_ERROR
    calls = 2 if producer == "advisor" else 1
    assert len(current.sdk_requests) == calls
    assert all(_GUARD_METADATA.isdisjoint(message) for request in current.sdk_requests for message in request["messages"])
    current.assert_settled_attempts(calls)
    return tool, needle, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["advisor", "argument_error"])
@pytest.mark.parametrize("reply_only", [False, True], ids=["next-primary", "reply-only"])
async def test_actual_feedback_reaches_azure_complete_and_keeps_original_json_wire(invocation, monkeypatch, producer, reply_only):
    tool, needle, calls = await _produce_feedback(invocation, monkeypatch, producer)
    if reply_only:
        quoted = _reply_only_messages([tool])
        record, offset = json.JSONDecoder().raw_decode(quoted[0]["content"].split("\n", 1)[1])
        assert record == {key: value for key, value in tool.items() if key not in _GUARD_METADATA}
        assert offset > 0
        assert "".join(part.text for part in quoted[0][PROMPT_GUARD_CONTENT_PARTS_KEY]) == quoted[0]["content"]
        before = len(invocation.azure_requests)
        with prompt_guard_user_scope(_USER), pytest.raises(PromptGuardRefused) as caught:
            await invocation.service._provider_gateway._call_llm_with_audit(
                quoted,
                [],
                timeout=10,
                recorder=BufferingRecorder(),
                provider_custody=invocation.owner.mint(ProviderInvocationFamily.PRIMARY),
            )
        assert caught.value.disposition == "blocked"
        assert invocation.guard_checks()[-1]["reason"] == "policy_blocked"
        documents = "\n".join(document for request in invocation.azure_requests[before:] for document in request["documents"])
        assert needle in documents
        assert len(invocation.sdk_requests) == calls
        invocation.assert_settled_attempts(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_only", [False, True], ids=["tool", "reply-only"])
@pytest.mark.parametrize("rejection", ["credential", "oversize"])
async def test_generic_feedback_credentials_and_atomic_oversize_never_dispatch(invocation, monkeypatch, reply_only, rejection):
    value = "https://fixture-user:fixture-password@catalog.example.test" if rejection == "credential" else "界" * 10_001
    content = json.dumps({"error": value})
    messages = [{"role": "tool", "tool_call_id": _TOOL_ID, "content": content, PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(content)}]
    if reply_only:
        messages = _reply_only_messages(messages)

    async def must_not_dispatch(**kwargs: Any) -> litellm.ModelResponse:
        invocation.sdk_requests.append(kwargs)
        raise AssertionError("Credential or complete oversized scalar must not enter the model SDK")

    monkeypatch.setattr(litellm, "acompletion", must_not_dispatch)
    with prompt_guard_user_scope(_USER):
        if rejection == "credential":
            with pytest.raises(CredentialMaterialRefused):
                await invocation.service._provider_gateway._call_llm_with_audit(
                    messages,
                    [],
                    timeout=10,
                    recorder=BufferingRecorder(),
                    provider_custody=invocation.owner.mint(ProviderInvocationFamily.PRIMARY),
                )
            assert invocation.guard_checks() == []
        else:
            with pytest.raises(PromptGuardRefused) as caught:
                await invocation.service._provider_gateway._call_llm_with_audit(
                    messages,
                    [],
                    timeout=10,
                    recorder=BufferingRecorder(),
                    provider_custody=invocation.owner.mint(ProviderInvocationFamily.PRIMARY),
                )
            assert caught.value.disposition == "unavailable"
            assert [check["reason"] for check in invocation.guard_checks()] == ["unsupported_input"]
    assert invocation.azure_requests == invocation.sdk_requests == []
    invocation.assert_settled_attempts(0)
