"""Ordinary visible evidence survives the actual joined P4 storage boundary."""

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


@pytest.fixture
def visible_writer(tmp_path, monkeypatch):
    import litellm

    async def no_provider(**_kwargs):
        raise AssertionError("Storage controls must never call an external provider")

    monkeypatch.setattr(litellm, "acompletion", no_provider)
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice",
        title="Visible evidence",
        auth_provider_type="local",
        owner_instance_id="owner",
        lease_seconds=300,
    )
    holder = {
        "context": authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="owner",
            lease_seconds=300,
        )
    }
    yield service, session, authority, holder
    if holder["context"] is not None:
        authority.release(holder["context"])
    engine.dispose()


def _kwargs(session, context):
    original = ' { "content": "ordinary authored text" } '
    effective = '{"content":"ordinary authored text"}'
    selected = json.dumps({"success": True, "data": {"rule": "A generic rule. " * 2000 + "DURABLE_TAIL_CANARY"}})
    display_arguments, display_result = '{"content":"[redacted]"}', "{}"
    companion = VisibleToolEvent.capture(
        context=context,
        request_id="actual-request",
        tool_call_id="visible-call",
        tool_name="create_blob",
        received_arguments=original,
        effective_arguments=effective,
        selected_result=selected,
        display_arguments=display_arguments,
        display_result=display_result,
    )
    private = build_private_tool_evidence(
        session_id=str(session.id),
        operation_id=context.fence.operation_id,
        operation_epoch=context.fence.operation_epoch,
        tool_call_id="visible-call",
        tool_name="create_blob",
        status="success",
        arguments_canonical=effective,
        result_canonical=canonical_json(json.loads(selected)),
        raw_arguments=original,
    )
    return (
        {
            "session_id": str(session.id),
            "assistant_content": "A visible tool action",
            "raw_content": None,
            "redacted_assistant_tool_calls": (
                {"id": "visible-call", "type": "function", "function": {"name": "create_blob", "arguments": display_arguments}},
            ),
            "redacted_tool_rows": (RedactedToolRow("visible-call", display_result, None, companion),),
            "private_tool_evidence": (private,),
            "parent_composition_state_id": None,
            "expected_current_state_id": None,
            "writer_principal": "compose_loop",
            "plugin_crash_pending": False,
            "session_operation_context": context,
        },
        original,
        effective,
        selected,
    )


async def _history(service, session, context):
    current = await service.add_message(
        session.id, "user", "Continue", writer_principal="route_user_message", session_operation_context=context
    )
    rows = await service.get_messages(session.id, limit=None, include_private_evidence=False)
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(session.id),
        actor_user_id="alice",
        current_request_id=str(current.id),
        request="Continue",
        records=rows,
    )
    return rows, json.loads(AdvisorTranscriptOwner(request="Continue", history=history).snapshot().provider_json)


@pytest.mark.asyncio
async def test_actual_persist_reload_keeps_original_effective_and_long_selected_result(visible_writer):
    service, session, _authority, holder = visible_writer
    context = holder["context"]
    kwargs, original, effective, selected = _kwargs(session, context)
    outcome = await service.persist_compose_turn_async(**kwargs)
    rows, value = await _history(service, session, context)
    assistant, tool = value["events"][:2]
    call = assistant["tool_calls"][0]
    assert call["received_arguments"] == original and call["effective_arguments"] == effective
    assert tool["content"] == selected and "DURABLE_TAIL_CANARY" in tool["content"]
    assert tool["history_availability"] == "ordinary_visible_event_secret_redacted"
    assert str(rows[1].parent_assistant_id) == outcome.assistant_id
    assert rows[1].operation_id is None  # User-ingress provenance is not repurposed.
    assert rows[1].tool_calls[0]["parent_assistant_id"] == outcome.assistant_id
    assert rows[1].tool_calls[0]["operation_id"] == context.fence.operation_id
    assert not any(message["role"] in {"tool", "audit"} for message in _composer_chat_history(rows))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "session_id",
        "operation_id",
        "operation_epoch",
        "tool_call_id",
        "tool_name",
        "display_arguments_hash",
        "display_result_hash",
        "payload_hash",
        "parent_assistant_id",
    ],
)
async def test_companion_mismatch_refuses_before_any_rows_are_written(visible_writer, field):
    service, session, _authority, holder = visible_writer
    kwargs, *_ = _kwargs(session, holder["context"])
    row = kwargs["redacted_tool_rows"][0]
    replacement = row.visible_event.operation_epoch + 1 if field == "operation_epoch" else "different-value"
    kwargs["redacted_tool_rows"] = (replace(row, visible_event=replace(row.visible_event, **{field: replacement})),)
    with pytest.raises(AuditIntegrityError):
        await service.persist_compose_turn_async(**kwargs)
    assert await service.get_messages(session.id, limit=None) == []


@pytest.mark.asyncio
async def test_visible_companion_rolls_back_with_its_actual_tool_cohort(visible_writer, monkeypatch):
    service, session, _authority, holder = visible_writer
    kwargs, *_ = _kwargs(session, holder["context"])
    insert = service._insert_chat_message

    def fail_sibling(conn, **arguments):
        if arguments["role"] == "audit":
            raise AuditIntegrityError("controlled sibling failure")
        return insert(conn, **arguments)

    monkeypatch.setattr(service, "_insert_chat_message", fail_sibling)
    with pytest.raises(AuditIntegrityError, match="controlled sibling"):
        await service.persist_compose_turn_async(**kwargs)
    assert await service.get_messages(session.id, limit=None) == []


def test_capture_redacts_whole_assignment_and_has_no_authored_uuid_key_exemption():
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    secret = '{"api_key":"SYNTHETIC_DURABLE_SECRET_CANARY"}'  # secret-scan: allow-this-line
    captured = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="call",
        tool_name="example",
        received_arguments=secret,
        effective_arguments=secret,
        selected_result=secret,
        display_arguments="{}",
        display_result="{}",
    )
    assert "SYNTHETIC_DURABLE_SECRET_CANARY" not in captured.payload_json
    assert captured.payload_hash == hashlib.sha256(captured.payload_json.encode()).hexdigest()
    identity = "12345678-1234-1234-1234-123456789012"
    for key in ("blob_id", "ordinary_key"):
        value = json.dumps({key: identity})
        projected = VisibleToolEvent.capture(
            context=CONTEXT,
            request_id="request",
            tool_call_id="call",
            tool_name="example",
            received_arguments=value,
            effective_arguments=value,
            selected_result=value,
            display_arguments="{}",
            display_result="{}",
        )
        assert identity not in projected.payload_json


@pytest.mark.asyncio
async def test_actual_fork_retains_source_binding_as_explicit_gap_not_child_authority(visible_writer):
    from tests.unit.web.sessions.test_fork import _fork_session

    service, session, authority, holder = visible_writer
    context = holder["context"]
    kwargs, *_ = _kwargs(session, context)
    await service.persist_compose_turn_async(**kwargs)
    target = await service.add_message(
        session.id, "user", "Fork from here", writer_principal="route_user_message", session_operation_context=context
    )
    authority.release(context)
    holder["context"] = None
    child, rows, _state = await _fork_session(
        service,
        source_session_id=session.id,
        fork_message_id=target.id,
        new_message_content="Continue copied draft",
        user_id="alice",
        auth_provider_type="local",
    )
    current = rows[-1]
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(child.id),
        actor_user_id="alice",
        current_request_id=str(current.id),
        request=current.content,
        records=rows,
    )
    value = AdvisorTranscriptOwner(request=current.content, history=history).snapshot().provider_json
    assert "unavailable_original_session_or_operation_binding" in value
    assert "DURABLE_TAIL_CANARY" not in value


@pytest.mark.asyncio
async def test_actual_planner_tools_and_public_call_gaps_survive_joined_cohort_reload(visible_writer, tmp_path, tool_context):
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.pipeline_planner import PlannerOriginatingMessage
    from elspeth.web.composer.planning_application import PlanningApplication
    from tests.unit.web.composer.test_llm_call_audit_summary import _PlannerAuditHost
    from tests.unit.web.composer.test_pipeline_planner import _custody, _inline_pipeline, _plan, _response, _ScriptedCompletion

    service, session, authority, holder = visible_writer
    context = holder["context"]
    intent = "Build a generic inline pipeline."
    human = await service.add_message(session.id, "user", intent, writer_principal="route_user_message", session_operation_context=context)
    recorder = BufferingRecorder()
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("emit_pipeline_proposal", {"pipeline": _inline_pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        intent=intent,
        originating_message=PlannerOriginatingMessage(
            session_id=str(session.id), message_id=str(human.id), user_id="alice", content=intent
        ),
        custody_config=replace(
            _custody(tmp_path), session_engine=service._engine, session_operation_context=context, session_operation_authority=authority
        ),
    )
    assert len(recorder.visible_tool_events) == 1
    invocation, companion = recorder.visible_tool_events[0]
    assert invocation is recorder.invocations[0]
    await PlanningApplication._persist_pipeline_planner_audit(
        _PlannerAuditHost(service),
        session_id=session.id,
        current_state_id=None,
        llm_calls=recorder.llm_calls,
        planner_attempts=recorder.planner_attempts,
        invocations=recorder.invocations,
        withheld_replies=recorder.withheld_replies,
        private_candidates=recorder.private_candidates,
        session_operation_context=context,
        visible_tool_events=recorder.visible_tool_events,
    )
    rows, value = await _history(service, session, context)
    tools = [event for event in value["events"] if event.get("history_availability") == "ordinary_visible_event_secret_redacted"]
    assert len(tools) == 1
    restored = tools[0]
    assert restored["content"] == json.loads(companion.payload_json)["selected_result"]
    assert restored["legacy_invocations"][0]["received_arguments"] == json.loads(companion.payload_json)["received_arguments"]
    gaps = [event["visible_response_coverage_gap"] for event in value["events"] if "visible_response_coverage_gap" in event]
    assert len(gaps) == len(recorder.llm_calls) == 2
    assert {gap["provider_call_id"] for gap in gaps} == {call.call_id for call in recorder.llm_calls}
    assert all(gap["operation_provenance"] == "write_time_COMPOSE_attested" for gap in gaps)
    assert all("no response bytes" in gap["reason"] for gap in gaps)
    assert all(message["role"] not in {"tool", "audit"} for message in _composer_chat_history(rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", [None, "operation", "public_hash", "call_id", "role", "writer", "proposal"])
async def test_public_response_gap_has_actual_fence_and_call_binding(visible_writer, violation):
    from elspeth.web.composer.audit import llm_call_audit_envelope
    from elspeth.web.composer.visible_tool_event import visible_response_gap
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from tests.unit.web.composer.test_llm_call_audit_summary import _call

    service, session, authority, holder = visible_writer
    context = holder["context"]
    call = replace(_call(finish_reason="length"), call_id=str(uuid4()))
    public = llm_call_audit_envelope(call)
    gap = visible_response_gap(call, context, public)
    role, writer = "audit", "compose_loop"
    if violation == "operation":
        gap["operation_id"] = "another-operation"
    elif violation == "public_hash":
        gap["public_call_hash"] = "0" * 64
    elif violation == "call_id":
        gap["provider_call_id"] = "another-call"
    elif violation == "role":
        role = "system"
    elif violation == "writer":
        writer = "route_user_message"
    elif violation == "proposal":
        authority.release(context)
        context = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.PROPOSAL, owner_instance_id="owner", lease_seconds=300
        )
        holder["context"] = context
    draft = AuditMessageDraft(role=role, content="Public call result recorded.", tool_calls=(public, gap))

    async def write():
        await service.add_messages_atomic(
            session.id, (draft,), writer_principal=writer, session_operation_context=context, session_operation_kind=context.operation_kind
        )

    if violation is not None:
        with pytest.raises(AuditIntegrityError):
            await write()
        assert await service.get_messages(session.id, limit=None) == []
    else:
        await write()
        _rows, value = await _history(service, session, context)
        event = value["events"][0]
        assert event["visible_response_coverage_gap"]["provider_call_id"] == call.call_id
        assert event["visible_response_coverage_gap"]["status"] == "success"
        assert "thinking" not in event and "tool_calls" not in event
