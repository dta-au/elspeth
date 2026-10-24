"""Physical authoring calls use explicit role guards and real fenced audit."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardClient,
    PromptGuardDecision,
    PromptGuardRequest,
    PromptGuardTrust,
)
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import provider_gateway
from elspeth.web.composer import service as service_module
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    interpretation_events_table,
    quota_provider_attempts_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort, _state_from_record
from tests.unit.web.composer._helpers import _empty_state, _make_settings, _mock_catalog
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


class _Client(PromptGuardClient):
    def __init__(self, decision: PromptGuardDecision) -> None:
        self.decision = decision
        self.requests: list[PromptGuardRequest] = []

    @property
    def adapter(self) -> str:
        return "azure_prompt_shields"

    @property
    def policy_fingerprint(self) -> str:
        return "b" * 64

    @property
    def timeout_seconds(self) -> float:
        return 1.0

    async def inspect(self, request: PromptGuardRequest) -> PromptGuardDecision:
        self.requests.append(request)
        return self.decision


@pytest.fixture
def invocation(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Wiring", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    yield engine, service, owner, coordinator
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["composer", "advisor"])
@pytest.mark.parametrize("configured", [False, True])
async def test_denied_physical_invocation_never_admits_or_enters_sdk(invocation, monkeypatch, role, configured):
    engine, _service, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.PRIMARY if role == "composer" else ProviderInvocationFamily.ADVISOR)
    client = _Client(PromptGuardDecision("blocked", "policy_blocked"))
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(PromptGuardRefused):
        await provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=PromptGuardMount(client) if configured else None,
            prompt_guard_role=role,
            prompt_guard_mode="required",
            prompt_guard_user_prompt="Compare the two neutral columns.",
            model="test/model",
            messages=[{"role": "user", "content": "Compare the two neutral columns."}],
        )

    assert sdk_calls == []
    assert custody.attempt is None and custody.call is None
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        rows = connection.execute(select(chat_messages_table)).all()
    assert len(rows) == 1
    check = rows[0].tool_calls[0]["check"]
    assert check["role"] == role
    assert check["disposition"] == ("blocked" if configured else "unavailable")
    assert len(client.requests) == int(configured)
    coordinator.assert_completed()


def _guarded_service(invocation, monkeypatch, *, composer_client=None, advisor_client=None, catalog=None, settings=None):
    engine, sessions, _owner, _coordinator = invocation
    composer_client = composer_client or _Client(PromptGuardDecision("allow", "clean"))
    advisor_client = advisor_client or _Client(PromptGuardDecision("blocked", "policy_blocked"))
    monkeypatch.setattr(provider_gateway, "build_prompt_guard_mount", lambda _settings: PromptGuardMount(composer_client))
    monkeypatch.setattr(service_module, "build_prompt_guard_mount", lambda _settings: PromptGuardMount(advisor_client))
    return ComposerServiceImpl.for_trained_operator(
        catalog=catalog if catalog is not None else _mock_catalog(),
        settings=settings if settings is not None else _make_settings(composer_prompt_guard_mode="required"),
        sessions_service=sessions,
        session_engine=engine,
    )


@pytest.mark.asyncio
async def test_common_primary_preserves_guard_identity_after_prior_calls(invocation, monkeypatch):
    from elspeth.web.dependencies import create_catalog_service
    from tests.unit.web.composer.test_pipeline_planner import _response_with_call_id, _ScriptedCompletion

    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    message = "Build a pipeline that reads a CSV file and writes a CSV file."
    user_row = await sessions.add_message(
        UUID(context.fence.session_id),
        "user",
        message,
        writer_principal="route_user_message",
        session_operation_context=context,
    )
    composer_client = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(
        invocation,
        monkeypatch,
        composer_client=composer_client,
        catalog=create_catalog_service(),
        settings=_make_settings(composer_prompt_guard_mode="required", composer_max_discovery_turns=1),
    )
    completion = _ScriptedCompletion(
        _response_with_call_id("guard-source-discovery", "list_sources", {}),
        _response_with_call_id("guard-sink-discovery", "list_sinks", {}),
    )

    async def complete_then_block_next_primary(**kwargs):
        response = await completion(**kwargs)
        if len(completion.requests) == 2:
            composer_client.decision = PromptGuardDecision("blocked", "policy_blocked")
        return response

    monkeypatch.setattr(litellm, "acompletion", complete_then_block_next_primary)
    with pytest.raises(PromptGuardRefused) as caught:
        await service.compose(
            message,
            [],
            _empty_state(),
            session_id=context.fence.session_id,
            user_message_id=str(user_row.id),
            user_id="alice",
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert caught.value.disposition == "blocked" and len(completion.requests) == 2
    assert len(composer_client.requests) == 3
    assert caught.value.tool_invocations == ()
    with engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table)).all()
        assert connection.execute(select(composition_states_table)).all() == []
        assert connection.execute(select(composition_proposals_table)).all() == []
    llm_rows = [row for row in rows if row.tool_calls and row.tool_calls[0].get("_kind") == "llm_call_audit"]
    assert len(llm_rows) == 2
    assert [llm_call_audit_envelope(call) for call in caught.value.llm_calls] == [
        row.tool_calls[0] for row in sorted(llm_rows, key=lambda row: row.sequence_no)
    ]
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_recovery_fixed_instruction_does_not_hide_variable_context_from_guard(invocation, monkeypatch):
    _engine, _sessions, owner, coordinator = invocation
    client = _Client(PromptGuardDecision("blocked", "policy_blocked"))
    service = _guarded_service(invocation, monkeypatch, composer_client=client)
    context = owner.required_work.coordinator.authority.context
    with pytest.raises(PromptGuardRefused):
        await service.compose_recovery(
            message="Please restore an editable draft.",
            recovery_context=GraphRecoveryContext(None, 0, None, 0, True),
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert len(client.requests) == 1
    documents = "\n".join(client.requests[0].documents)
    assert "malformed_head" in documents
    assert "Choose exactly one terminal recovery control" not in documents
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_denied_primary_preserves_real_resolved_review_graph_and_version(invocation, monkeypatch):
    from tests.unit.web.sessions.test_interpretation_events_routes import _llm_node

    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    sid = UUID(context.fence.session_id)
    saved = await sessions.save_composition_state(
        sid,
        CompositionStateData(nodes=[_llm_node()], metadata_={"name": "Reviewed graph", "description": ""}),
        provenance="tool_call",
        session_operation_context=context,
    )
    pending = await sessions.create_pending_interpretation_event(
        session_id=sid,
        composition_state_id=saved.id,
        affected_node_id="llm_transform_1",
        tool_call_id="review-1",
        user_term="cool",
        kind=InterpretationKind.VAGUE_TERM,
        llm_draft="Compare the neutral columns.",
        model_identifier="anthropic/claude-opus-4-7",
        model_version="2026-05-01",
        provider="anthropic",
        composer_skill_hash="a" * 64,
        session_operation_context=context,
    )
    approved, head = await sessions.resolve_interpretation_event(
        session_id=sid,
        event_id=pending.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="alice",
        session_operation_context=context,
    )
    assert approved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    tables = (composition_states_table, composition_proposals_table, interpretation_events_table)
    with engine.connect() as connection:
        before = [connection.execute(select(table)).all() for table in tables]
    service = _guarded_service(invocation, monkeypatch, composer_client=_Client(PromptGuardDecision("blocked", "policy_blocked")))
    with pytest.raises(PromptGuardRefused) as caught:
        await service.compose(
            "Explain this saved draft.",
            [],
            _state_from_record(head),
            session_id=str(sid),
            current_state_id=str(head.id),
            user_id="alice",
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert caught.value.partial_state is None and caught.value.tool_invocations == () and caught.value.llm_calls == ()
    with engine.connect() as connection:
        assert [connection.execute(select(table)).all() for table in tables] == before
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
    assert (await sessions.get_current_state(sid)).id == head.id
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
@pytest.mark.parametrize("with_recorder", [False, True])
async def test_real_advisor_checkpoint_refusal_is_terminal_with_or_without_recorder(invocation, monkeypatch, phase, with_recorder):
    engine, _sessions, owner, coordinator = invocation
    service = _guarded_service(invocation, monkeypatch)
    recorder = BufferingRecorder() if with_recorder else None
    with prompt_guard_user_scope("Compare columns."), pytest.raises(PromptGuardRefused) as caught:
        await service._advisor_checkpoint._run_advisor_checkpoint(
            phase=phase,
            state=_empty_state(),
            session_id=owner.required_work.coordinator.authority.context.fence.session_id,
            recorder=recorder,
            provider_owner=owner,
            session_operation_context=owner.required_work.coordinator.authority.context,
            user_message="Compare columns.",
        )
    assert caught.value.disposition == "blocked"
    assert caught.value.llm_calls == ()
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        rows = connection.execute(select(chat_messages_table)).all()
    assert len(rows) == 1 and rows[0].tool_calls[0]["_kind"] == "prompt_guard_audit"
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_real_advisor_format_retry_gets_its_own_guard_and_preserves_prior_model_call(invocation, monkeypatch):
    engine, _sessions, owner, coordinator = invocation

    class RetryClient(_Client):
        async def inspect(self, request):
            self.requests.append(request)
            return PromptGuardDecision("allow", "clean") if len(self.requests) == 1 else PromptGuardDecision("blocked", "policy_blocked")

    client = RetryClient(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": "Not the requested JSON.", "tool_calls": None}}],
            usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    recorder = BufferingRecorder()
    with prompt_guard_user_scope("Compare columns."), pytest.raises(PromptGuardRefused) as caught:
        await service._advisor_checkpoint._run_advisor_checkpoint(
            phase="end",
            state=_empty_state(),
            session_id=owner.required_work.coordinator.authority.context.fence.session_id,
            recorder=recorder,
            provider_owner=owner,
            session_operation_context=owner.required_work.coordinator.authority.context,
            user_message="Compare columns.",
        )
    assert len(client.requests) == 2 and len(sdk_calls) == 1
    assert client.requests[0].role == client.requests[1].role == "advisor"
    assert client.requests[0].request_hash != client.requests[1].request_hash
    assert len(caught.value.llm_calls) == len(recorder.llm_calls) == 1
    with engine.connect() as connection:
        assert len(connection.execute(select(quota_provider_attempts_table)).all()) == 1
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_real_title_uses_composer_guard_before_model_and_title_mutation(invocation, monkeypatch, allowed):
    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    client = _Client(PromptGuardDecision("allow", "clean") if allowed else PromptGuardDecision("blocked", "policy_blocked"))
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        assert all(PROMPT_GUARD_TRUST_KEY not in message for message in kwargs["messages"])
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": "Neutral Column Comparison", "tool_calls": None}}],
            usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)

    async def title():
        await _auto_title.maybe_auto_title_session(
            service=sessions,
            session_id=UUID(context.fence.session_id),
            user_message="Compare columns.",
            model="test/model",
            temperature=None,
            seed=None,
            session_operation_context=context,
            prompt_guard_mount=PromptGuardMount(client),
            prompt_guard_mode="required",
            provider_custody=owner.mint(ProviderInvocationFamily.TITLE),
        )

    if allowed:
        await title()
    else:
        with pytest.raises(PromptGuardRefused):
            await title()
    assert len(sdk_calls) == int(allowed)
    assert len(client.requests) == 1 and client.requests[0].role == "composer"
    assert "Compare columns." in client.requests[0].user_prompt
    session = await sessions.get_session(UUID(context.fence.session_id))
    assert session.title == ("Neutral Column Comparison" if allowed else "Wiring")
    with engine.connect() as connection:
        assert len(connection.execute(select(quota_provider_attempts_table)).all()) == int(allowed)
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_manual_advisor_refusal_drains_earlier_mutation_and_model_audit_once(invocation, monkeypatch):
    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch)
    await sessions.update_composer_preferences(
        UUID(context.fence.session_id), trust_mode="auto_commit", density_default="high", actor="user:alice"
    )
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        assert len(sdk_calls) == 1, "The denied advisor must not dispatch and the request must terminate"
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "metadata-1",
                                "type": "function",
                                "function": {"name": "set_metadata", "arguments": json.dumps({"patch": {"name": "Earlier valid edit"}})},
                            },
                            {
                                "id": "advisor-2",
                                "type": "function",
                                "function": {
                                    "name": "request_advisor_hint",
                                    "arguments": json.dumps(
                                        {
                                            "trigger": "proactive_security_safety",
                                            "problem_summary": "Review the neutral draft.",
                                            "recent_errors": [],
                                            "attempted_actions": [],
                                        }
                                    ),
                                },
                            },
                        ],
                    }
                }
            ],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(PromptGuardRefused) as caught:
        await service.compose(
            "Update the draft label and review it.",
            [],
            _empty_state(),
            session_id=context.fence.session_id,
            user_id="alice",
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert len(sdk_calls) == 1
    assert caught.value.tool_invocations == () and len(caught.value.llm_calls) == 1
    assert caught.value.partial_state is None, "P4 already saved the earlier valid work; do not append an identity-copy state"
    current = await sessions.get_current_state(UUID(context.fence.session_id))
    assert current.metadata_["name"] == "Earlier valid edit"
    with engine.connect() as connection:
        saved_states = connection.execute(select(composition_states_table)).all()
    assert len(saved_states) == 1
    await _persist_turn_audit_cohort(
        sessions,
        UUID(context.fence.session_id),
        caught.value.tool_invocations,
        caught.value.llm_calls,
        tool_composition_state_id=None,
        llm_composition_state_id=None,
        plugin_crash_pending=True,
        required_audit=True,
        required_work=coordinator,
        session_operation_context=context,
    )
    with engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
        assert connection.execute(select(composition_states_table)).all() == saved_states
    tool_rows = [row for row in rows if row.role == "tool" and row.tool_call_id in ("metadata-1", "advisor-2")]
    assert [row.tool_call_id for row in tool_rows] == ["metadata-1", "advisor-2"]
    assert json.loads(tool_rows[-1].content)["success"] is False
    evidence = [
        row.tool_calls[0]["evidence"]
        for row in rows
        if row.tool_calls
        and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
        and row.tool_calls[0]["tool_call_id"] == "advisor-2"
    ]
    assert len(evidence) == 1 and evidence[0]["status"] == "success"
    refusal = json.loads(evidence[0]["result"]["content"])
    assert refusal["error_type"] == "prompt_guard_blocked" and refusal["outbound_call_made"] is False
    assert len(attempts) == 1
    llm_rows = [row for row in rows if row.tool_calls and row.tool_calls[0].get("_kind") == "llm_call_audit"]
    assert len(llm_rows) == 1
    coordinator.assert_completed()


def test_missing_required_mounts_disable_composer_with_safe_existing_reason():
    service = ComposerServiceImpl.for_trained_operator(
        catalog=MagicMock(spec=CatalogService),
        settings=_make_settings(
            composer_prompt_guard_mode="required",
            composer_endpoint_base_url="https://primary.example.test/v1",
            composer_endpoint_api_key="fixture-primary",
            composer_advisor_endpoint_base_url="https://advisor.example.test/v1",
            composer_advisor_endpoint_api_key="fixture-advisor",
        ),
    )
    availability = service.get_availability()
    assert not availability.available
    assert availability.reason == "Composer is unavailable: required prompt guard configuration is missing for composer, advisor."
    assert "example.test" not in availability.reason


@pytest.mark.asyncio
async def test_guard_refusal_has_no_model_audit_or_provider_retry(invocation, monkeypatch):
    engine, _service, owner, coordinator = invocation
    client = _Client(PromptGuardDecision("blocked", "policy_blocked"))
    monkeypatch.setattr(provider_gateway, "build_prompt_guard_mount", lambda _settings: PromptGuardMount(client))
    gateway = provider_gateway.ProviderGateway(
        model="test/model", settings=_make_settings(composer_prompt_guard_mode="required"), endpoint_base_url=None, endpoint_api_key=None
    )
    recorder = BufferingRecorder()
    with prompt_guard_user_scope("Compare columns."), pytest.raises(PromptGuardRefused):
        await gateway._call_llm_with_audit(
            [{"role": "user", "content": "Compare columns."}],
            [],
            timeout=2,
            recorder=recorder,
            provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        )
    assert len(client.requests) == 1
    assert recorder.llm_calls == ()
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_existing_provider_deadline_includes_guard_and_joins_cancelled_check(invocation, monkeypatch):
    engine, _service, owner, coordinator = invocation
    entered = asyncio.Event()
    joined = asyncio.Event()

    class WaitingClient(_Client):
        async def inspect(self, request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                joined.set()

    client = WaitingClient(PromptGuardDecision("allow", "clean"))
    monkeypatch.setattr(provider_gateway, "build_prompt_guard_mount", lambda _settings: PromptGuardMount(client))
    gateway = provider_gateway.ProviderGateway(
        model="test/model", settings=_make_settings(composer_prompt_guard_mode="required"), endpoint_base_url=None, endpoint_api_key=None
    )
    recorder = BufferingRecorder()
    with prompt_guard_user_scope("Compare columns."), pytest.raises(TimeoutError):
        await gateway._call_llm_with_audit(
            [{"role": "user", "content": "Compare columns."}],
            [],
            timeout=0.02,
            recorder=recorder,
            provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        )
    assert entered.is_set() and joined.is_set()
    assert recorder.llm_calls == ()
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        rows = connection.execute(select(chat_messages_table)).all()
    assert rows[0].tool_calls[0]["check"]["disposition"] == "cancelled"
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_debug_mode_is_audited_before_sdk_and_never_changes_wire_kwargs(invocation, monkeypatch):
    engine, _service, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    transmitted = []

    async def completion(**kwargs):
        with engine.connect() as connection:
            rows = connection.execute(select(chat_messages_table)).all()
        assert rows[0].tool_calls[0]["check"]["disposition"] == "debug_bypass"
        transmitted.append(kwargs)
        return litellm.ModelResponse(
            model="test/model", choices=[{"message": {"role": "assistant", "content": "A neutral reply.", "tool_calls": None}}]
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    gateway = provider_gateway.ProviderGateway(
        model="test/model",
        settings=_make_settings(composer_prompt_guard_mode="debug-when-unconfigured"),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )
    with prompt_guard_user_scope("Compare columns."):
        result = await gateway._call_llm_with_audit(
            [{"role": "user", "content": "Compare columns."}], [], timeout=2, recorder=None, provider_custody=custody
        )
    assert result.message.content == "A neutral reply."
    assert transmitted == [
        {
            "model": "test/model",
            "messages": [{"role": "user", "content": "Compare columns."}],
            "num_retries": 0,
            "max_retries": 0,
            "reasoning_effort": "low",
        }
    ]
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_role", ["composer", "advisor"])
async def test_mixed_mounts_preserve_configured_role_size_refusal(invocation, monkeypatch, configured_role):
    from tests.unit.web.composer.test_prompt_guard_admission import _checks

    engine, sessions, owner, coordinator = invocation
    client = _Client(PromptGuardDecision("allow", "clean"))
    configured_mount = PromptGuardMount(client)
    monkeypatch.setattr(
        provider_gateway, "build_prompt_guard_mount", lambda _settings: configured_mount if configured_role == "composer" else None
    )
    monkeypatch.setattr(
        service_module, "build_prompt_guard_mount", lambda _settings: configured_mount if configured_role == "advisor" else None
    )
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=_make_settings(composer_prompt_guard_mode="debug-when-unconfigured"),
        sessions_service=sessions,
        session_engine=engine,
    )
    mounts = {"composer": service.composer_prompt_guard_mount, "advisor": service._advisor_prompt_guard_mount}
    assert mounts[configured_role] is configured_mount
    messages = [{"role": "tool", "content": "x" * 25_000}, {"role": "user", "content": "Keep this draft."}]
    transmitted = []

    async def completion(**kwargs):
        assert _checks(engine)[-1]["disposition"] == "debug_bypass"
        transmitted.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    for role in ("composer", "advisor"):
        custody = owner.mint(ProviderInvocationFamily.PRIMARY if role == "composer" else ProviderInvocationFamily.ADVISOR)
        operation = provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=mounts[role],
            prompt_guard_mode="debug-when-unconfigured",
            prompt_guard_role=role,
            prompt_guard_user_prompt="Keep this draft.",
            model="test/model",
            messages=messages,
        )
        if role == configured_role:
            with pytest.raises(PromptGuardRefused) as refused:
                await operation
            assert refused.value.http_status == 422 and custody.attempt is None
        else:
            assert mounts[role] is None
            await operation
    assert client.requests == []
    assert len(transmitted) == 1 and transmitted[0]["messages"] == messages
    checks = {check["role"]: check for check in _checks(engine)}
    assert set(checks) == {"composer", "advisor"}
    assert (checks[configured_role]["disposition"], checks[configured_role]["reason"]) == ("unavailable", "unsupported_input")
    other_role = "advisor" if configured_role == "composer" else "composer"
    assert (checks[other_role]["disposition"], checks[other_role]["reason"]) == ("debug_bypass", "guard_not_configured")
    with engine.connect() as connection:
        assert len(connection.execute(select(quota_provider_attempts_table)).all()) == 1
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_unconfigured_debug_oversize_still_checks_complete_credentials_before_sdk(invocation, monkeypatch):
    from elspeth.web.credential_guard import CredentialMaterialRefused
    from tests.unit.web.composer.test_prompt_guard_admission import _checks

    engine, _sessions, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    content = "x" * 25_000 + " https://fixture-user:fixture-pass@example.test"

    async def completion(**kwargs):
        raise AssertionError("Credential-bearing input must not enter the SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(CredentialMaterialRefused):
        await provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=None,
            prompt_guard_mode="debug-when-unconfigured",
            prompt_guard_role="composer",
            prompt_guard_user_prompt="Keep this draft.",
            model="test/model",
            messages=[{"role": "tool", "content": content}],
        )
    assert _checks(engine) == [] and custody.attempt is None
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_owned_trust_is_stripped_but_unmarked_system_text_remains_guard_evidence(invocation, monkeypatch):
    _engine, _service, owner, coordinator = invocation
    client = _Client(PromptGuardDecision("blocked", "policy_blocked"))
    with pytest.raises(PromptGuardRefused):
        await provider_gateway._litellm_acompletion(
            provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
            prompt_guard_mount=PromptGuardMount(client),
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            prompt_guard_user_prompt="Compare columns.",
            model="test/model",
            messages=[
                {"role": "system", "content": "OPERATOR_SKILL", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION},
                {"role": "user", "content": "CATALOG_REFERENCE", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.REFERENCE},
                {"role": "system", "content": "UNTRUSTED_SYSTEM_EVIDENCE"},
                {"role": "user", "content": "Compare columns."},
            ],
        )
    documents = "\n".join(client.requests[0].documents)
    assert "UNTRUSTED_SYSTEM_EVIDENCE" in documents
    assert "OPERATOR_SKILL" not in documents and "CATALOG_REFERENCE" not in documents
    assert PROMPT_GUARD_TRUST_KEY not in documents
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_allow_binds_detached_messages_and_tools_despite_caller_mutation(invocation, monkeypatch):
    _engine, _sessions, owner, coordinator = invocation
    messages = [{"role": "user", "content": "Original neutral input."}]
    tools = [{"type": "function", "function": {"name": "neutral_tool", "description": "Original description"}}]

    class MutatingClient(_Client):
        async def inspect(self, request):
            self.requests.append(request)
            messages[0]["content"] = "Changed after inspection"
            tools[0]["function"]["description"] = "Changed after inspection"
            return PromptGuardDecision("allow", "clean")

    client = MutatingClient(PromptGuardDecision("allow", "clean"))
    sent = []

    async def completion(**kwargs):
        sent.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    await provider_gateway._litellm_acompletion(
        provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        prompt_guard_mount=PromptGuardMount(client),
        prompt_guard_mode="required",
        prompt_guard_role="composer",
        prompt_guard_user_prompt="Original neutral input.",
        model="test/model",
        messages=messages,
        tools=tools,
    )
    assert sent[0]["messages"] == [{"role": "user", "content": "Original neutral input."}]
    assert sent[0]["tools"][0]["function"]["description"] == "Original description"
    assert messages[0]["content"] == "Changed after inspection"
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("hatch", [False, True])
async def test_real_planner_selects_independent_mount_even_with_identical_model_names(tmp_path, tool_context, hatch):
    from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion

    composer = _Client(PromptGuardDecision("allow", "clean") if hatch else PromptGuardDecision("blocked", "policy_blocked"))
    advisor = _Client(PromptGuardDecision("blocked", "policy_blocked"))
    completion = _ScriptedCompletion(_response(("list_sources", {})), _response(("list_sinks", {})))
    recorder = BufferingRecorder()
    with pytest.raises(PromptGuardRefused) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            model_overrides={
                "max_discovery_turns": 1,
                "model_identifier": "anthropic/claude-planner",
                "escape_hatch_model": "anthropic/claude-planner",
                "escape_hatch_provider": "test-provider",
                "prompt_guard_mode": "required",
                "prompt_guard_mount": PromptGuardMount(composer),
                "escape_hatch_prompt_guard_mount": PromptGuardMount(advisor),
            },
        )
    assert len(completion.requests) == (2 if hatch else 0)
    assert len(advisor.requests) == int(hatch)
    assert all(request.role == "composer" for request in composer.requests)
    assert all(request.role == "advisor" for request in advisor.requests)
    assert caught.value.disposition == "blocked"
    assert len(recorder.llm_calls) == (2 if hatch else 0)


@pytest.mark.asyncio
async def test_captured_planner_catalog_reference_preserves_complete_untrusted_graph_evidence(tmp_path, tool_context, record_property):
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.planner_authoring_aids import build_schema_contract_evidence
    from elspeth.web.composer.prompt_guard_rendering import json_content_parts
    from elspeth.web.composer.state import NodeSpec
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    graph_plugin = "graph_derived_omission_canary"
    current = _empty_state().with_node(
        NodeSpec(
            id="prior_node",
            node_type="transform",
            plugin=graph_plugin,
            input="rows",
            on_success="out",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    loaded = frozenset({("transform", "passthrough")})
    expected_schema, _ = build_schema_contract_evidence(policy, schemas_loaded=loaded, referenced={("transform", graph_plugin)})
    client = _Client(PromptGuardDecision("allow", "clean"))
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        current_state=current,
        schemas_loaded=loaded,
        policy_override=(policy, snapshot),
        model_overrides={"prompt_guard_mount": PromptGuardMount(client), "prompt_guard_mode": "required"},
    )
    assert len(client.requests) == len(completion.requests) == 1
    wire = completion.requests[0]
    parsed = [json.loads(message["content"]) for message in wire["messages"] if message["role"] == "user"]
    categories = [sorted(payload) for payload in parsed]
    record_property("wire_user_message_chars", [len(message["content"]) for message in wire["messages"] if message["role"] == "user"])
    record_property("guard_document_chars", [len(document) for document in client.requests[0].documents])
    record_property("wire_user_field_categories", categories)
    references = [payload for payload in parsed if "authoring_aids" in payload and "intent" not in payload]
    assert len(references) == 1
    reference = references[0]
    evidence = next(payload for payload in parsed if "intent" in payload)
    assert {**reference["schema_contract_evidence"], **evidence["schema_contract_evidence"]} == expected_schema
    assert reference["schema_contract_evidence"]["schemas"][0]["plugin_id"] == "transform/passthrough"
    assert "authoring_aids" not in "\n".join(client.requests[0].documents)
    assert graph_plugin in "\n".join(client.requests[0].documents)
    assert max(map(len, client.requests[0].documents)) < 10_000
    assert all(PROMPT_GUARD_TRUST_KEY not in message for message in wire["messages"])
    hashed = {
        "role": "composer",
        "policy_fingerprint": client.policy_fingerprint,
        "enforce_azure_character_limits": True,
        "user_prompt": "Build the requested pipeline.",
        "messages": wire["messages"],
        "tools": wire["tools"],
        "trusted_message_indexes": [0, 1],
        "content_parts": [
            None,
            None,
            [
                {"text": part.text, "trust": None, "inspection_text": part.inspection_text}
                for part in json_content_parts(wire["messages"][2]["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ],
        ],
        "tool_argument_parts": [None, None, None],
    }
    assert (
        client.requests[0].request_hash
        == sha256(json.dumps(hashed, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    )
