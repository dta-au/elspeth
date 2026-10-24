"""Ordinary visible evidence survives the actual joined P4 storage boundary."""

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes._helpers import _composer_chat_history, _message_response
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
    rows = (RedactedToolRow("visible-call", display_result, None, companion),)
    return (
        {
            "session_id": str(session.id),
            "assistant_content": "A visible tool action",
            "raw_content": None,
            "redacted_assistant_tool_calls": (
                {"id": "visible-call", "type": "function", "function": {"name": "create_blob", "arguments": display_arguments}},
            ),
            "redacted_tool_rows": rows,
            "revoked_tool_rows": rows,
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
    kwargs["revoked_tool_rows"] = kwargs["redacted_tool_rows"]
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


@pytest.mark.parametrize(
    "inline_value",
    [
        {"content": "INLINE_CUSTODY_PRIVATE_CANARY", "filename": "input.csv", "mime_type": "text/csv"},
        "INLINE_CUSTODY_PRIVATE_CANARY",
        ["INLINE_CUSTODY_PRIVATE_CANARY", {"nested": "INLINE_CUSTODY_PRIVATE_CANARY"}],
        {"unknown": {"nested": "INLINE_CUSTODY_PRIVATE_CANARY"}},
        {"content": "bad\udc80INLINE_CUSTODY_PRIVATE_CANARY"},
    ],
    ids=("valid", "string", "array", "unknown-fields", "surrogate"),
)
def test_capture_excludes_inline_custody_bytes_from_every_visible_tool_string(inline_value):
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    authored = json.dumps({"source": {"inline_blob": inline_value}, "ordinary": "KEEP_ORDINARY_CANARY"})
    captured = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="call",
        tool_name="set_pipeline",
        received_arguments=authored,
        effective_arguments=authored,
        selected_result=json.dumps({"data": {"pipeline": json.loads(authored)}}),
        display_arguments="{}",
        display_result="{}",
    )
    assert "INLINE_CUSTODY_PRIVATE_CANARY" not in captured.payload_json
    payload = json.loads(captured.payload_json)
    for name in ("received_arguments", "effective_arguments", "selected_result"):
        assert "KEEP_ORDINARY_CANARY" in payload[name]
    assert captured.payload_hash == hashlib.sha256(captured.payload_json.encode()).hexdigest()


@pytest.mark.parametrize(
    "malformed",
    [
        '{"source":{"inline_blob":"INLINE_CUSTODY_PRIVATE_CANARY"}',
        '{"source":{"inline_blob":"INLINE_CUSTODY_PRIVATE_CANARY"},"source":{}}',
    ],
    ids=("invalid-json", "duplicate-keys"),
)
def test_capture_refuses_unparseable_pipeline_strings_without_losing_ordinary_other_tools(malformed):
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    pipeline = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="pipeline",
        tool_name="set_pipeline",
        received_arguments=malformed,
        effective_arguments=malformed,
        selected_result=malformed,
        display_arguments="{}",
        display_result="{}",
    )
    assert "INLINE_CUSTODY_PRIVATE_CANARY" not in pipeline.payload_json
    payload = json.loads(pipeline.payload_json)
    for name in ("received_arguments", "effective_arguments", "selected_result"):
        assert payload[name] is None
        assert name + "_inline_custody_projection_unavailable" in payload["availability_gaps"]
    ordinary = "ordinary non-JSON tool prose"
    control = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="ordinary",
        tool_name="ordinary_unknown_tool",
        received_arguments=ordinary,
        effective_arguments=ordinary,
        selected_result=ordinary,
        display_arguments="{}",
        display_result="{}",
    )
    assert json.loads(control.payload_json)["received_arguments"] == ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ("received_arguments", "effective_arguments", "selected_result"))
async def test_historical_hash_valid_inline_evidence_is_private_readable_but_refused_publicly(visible_writer, slot):
    service, session, _authority, holder = visible_writer
    context = holder["context"]
    kwargs, *_ = _kwargs(session, context)
    authored = json.dumps({"source": {"inline_blob": {"content": "HISTORICAL_INLINE_PRIVATE_CANARY"}}})
    safe = VisibleToolEvent.capture(
        context=context,
        request_id="request",
        tool_call_id="visible-call",
        tool_name="set_pipeline",
        received_arguments=authored,
        effective_arguments=authored,
        selected_result=authored,
        display_arguments="{}",
        display_result="{}",
    )
    kwargs["redacted_assistant_tool_calls"] = (
        {"id": "visible-call", "type": "function", "function": {"name": "set_pipeline", "arguments": "{}"}},
    )
    kwargs["redacted_tool_rows"] = (RedactedToolRow("visible-call", "{}", None, safe),)
    kwargs["revoked_tool_rows"] = kwargs["redacted_tool_rows"]
    kwargs["private_tool_evidence"] = (
        build_private_tool_evidence(
            session_id=str(session.id),
            operation_id=context.fence.operation_id,
            operation_epoch=context.fence.operation_epoch,
            tool_call_id="visible-call",
            tool_name="set_pipeline",
            status="success",
            arguments_canonical=canonical_json(json.loads(authored)),
            result_canonical="{}",
        ),
    )
    await service.persist_compose_turn_async(**kwargs)
    public = await service.get_messages(session.id, limit=None, include_private_evidence=False)
    safe_row = next(row for row in public if row.role == "tool")
    assert "HISTORICAL_INLINE_PRIVATE_CANARY" not in _message_response(safe_row).model_dump_json()
    safe_envelope = safe_row.tool_calls[0]
    assert VisibleToolEvent.from_envelope(safe_envelope).payload_hash == safe.payload_hash

    # Recreate an older, correctly hash-bound unsafe companion in storage. The
    # public reader must refuse it without rewriting the original audit bytes.
    payload = json.loads(safe.payload_json)
    payload[slot] = authored
    legacy_json = json.dumps(payload)
    legacy = replace(
        safe,
        parent_assistant_id=str(safe_row.parent_assistant_id),
        payload_json=legacy_json,
        payload_hash=hashlib.sha256(legacy_json.encode()).hexdigest(),
    )
    legacy_envelope = legacy.to_envelope()
    with service._engine.begin() as conn:
        conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == str(safe_row.id)).values(tool_calls=[legacy_envelope]))
    with pytest.raises(AuditIntegrityError, match="inline custody"):
        VisibleToolEvent.from_envelope(legacy_envelope)
    with pytest.raises(AuditIntegrityError, match="inline custody"):
        await service.get_messages(session.id, limit=None, include_private_evidence=False)
    private = await service.get_messages(session.id, limit=None)
    legacy_row = next(row for row in private if row.id == safe_row.id)
    assert legacy_row.tool_calls[0]["payload_json"] == legacy_json
    with pytest.raises(AuditIntegrityError, match="inline custody"):
        _message_response(legacy_row)
    with service._engine.connect() as conn:
        stored = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.id == str(safe_row.id))).scalar_one()
    assert stored == [legacy_envelope]


def test_pipeline_capture_with_credential_assignment_records_explicit_gaps_and_roundtrips():
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    authored = json.dumps(
        {
            "source": {"inline_blob": {"content": "PRIVATE_INLINE_CANARY"}},
            "ordinary": "KEEP",
            "options": {"api_key": "SYNTHETIC_ASSIGNMENT_CANARY"},  # secret-scan: allow-this-line
        }
    )
    event = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="call",
        tool_name="set_pipeline",
        received_arguments=authored,
        effective_arguments=authored,
        selected_result=authored,
        display_arguments="{}",
        display_result="{}",
    )
    payload = json.loads(event.payload_json)
    for slot in ("received_arguments", "effective_arguments", "selected_result"):
        assert payload[slot] is None
        assert slot + "_credential_redaction_invalid_json" in payload["availability_gaps"]
    assert "PRIVATE_INLINE_CANARY" not in event.payload_json
    assert "SYNTHETIC_ASSIGNMENT_CANARY" not in event.payload_json
    assert VisibleToolEvent.from_envelope(event.to_envelope()) == event


@pytest.mark.parametrize("slot", ("received_arguments", "effective_arguments", "selected_result"))
def test_stored_outer_duplicate_fields_cannot_hide_inline_custody_bytes(slot):
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    safe = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="call",
        tool_name="set_pipeline",
        received_arguments="{}",
        effective_arguments="{}",
        selected_result="{}",
        display_arguments="{}",
        display_result="{}",
    )
    unsafe = json.dumps({"source": {"inline_blob": {"content": "HIDDEN_PRIVATE_CANARY"}}})
    payload = "{" + json.dumps(slot) + ":" + json.dumps(unsafe) + "," + safe.payload_json[1:]
    legacy = replace(safe, payload_json=payload, payload_hash=hashlib.sha256(payload.encode()).hexdigest())
    with pytest.raises(AuditIntegrityError, match="stored payload"):
        VisibleToolEvent.from_envelope(legacy.to_envelope())


@pytest.mark.parametrize(
    "body",
    [
        "{",
        '{"received_arguments":"\\udc80","effective_arguments":null,"selected_result":null,"availability_gaps":[]}',
        '{"received_arguments":"\udc80","effective_arguments":null,"selected_result":null,"availability_gaps":[]}',
    ],
)
def test_stored_outer_parse_and_unicode_failures_raise_audit_integrity_error(body):
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    safe = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="call",
        tool_name="set_pipeline",
        received_arguments="{}",
        effective_arguments="{}",
        selected_result="{}",
        display_arguments="{}",
        display_result="{}",
    )
    corrupted = replace(safe, payload_json=body, payload_hash=hashlib.sha256(body.encode(errors="surrogatepass")).hexdigest())
    with pytest.raises(AuditIntegrityError, match="stored payload"):
        VisibleToolEvent.from_envelope(corrupted.to_envelope())


def test_capture_roundtrips_large_aggregate_without_omitting_any_ordinary_slot():
    from tests.unit.web.composer.test_turn_effects import CONTEXT

    original = json.dumps({"ordinary": "a" * 400_000})
    event = VisibleToolEvent.capture(
        context=CONTEXT,
        request_id="request",
        tool_call_id="call",
        tool_name="set_pipeline",
        received_arguments=original,
        effective_arguments=original,
        selected_result=original,
        display_arguments="{}",
        display_result="{}",
    )
    assert len(event.payload_json.encode()) > 1024 * 1024
    restored = VisibleToolEvent.from_envelope(event.to_envelope())
    payload = json.loads(restored.payload_json)
    assert payload["availability_gaps"] == []
    for slot in ("received_arguments", "effective_arguments", "selected_result"):
        assert payload[slot] == original


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
