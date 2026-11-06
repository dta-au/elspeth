"""U-22 F4: a graph outside the supported envelope is a classified, value-free refusal.

Before U-22 an admitted graph whose complete projection exceeds the 1 MiB
bounded-JSON envelope raised an unclassified ``CompositionViewUnsupported``
(a ``ValueError``) from every preparation owner except the discovery tool, so a
compose turn surfaced it as an owned 500. Each preparation owner must instead
raise ``ComposerContextUnsupported``: HTTP 422, ``composer_context_unsupported``,
a closed reason code and no graph content, before any provider call.

The oversized fixture follows the U-08 red-team's shape: five nodes, each carrying a
~234 KB authored note, each below the 262,144-byte per-string credential limit, so
the entry credential guard admits it while the aggregate exceeds the 1 MiB view
envelope. The bytes sit in an ordinary option, not a Jinja template, so no
earlier template-size check intervenes. These are owner-level tests; route
reachability is separate.
"""

from __future__ import annotations

import json
from uuid import UUID

import litellm
import pytest

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer._compose_loop_carriers import _AdvisorFirstPartyFailure
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.content_views import CompositionViewUnsupported
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel
from elspeth.web.credential_guard import require_no_credential_material_in_state
from tests.unit.web.composer.test_prompt_guard_wiring import _Client, _guarded_service

pytest_plugins = ("tests.unit.web.composer.test_prompt_guard_wiring",)

_REQUEST = "Review the complete graph."


def _graph(note_bytes: int) -> CompositionState:
    note = ("Reviewer note: " + "lorem ipsum dolor " * (note_bytes // 18 + 1))[:note_bytes]
    nodes = tuple(
        NodeSpec(
            id=f"n{index}",
            node_type="transform",
            plugin="passthrough",
            input="rows",
            on_success="rated",
            on_error=None,
            options={"schema": {"mode": "observed"}, "note": note},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        for index in range(5)
    )
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={"path": "input.csv"}, on_validation_failure="discard"),
        nodes=nodes,
        edges=(),
        outputs=(OutputSpec(name="rated", plugin="csv", options={"path": "out.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=2,
    )


_OVERSIZED = 234_000  # 5 x 234 KB > 1 MiB aggregate; each below the credential string limit
_SUPPORTED = 3_000  # matched control: long values, well inside the envelope


def test_oversized_fixture_is_admitted_by_the_credential_guard_and_refused_by_the_view() -> None:
    """Precondition: the fixture reaches the raw view envelope, not an earlier refusal."""
    from elspeth.web.composer.content_views import build_bounded_composition_view
    from elspeth.web.composer.prompts import project_composition_content

    state = _graph(_OVERSIZED)
    require_no_credential_material_in_state(state, surface="composer_state_before_provider")
    scope = ComposerContentScope(UUID(int=7), composition_content_hash(state))
    with pytest.raises(CompositionViewUnsupported):
        build_bounded_composition_view(scope, project_composition_content(state))
    supported = _graph(_SUPPORTED)
    build_bounded_composition_view(
        ComposerContentScope(UUID(int=7), composition_content_hash(supported)), project_composition_content(supported)
    )


def _assert_classified(refusal: ComposerContextUnsupported) -> None:
    assert refusal.http_status == 422
    assert refusal.reason == "graph_resource_limit"
    payload = refusal.to_payload()
    assert payload["error_type"] == "composer_context_unsupported"
    assert payload["failure_code"] == "context_unsupported"
    assert payload["reason"] == "graph_resource_limit"
    assert "lorem ipsum" not in json.dumps(payload) and "lorem ipsum" not in str(refusal)


def test_primary_message_owner_classifies_an_unsupported_graph(invocation, monkeypatch) -> None:
    _engine, _sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch)
    supported = service._build_messages(
        [], _graph(_SUPPORTED), _REQUEST, session_id=operation.fence.session_id, user_id="alice", session_operation_context=operation
    )
    assert supported
    with pytest.raises(ComposerContextUnsupported) as refused:
        service._build_messages(
            [], _graph(_OVERSIZED), _REQUEST, session_id=operation.fence.session_id, user_id="alice", session_operation_context=operation
        )
    _assert_classified(refused.value)
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
async def test_advisor_checkpoint_classifies_an_unsupported_graph_before_dispatch(invocation, monkeypatch, phase) -> None:
    _engine, _sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    client = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        raise AssertionError("An unsupported graph must not reach the SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_REQUEST), pytest.raises(ComposerContextUnsupported) as refused:
        await service._advisor_checkpoint._run_advisor_checkpoint(
            phase=phase,
            state=_graph(_OVERSIZED),
            session_id=operation.fence.session_id,
            recorder=BufferingRecorder(),
            user_message=_REQUEST,
            session_operation_context=operation,
            provider_owner=owner,
        )
    _assert_classified(refused.value)
    assert calls == [] and client.requests == []
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_manual_advisor_carries_the_classified_refusal(invocation, monkeypatch) -> None:
    _engine, _sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    client = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        raise AssertionError("An unsupported graph must not reach the SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_REQUEST):
        result = await service._advisor_checkpoint._call_advisor_for_tool(
            RequestAdvisorHintArgumentsModel(
                trigger="proactive_security_safety", problem_summary=_REQUEST, recent_errors=[], attempted_actions=[]
            ),
            recorder=BufferingRecorder(),
            provider_owner=owner,
            current_state=_graph(_OVERSIZED),
            session_id=operation.fence.session_id,
            session_operation_context=operation,
        )
    assert isinstance(result, _AdvisorFirstPartyFailure)
    assert isinstance(result.original_exc, ComposerContextUnsupported)
    _assert_classified(result.original_exc)
    assert calls == [] and client.requests == []
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_manual_advisor_dispatch_records_a_semantic_refusal_not_a_plugin_crash(invocation, monkeypatch) -> None:
    """Actual request_advisor_hint dispatch: the context refusal is a semantic tool outcome.

    Fault injection: the Advisor's own graph fit raises the carrier. An
    oversized starting graph is refused earlier, at primary preparation, so
    this proves the tool_batch arm, not natural HTTP reachability of the
    manual owner. The tool row must be a completed semantic refusal (never
    PLUGIN_CRASH), no Advisor call may be made, and the turn must end with the
    carrier itself so the route can settle the permanent 422.
    """
    from sqlalchemy import select

    from elspeth.web.composer import advisor_checkpoint
    from elspeth.web.sessions.models import chat_messages_table
    from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort
    from tests.unit.web.composer._helpers import _empty_state

    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch, advisor_client=_Client(PromptGuardDecision("allow", "clean")))

    def refuse_graph(*_args, **_kwargs):
        raise ComposerContextUnsupported("graph_resource_limit")

    monkeypatch.setattr(advisor_checkpoint._advisor_context, "fit_advisor_checkpoint_summary", refuse_graph)
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        assert len(sdk_calls) == 1, "the refused Advisor must not dispatch and the turn must end"
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
                                "id": "advisor-1",
                                "type": "function",
                                "function": {
                                    "name": "request_advisor_hint",
                                    "arguments": json.dumps(
                                        {
                                            "trigger": "proactive_security_safety",
                                            "problem_summary": "Review the draft.",
                                            "recent_errors": [],
                                            "attempted_actions": [],
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(ComposerContextUnsupported) as caught:
        await service.compose(
            "Review the draft.",
            [],
            _empty_state(),
            session_id=context.fence.session_id,
            user_id="alice",
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert len(sdk_calls) == 1  # the primary only; no Advisor call
    _assert_classified(caught.value)
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
    tool_rows = [row for row in rows if row.role == "tool" and row.tool_call_id == "advisor-1"]
    assert len(tool_rows) == 1 and json.loads(tool_rows[0].content)["success"] is False
    evidence = [
        row.tool_calls[0]["evidence"]
        for row in rows
        if row.tool_calls
        and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
        and row.tool_calls[0]["tool_call_id"] == "advisor-1"
    ]
    assert len(evidence) == 1 and evidence[0]["status"] == "success", "a semantic refusal, never PLUGIN_CRASH"
    refusal = json.loads(evidence[0]["result"]["content"])
    assert refusal["error_type"] == "composer_context_unsupported" and refusal["outbound_call_made"] is False
    assert refusal["failure_code"] == "context_unsupported" and refusal["reason"] == "graph_resource_limit"
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_early_refusal_after_mutation_keeps_the_saved_effect_and_carries_evidence(monkeypatch) -> None:
    """EARLY after a completed mutation: the refusal carries evidence; the saved effect stands.

    Fault injection again (the EARLY fit raises the carrier). The completed
    set_source mutation must remain saved and audited; the turn must end with
    an evidence-bearing ComposerContextUnsupported chained from the original,
    and the Advisor SDK must never be called.
    """
    from typing import Any
    from unittest.mock import AsyncMock, patch
    from uuid import UUID

    from sqlalchemy import select

    from elspeth.web.composer import advisor_checkpoint
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.state import SourceSpec
    from elspeth.web.composer.tools import ToolResult
    from elspeth.web.sessions.models import chat_messages_table
    from tests.helpers.composer_sdk import ComposerSDKScript
    from tests.unit.web.composer._helpers import _empty_state, _make_settings, _mock_catalog
    from tests.unit.web.composer.test_service import _REAL_RUN_ADVISOR_CHECKPOINT, _composer_service_with_session, _make_raw_llm_response

    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
    sessions = service._require_sessions_service()
    await sessions.update_composer_preferences(UUID(session_id), trust_mode="auto_commit", density_default="high", actor="user:test")
    monkeypatch.setattr(
        service._advisor_checkpoint,
        "_run_advisor_checkpoint",
        _REAL_RUN_ADVISOR_CHECKPOINT.__get__(service._advisor_checkpoint, AdvisorCheckpointOwner),
    )

    def refuse_graph(*_args, **_kwargs):
        raise ComposerContextUnsupported("graph_resource_limit")

    monkeypatch.setattr(advisor_checkpoint._advisor_context, "fit_advisor_checkpoint_summary", refuse_graph)
    source_arguments = {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": "/data/input.csv", "schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
    }

    def mutate_source(
        _tool_name: str, _arguments: dict[str, Any], current_state: CompositionState, _catalog: Any, **_kwargs: Any
    ) -> ToolResult:
        updated = current_state.with_source(SourceSpec(**source_arguments))
        return ToolResult(success=True, updated_state=updated, validation=updated.validate(), affected_nodes=("source",), data=None)

    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[_make_raw_llm_response(tool_calls=[{"id": "source-1", "name": "set_source", "arguments": source_arguments}])],
    )
    with (
        patch("litellm.acompletion", new=script),
        patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", new_callable=AsyncMock) as advisor,
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=mutate_source),
        pytest.raises(ComposerContextUnsupported) as raised,
    ):
        await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)

    _assert_classified(raised.value)
    assert isinstance(raised.value.__cause__, ComposerContextUnsupported), "chained from the original refusal"
    assert raised.value.llm_calls, "the actual primary call travels with the refusal"
    advisor.assert_not_awaited()
    assert service._session_engine is not None
    with service._session_engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.role, chat_messages_table.c.tool_call_id, chat_messages_table.c.composition_state_id).where(
                chat_messages_table.c.session_id == session_id
            )
        ).all()
    tool_rows = [row for row in rows if row.role == "tool"]
    assert [row.tool_call_id for row in tool_rows] == ["source-1"] and tool_rows[0].composition_state_id is not None
    current = await sessions.get_current_state(UUID(session_id))
    assert current is not None and current.sources["source"]["plugin"] == "csv"


@pytest.mark.asyncio
async def test_manual_advisor_preparation_refusal_spends_no_advisor_allowance(invocation, monkeypatch) -> None:
    """U-22 §6: the manual allowance is spent only by an actual Advisor dispatch.

    ``advisor_calls_used`` was debited before preparation, so a request that
    never left the process still consumed a model attempt. Actual
    request_advisor_hint dispatch with a coverage refusal at preparation (fault
    injection at the fit) must report budget_used 0 and make no Advisor call; the
    turn settles with its permanent refusal and retained primary evidence.
    """
    from sqlalchemy import select

    from elspeth.web.composer import advisor_checkpoint
    from elspeth.web.sessions.models import chat_messages_table
    from tests.unit.web.composer._helpers import _empty_state

    engine, _sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    advisor_guard = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, advisor_client=advisor_guard)

    def refuse_coverage(*_args, **_kwargs):
        raise ComposerContextUnsupported("graph_coverage_unavailable")

    monkeypatch.setattr(advisor_checkpoint._advisor_context, "fit_advisor_checkpoint_summary", refuse_coverage)
    hint = {
        "id": "advisor-1",
        "type": "function",
        "function": {
            "name": "request_advisor_hint",
            "arguments": json.dumps(
                {
                    "trigger": "proactive_security_safety",
                    "problem_summary": "Review the draft.",
                    "recent_errors": [],
                    "attempted_actions": [],
                }
            ),
        },
    }
    replies = [
        {"role": "assistant", "content": None, "tool_calls": [hint]},
        {"role": "assistant", "content": "The draft needs no further change."},
    ]
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        message = replies.pop(0) if replies else {"role": "assistant", "content": "Done."}
        return litellm.ModelResponse(
            model=kwargs["model"], usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}, choices=[{"message": message}]
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(ComposerContextUnsupported) as caught:
        await service.compose(
            "Review the draft.",
            [],
            _empty_state(),
            session_id=context.fence.session_id,
            user_id="alice",
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert caught.value.reason == "graph_coverage_unavailable"
    assert len(sdk_calls) == 1, "only the primary dispatched before the preparation refusal"
    assert len(caught.value.llm_calls) == 1, "the refusal retains the prior physical call"
    assert advisor_guard.requests == [], "the refused Advisor request was never inspected or dispatched"
    with engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
    evidence = [
        row.tool_calls[0]["evidence"]
        for row in rows
        if row.tool_calls
        and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
        and row.tool_calls[0]["tool_call_id"] == "advisor-1"
    ]
    assert len(evidence) == 1
    payload = json.loads(evidence[0]["result"]["content"])
    assert payload["reason"] == "graph_coverage_unavailable"
    assert payload["error_type"] == "composer_context_unsupported"
    assert payload["budget_used"] == 0, "a preparation refusal spends no Advisor allowance"
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_natural_oversized_graph_refusal_does_not_publish_a_setup_failure(invocation, monkeypatch) -> None:
    """P10 progress-only carry: exercise actual preparation, without a request-byte cap."""
    _engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    client = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, composer_client=client)
    user = await sessions.add_message(
        UUID(context.fence.session_id),
        "user",
        _REQUEST,
        writer_principal="route_user_message",
        session_operation_context=context,
    )
    sdk_calls = []
    events = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        raise AssertionError("Unsupported graph preparation must precede the SDK")

    async def progress(event):
        events.append(event)

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(ComposerContextUnsupported) as caught:
        await service.compose(
            _REQUEST,
            [],
            _graph(_OVERSIZED),
            session_id=context.fence.session_id,
            user_message_id=str(user.id),
            user_id="alice",
            progress=progress,
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    _assert_classified(caught.value)
    assert sdk_calls == [] and client.requests == []
    assert caught.value.llm_calls == (), "no physical call or guard verdict is invented for preparation refusal"
    assert all(event.reason != "service_setup_failed" for event in events)
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("advisor_path", ["manual", "early"])
@pytest.mark.parametrize("verification_fails", [False, True], ids=["verified-effect", "effect-integrity-failure"])
@pytest.mark.parametrize("cancel_requested", [False, True], ids=["no-cancel", "deferred-cancel"])
async def test_service_context_refusal_preserves_actual_committed_effect_and_cancellation_precedence(
    invocation, monkeypatch, advisor_path: str, verification_fails: bool, cancel_requested: bool
) -> None:
    """Inject refusal at the Advisor owner while real P3/P4 work completes.

    Both the metadata mutation and its checkpoint use production ownership.
    For the failure control only the pure post-commit comparison input changes;
    the real effect verifier then raises its original AuditIntegrityError. A
    caller cancel lands while the shielded Advisor stage is still running and
    must emerge after that same P4 checkpoint, retaining both original causes.
    """
    import asyncio

    from sqlalchemy import select

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.composer import service as service_module
    from elspeth.web.composer.turn_effects import TurnEvidence
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
    from elspeth.web.sessions.models import chat_messages_table, composition_states_table, quota_provider_attempts_table
    from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort
    from tests.unit.web.composer._helpers import _empty_state

    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    primary_guard = _Client(PromptGuardDecision("allow", "clean"))
    advisor_guard = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(invocation, monkeypatch, composer_client=primary_guard, advisor_client=advisor_guard)
    await sessions.update_composer_preferences(
        UUID(context.fence.session_id), trust_mode="auto_commit", density_default="high", actor="user:alice"
    )
    request = "Set the pipeline name to Saved before refusal, then review it."
    user = await sessions.add_message(
        UUID(context.fence.session_id), "user", request, writer_principal="route_user_message", session_operation_context=context
    )
    original_refusal = ComposerContextUnsupported("graph_resource_limit")
    original_cancel_cause = RuntimeError("PRIVATE prior cancellation cause")
    entered = asyncio.Event()
    release = asyncio.Event()
    sdk_calls = []
    recorders = []
    cancellations = []
    integrity_failures = []
    committed_snapshots = []
    evidence_owners = []
    actual_accept = TurnEvidence.accept_checkpoint
    actual_await_tool_turn = service_module._await_tool_turn_with_deferred_cancellation
    tool_calls = [
        {
            "id": "saved-context-metadata",
            "type": "function",
            "function": {"name": "set_metadata", "arguments": json.dumps({"patch": {"name": "Saved before refusal"}})},
        }
    ]
    if advisor_path == "manual":
        tool_calls.append(
            {
                "id": "refused-context-advisor",
                "type": "function",
                "function": {
                    "name": "request_advisor_hint",
                    "arguments": json.dumps(
                        {
                            "trigger": "proactive_security_safety",
                            "problem_summary": "Review the pipeline name.",
                            "recent_errors": [],
                            "attempted_actions": ["set_metadata"],
                        }
                    ),
                },
            }
        )
    expected_tool_ids = tuple(call["id"] for call in tool_calls)

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        assert len(sdk_calls) == 1, "no Advisor or later primary is dispatched after preparation refusal"
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            choices=[{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}],
        )

    async def refuse_advisor(*_args, **kwargs):
        recorders.append(kwargs["recorder"])
        entered.set()
        if cancel_requested:
            await release.wait()
        raise original_refusal

    async def observe_actual_deferred_cancel(awaitable, *, cancellation_requested):
        outcome, cancellation = await actual_await_tool_turn(awaitable, cancellation_requested=cancellation_requested)
        if cancellation is not None:
            assert cancellation.__cause__ is None
            cancellation.__cause__ = original_cancel_cause
            cancellations.append(cancellation)
        return outcome, cancellation

    def verify_actual_committed_checkpoint(evidence, witness, *, expected_state, tool_call_ids, assistant_message_id):
        with engine.connect() as connection:
            states = connection.execute(select(composition_states_table)).all()
            conversation = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        assert len(states) == 1 and states[0].id == str(witness.state.id)
        assert [row.role for row in conversation] == ["assistant", *["tool"] * len(expected_tool_ids)]
        assert conversation[0].id == assistant_message_id
        assert tuple(row.tool_call_id for row in conversation[1:]) == expected_tool_ids
        assert tool_call_ids == expected_tool_ids
        committed_snapshots.append((states, conversation, assistant_message_id))
        evidence_owners.append(evidence)
        try:
            actual_accept(
                evidence,
                witness,
                expected_state=expected_state.with_metadata({"name": "PRIVATE comparison mismatch"})
                if verification_fails
                else expected_state,
                tool_call_ids=tool_call_ids,
                assistant_message_id=assistant_message_id,
            )
        except AuditIntegrityError as failure:
            integrity_failures.append(failure)
            raise

    def exception_links(error):
        linked = [error]
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                linked.extend(exception_links(child))
        if error.__cause__ is not None:
            linked.extend(exception_links(error.__cause__))
        return linked

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(
        service._advisor_checkpoint,
        "_call_advisor_with_audit" if advisor_path == "manual" else "_maybe_run_early_checkpoint",
        refuse_advisor,
    )
    monkeypatch.setattr(TurnEvidence, "accept_checkpoint", verify_actual_committed_checkpoint)
    monkeypatch.setattr(service_module, "_await_tool_turn_with_deferred_cancellation", observe_actual_deferred_cancel)
    task = asyncio.create_task(
        service.compose(
            request,
            [],
            _empty_state(),
            session_id=context.fence.session_id,
            user_message_id=str(user.id),
            user_id="alice",
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    )
    if cancel_requested:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel("controlled caller cancellation")
        await asyncio.sleep(0)
        release.set()
    expected_error = (
        asyncio.CancelledError if cancel_requested else BaseExceptionGroup if verification_fails else ComposerContextUnsupported
    )
    with pytest.raises(expected_error) as caught:
        await task
    linked = exception_links(caught.value)
    assert any(error is original_refusal for error in linked), "preserve the exact original preparation refusal"
    assert len(committed_snapshots) == len(evidence_owners) == 1
    assert len(integrity_failures) == int(verification_fails)
    assert len(sdk_calls) == len(primary_guard.requests) == 1
    assert advisor_guard.requests == [], "no guard decision is invented for the undispatched Advisor"
    assert evidence_owners[0].effects.saved_revision_count == 1
    assert evidence_owners[0].effects.verification == ("unavailable" if verification_fails else "verified")
    if cancel_requested:
        assert len(cancellations) == 1 and caught.value is cancellations[0]
        assert caught.value.args == ("controlled caller cancellation",)
        assert any(error is original_cancel_cause for error in linked)
    else:
        assert cancellations == []
    if verification_fails:
        assert any(error is integrity_failures[0] for error in linked)
        assert integrity_failures[0].failed_turn.assistant_message_id == committed_snapshots[0][2]
        assert integrity_failures[0].failed_turn.tool_responses_persisted == len(expected_tool_ids)
        projection = project_composer_operation_error(caught.value, request_id="context-effect-request")
        assert projection.http_status == 500 and projection.error_type == "audit_integrity_error"
    elif not cancel_requested:
        projection = project_composer_operation_error(caught.value, request_id="context-effect-request")
        assert projection.http_status == 422 and projection.error_type == "composer_context_unsupported"
    if advisor_path == "manual":
        invocation_record = next(item for item in recorders[0].invocations if item.tool_name == "request_advisor_hint")
        assert invocation_record.status.value == "success"
        assert invocation_record.result_canonical is not None
        result = json.loads(invocation_record.result_canonical)
        assert result["error_type"] == "composer_context_unsupported"
        assert result["outbound_call_made"] is False and result["budget_used"] == 0
    # P4's assistant/tool checkpoint is already durable. Drain the remaining
    # actual primary audit through the route owner and prove no effect replay.
    assert len(recorders) == 1 and len(recorders[0].llm_calls) == 1
    await _persist_turn_audit_cohort(
        sessions,
        UUID(context.fence.session_id),
        (),
        recorders[0].llm_calls,
        tool_composition_state_id=None,
        llm_composition_state_id=None,
        plugin_crash_pending=True,
        required_audit=True,
        required_work=coordinator,
        session_operation_context=context,
    )
    coordinator.assert_completed()
    with engine.connect() as connection:
        states_after = connection.execute(select(composition_states_table)).all()
        rows_after = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
    assert states_after == committed_snapshots[0][0]
    assert [row for row in rows_after if row.role in ("assistant", "tool")] == committed_snapshots[0][1]
    assert len(attempts) == 1 and attempts[0].attempt_id == recorders[0].llm_calls[0].call_id
    current = await sessions.get_current_state(UUID(context.fence.session_id))
    assert current is not None and current.metadata_["name"] == "Saved before refusal"
