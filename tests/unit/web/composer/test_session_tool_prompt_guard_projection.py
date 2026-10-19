"""Actual session-aware feedback reaches the physical guard without JSON escaping."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import litellm
import pytest
import pytest_asyncio
from sqlalchemy import select

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer import provider_gateway, tool_batch
from elspeth.web.composer import service as service_module
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl, _reply_only_messages
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
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
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.composer._helpers import _make_settings, _mock_catalog
from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion

_USER = "Explain the neutral schema fields."
_TOOL_ID = "session-feedback-call"
_PRIMARY = "openai/gpt-4o"
_ADVISOR = "openai/o3"


@dataclass
class _Invocation:
    composer: ComposerServiceImpl
    sessions: SessionServiceImpl
    context: SessionOperationContext
    owner: ProviderInvocationOwner
    coordinator: RequiredWorkCoordinator
    user_message_id: str
    needle: str | None = None
    azure_requests: list = field(default_factory=list)
    sdk_requests: list = field(default_factory=list)
    feedback: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    dispatches: list = field(default_factory=list)

    def checks(self):
        with self.sessions._engine.connect() as connection:
            rows = connection.execute(
                select(chat_messages_table).where(chat_messages_table.c.session_id == self.context.fence.session_id)
            ).all()
        return [
            row.tool_calls[0]["check"]
            for row in rows
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
        ]

    def assert_settled(self):
        with self.sessions._engine.connect() as connection:
            attempts = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == self.context.fence.session_id)
            ).all()
        assert len(attempts) == len(self.sdk_requests)
        assert all(row.settled_at is not None and row.ledger_entry_id is not None for row in attempts)
        self.coordinator.settlement_failure_receipts()


@asynccontextmanager
async def _owned_turn(sessions, session_id):
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
        owner = ProviderInvocationOwner(service=sessions, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        try:
            yield lease.context, coordinator, owner
        finally:
            coordinator.settlement_failure_receipts()
    coordinator.assert_completed()


@pytest_asyncio.fixture
async def invocation(composer_service_with_real_sessions, result_session_id, monkeypatch, tmp_path):
    sessions = composer_service_with_real_sessions._sessions_service
    assert sessions is not None
    async with _owned_turn(sessions, UUID(result_session_id)) as (context, coordinator, owner):
        holder = []

        def inspect(request):
            current = holder[0]
            body = json.loads(request.content)
            current.azure_requests.append((request.url.host, body))
            return httpx.Response(
                200,
                json={
                    "userPromptAnalysis": {"attackDetected": False},
                    "documentsAnalysis": [
                        {
                            "attackDetected": request.url.host == "primary.example.test"
                            and current.needle is not None
                            and current.needle in document
                        }
                        for document in body["documents"]
                    ],
                },
            )

        def mount(config):
            return PromptGuardMount(AzurePromptGuardClient(config, http_transport=httpx.MockTransport(inspect)))

        monkeypatch.setattr(provider_gateway, "build_prompt_guard_mount", mount)
        monkeypatch.setattr(service_module, "build_prompt_guard_mount", mount)
        settings = _make_settings(
            data_dir=tmp_path,
            composer_model=_PRIMARY,
            composer_advisor_model=_ADVISOR,
            composer_prompt_guard_mode="required",
            composer_prompt_guard=PromptGuardSettings(endpoint="https://primary.example.test", subscription_key="offline-primary"),
            composer_advisor_prompt_guard=PromptGuardSettings(endpoint="https://advisor.example.test", subscription_key="offline-advisor"),
        )
        composer = ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(), settings=settings, sessions_service=sessions, session_engine=sessions._engine
        )
        human = await sessions.add_message(
            UUID(context.fence.session_id), "user", _USER, writer_principal="route_user_message", session_operation_context=context
        )
        current = _Invocation(composer, sessions, context, owner, coordinator, str(human.id))
        holder.append(current)
        original_session = composer._session_tools._dispatch_session_aware_tool
        original_batch = tool_batch.run_tool_batch

        async def observe_session(**kwargs):
            outcome = await original_session(**kwargs)
            tool = deepcopy(kwargs["llm_messages"][-1])
            assert tool["role"] == "tool" and tool["tool_call_id"] == _TOOL_ID
            payload = json.loads(tool["content"])
            current.needle = payload["error"] if "error" in payload else payload["data"]["message"]
            current.feedback.append(tool)
            current.outcomes.append(outcome)
            return outcome

        async def observe_batch(**kwargs):
            outcome = await original_batch(**kwargs)
            current.dispatches.append(outcome[0])
            return outcome

        monkeypatch.setattr(composer._session_tools, "_dispatch_session_aware_tool", observe_session)
        monkeypatch.setattr(tool_batch, "run_tool_batch", observe_batch)
        yield current


def _review_state():
    # An ordinary incomplete draft can stage review before source/sinks exist;
    # the actual production preflight explicitly permits that sequence.
    node = NodeSpec(
        id="review",
        node_type="transform",
        plugin="llm",
        input="rows",
        on_success="out",
        on_error="discard",
        options={"prompt_template": "Rate {{interpretation:cool}}."},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    return CompositionState(nodes=(node,), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["missing_state", "argument_error", "success"])
@pytest.mark.parametrize("reply_only", [False, True], ids=["next-primary", "reply-only"])
async def test_real_session_review_feedback_keeps_exact_wire_and_decoded_guard_input(invocation, monkeypatch, producer, reply_only):
    current = invocation
    # The negative case is an unsaved draft with no persisted FK target.
    # An empty persisted session selects the planner before the session tool.
    state = _review_state()
    current_state_id = None
    if producer != "missing_state":
        serialized = state.to_dict()
        saved_state = await current.sessions.save_composition_state(
            UUID(current.context.fence.session_id),
            CompositionStateData(
                nodes=serialized["nodes"], sources=serialized["sources"], metadata_=serialized["metadata"], is_valid=False
            ),
            provenance="session_seed",
            session_operation_context=current.context,
        )
        current_state_id = str(saved_state.id)
    target = "absent" if producer == "argument_error" else "review"
    arguments = {"affected_node_id": target, "kind": "vague_term", "user_term": "cool", "llm_draft": "Clear and useful wording."}

    async def complete(**kwargs):
        current.sdk_requests.append(deepcopy(kwargs))
        if len([request for request in current.sdk_requests if request["model"] == _PRIMARY]) == 1 and kwargs["model"] == _PRIMARY:
            content = None
            calls = [
                {
                    "id": _TOOL_ID,
                    "type": "function",
                    "function": {"name": "request_interpretation_review", "arguments": json.dumps(arguments)},
                }
            ]
        elif kwargs["model"] == _ADVISOR:
            content = json.dumps(
                {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
            )
            calls = None
        else:
            raise AssertionError("Session feedback must reach the detector before another primary SDK call")
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": content, "tool_calls": calls}}],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    with pytest.raises(PromptGuardRefused) as caught:
        await current.composer.compose(
            _USER,
            [],
            state,
            session_id=current.context.fence.session_id,
            current_state_id=current_state_id,
            user_id="phase3-test-user",
            user_message_id=current.user_message_id,
            session_operation_context=current.context,
            required_work=current.owner.required_work,
            provider_owner=current.owner,
        )
    assert caught.value.disposition == "blocked"
    assert current.checks()[-1]["reason"] == "policy_blocked"
    assert len(current.feedback) == len(current.outcomes) == len(current.dispatches) == 1
    tool, outcome, dispatch = current.feedback[0], current.outcomes[0], current.dispatches[0]
    assert "".join(part.text for part in tool[PROMPT_GUARD_CONTENT_PARTS_KEY]) == tool["content"]
    payload = json.loads(tool["content"])
    expected_wire = json.dumps(outcome.result.to_dict()) if outcome.result is not None else json.dumps(payload)
    assert tool["content"] == expected_wire
    assert not outcome.is_discovery and not dispatch.turn_has_discovery and dispatch.turn_has_mutation
    assert dispatch.plugin_crash is dispatch.advisor_failure is None
    events = await current.sessions.list_interpretation_events(UUID(current.context.fence.session_id), status="all")
    requested = [event for event in events if event.tool_call_id == _TOOL_ID]
    assert len(requested) == int(producer == "success")
    assert dispatch.private_invocations[0].status is (ComposerToolStatus.SUCCESS if producer == "success" else ComposerToolStatus.ARG_ERROR)
    if requested:
        assert requested[0].llm_draft == arguments["llm_draft"] and requested[0].user_term == "cool"
    primary_documents = "\n".join(
        document for host, request in current.azure_requests if host == "primary.example.test" for document in request["documents"]
    )
    assert current.needle in primary_documents
    assert len([request for request in current.sdk_requests if request["model"] == _PRIMARY]) == 1
    current.assert_settled()
    if reply_only:
        before = len(current.azure_requests)
        with prompt_guard_user_scope(_USER), pytest.raises(PromptGuardRefused) as quoted_refusal:
            await current.composer._provider_gateway._call_llm_with_audit(
                _reply_only_messages([tool]),
                [],
                timeout=10,
                recorder=BufferingRecorder(),
                provider_custody=current.owner.mint(ProviderInvocationFamily.PRIMARY),
            )
        assert quoted_refusal.value.disposition == "blocked"
        assert current.needle in "\n".join(
            document for _host, request in current.azure_requests[before:] for document in request["documents"]
        )
        current.assert_settled()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_only", [False, True])
@pytest.mark.parametrize("kind", ["cjk", "dense_quotes", "credential", "atomic_oversize"])
async def test_session_feedback_boundary_still_refuses_credentials_and_whole_oversized_scalars(invocation, monkeypatch, reply_only, kind):
    current = invocation
    value = {
        "cjk": "界" * 1500,
        "dense_quotes": '"field",' * 800,
        "credential": "https://fixture-user:fixture-password@example.test",
        "atomic_oversize": "界" * 10_001,
    }[kind]
    content = json.dumps({"error": value})
    messages = [{"role": "tool", "tool_call_id": _TOOL_ID, "content": content, PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(content)}]
    if reply_only:
        messages = _reply_only_messages(messages)

    async def never(**kwargs):
        current.sdk_requests.append(kwargs)
        raise AssertionError("Closed boundary must not enter the SDK")

    monkeypatch.setattr(litellm, "acompletion", never)
    with prompt_guard_user_scope(_USER):
        if kind == "credential":
            with pytest.raises(CredentialMaterialRefused):
                await current.composer._provider_gateway._call_llm_with_audit(
                    messages,
                    [],
                    timeout=10,
                    recorder=BufferingRecorder(),
                    provider_custody=current.owner.mint(ProviderInvocationFamily.PRIMARY),
                )
        elif kind == "atomic_oversize":
            with pytest.raises(PromptGuardRefused) as caught:
                await current.composer._provider_gateway._call_llm_with_audit(
                    messages,
                    [],
                    timeout=10,
                    recorder=BufferingRecorder(),
                    provider_custody=current.owner.mint(ProviderInvocationFamily.PRIMARY),
                )
            assert caught.value.disposition == "unavailable"
            assert current.checks()[-1]["reason"] == "unsupported_input"
        else:
            current.needle = value
            with pytest.raises(PromptGuardRefused) as caught:
                await current.composer._provider_gateway._call_llm_with_audit(
                    messages,
                    [],
                    timeout=10,
                    recorder=BufferingRecorder(),
                    provider_custody=current.owner.mint(ProviderInvocationFamily.PRIMARY),
                )
            assert caught.value.disposition == "blocked"
            assert current.checks()[-1]["reason"] == "policy_blocked"
            assert value in "\n".join(document for _host, request in current.azure_requests for document in request["documents"])
    assert current.sdk_requests == []
    if kind in {"credential", "atomic_oversize"}:
        assert current.azure_requests == []
    current.assert_settled()


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed_discovery", [False, True], ids=["only-missing-component", "mixed-missing-component"])
@pytest.mark.parametrize(
    "component", ["界" * 1700, '"field",' * 800, "\x01" * 1700], ids=["cjk-countercontrol", "dense-quotes", "escaped-control"]
)
async def test_real_planner_missing_component_feedback_inspects_complete_authored_component(
    tmp_path, tool_context, monkeypatch, mixed_discovery, component
):
    unique_evidence = "Component '" + component + "' not found."
    inspected = []

    def inspect(request):
        body = json.loads(request.content)
        inspected.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": unique_evidence in document} for document in body["documents"]],
            },
        )

    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://planner.example.test", subscription_key="offline-planner"),
            http_transport=httpx.MockTransport(inspect),
        )
    )
    calls = [("get_pipeline_state", {"component": component})]
    if mixed_discovery:
        calls.append(("get_plugin_schema", {"plugin_type": "transform", "name": "field_mapper"}))
    completion = _ScriptedCompletion(
        _response(*calls), AssertionError("Missing-component feedback must block before the next physical SDK call")
    )
    with pytest.raises(PromptGuardRefused) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            information_aware=True,
            model_overrides={"prompt_guard_mount": mount, "prompt_guard_mode": "required"},
        )
    assert caught.value.disposition == "blocked"
    assert caught.value._decision.reason == "policy_blocked"
    assert len(completion.requests) == 1
    assert "get_pipeline_state" in {tool["function"]["name"] for tool in completion.requests[0]["tools"]}
    assert unique_evidence in "\n".join(document for request in inspected for document in request["documents"])
