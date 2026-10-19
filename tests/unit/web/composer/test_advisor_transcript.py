"""Visible chronology is review evidence, never provider protocol or authority."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.validation import _redact_sensitive_content


def _row(session, role, content, *, writer="compose_loop", **kwargs):
    return ChatMessageRecord(
        id=uuid4(),
        session_id=session,
        role=role,
        content=content,
        created_at=datetime.now(UTC),
        writer_principal=writer,
        **kwargs,
    )


def test_authenticated_history_preserves_repeated_humans_and_current_request_reference():
    sid = uuid4()
    first = _row(sid, "user", "Same requirement", writer="route_user_message", sequence_no=1)
    second = _row(sid, "user", "Same requirement", writer="route_user_message", sequence_no=2)
    current = _row(sid, "user", "Complete current request", writer="route_user_message", sequence_no=3)
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(sid),
        actor_user_id="actor",
        current_request_id=str(current.id),
        request=current.content,
        records=(first, second, current),
    )
    owner = AdvisorTranscriptOwner(request=current.content, history=history)
    value = json.loads(owner.snapshot().provider_json)
    assert [event["event_id"] for event in value["events"]] == [str(first.id), str(second.id), str(current.id)]
    assert [event["content"] for event in value["events"][:2]] == [first.content, second.content]
    assert value["events"][2]["content_ref"] == "complete_original_user_request_section"
    assert "content" not in value["events"][2]
    with pytest.raises(AuditIntegrityError):
        history.validate_for(session_id=str(uuid4()), actor_user_id="actor", request_id=str(current.id), request=current.content)
    assert owner.covers_prior_requests([first.content, second.content])
    assert not owner.covers_prior_requests([first.content, second.content, first.content])
    assert not owner.covers_prior_requests(["A distinct caller-owned amendment"])
    assert not AdvisorTranscriptOwner(request=current.content).covers_prior_requests([first.content])


def test_current_events_keep_original_args_linked_to_backend_finalization_and_deleted_notices():
    owner = AdvisorTranscriptOwner(request="Build the requested draft")
    messages = [{"role": "system", "content": "STATIC_SKILL_CANARY"}, {"role": "user", "content": "CATALOG_CANARY"}]
    owner.begin_messages(messages)
    assistant = {
        "role": "assistant",
        "content": "A visible explanation",
        "thinking_blocks": [{"thinking": "HIDDEN_CANARY"}],
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "set_pipeline", "arguments": '{ "node": "authored" }'}}],
    }
    messages.append(assistant)
    owner.observe_messages(messages)
    assistant["tool_calls"][0]["function"]["arguments"] = '{"node":"authored","review":"backend-added"}'
    notice = {"role": "user", "content": "Visible backend notice"}
    messages.append(notice)
    owner.observe_messages(messages)
    messages.remove(notice)
    owner.observe_messages(messages)
    snapshot = owner.snapshot()
    value = json.loads(snapshot.provider_json)
    call = value["events"][0]["tool_calls"][0]
    assert call["received_arguments"] == '{ "node": "authored" }'
    assert call["effective_arguments"] == assistant["tool_calls"][0]["function"]["arguments"]
    assert call["argument_provenance"] == "provider_received_then_backend_finalized"
    assert value["events"][1]["content"] == notice["content"]
    assert "STATIC_SKILL_CANARY" not in snapshot.provider_json and "CATALOG_CANARY" not in snapshot.provider_json
    assert "HIDDEN_CANARY" not in snapshot.provider_json


def test_transcript_redacts_secrets_and_losslessly_guards_long_tail_and_nested_json():
    owner = AdvisorTranscriptOwner(request="Current")
    owner.begin_messages([])
    body = "Read these supplied rules. " * 1700 + "TAIL_REQUIREMENT_CANARY"
    owner.observe_messages([{"role": "tool", "tool_call_id": "read-1", "content": json.dumps({"content": body})}])
    secret = "sk-" + "z" * 48
    owner.observe_messages([{"role": "assistant", "content": "Visible " + secret}])
    snapshot = owner.snapshot()
    assert secret not in snapshot.provider_json
    assert "TAIL_REQUIREMENT_CANARY" in snapshot.provider_json
    parts = snapshot.untrusted_content_parts()
    assert "".join(part.text for part in parts) == snapshot.provider_json
    assert all(
        part.trust is None and len(part.inspection_text if part.inspection_text is not None else part.text) < 10000 for part in parts
    )
    assert "TAIL_REQUIREMENT_CANARY" in "".join(part.inspection_text or part.text for part in parts)
    before = snapshot.binding
    owner.observe_messages([{"role": "assistant", "content": "A new visible event"}])
    assert owner.snapshot().binding != before


def test_complete_json_assignment_redaction_has_no_authored_key_identity_exemption():
    from elspeth.web.composer.advisor_transcript import _redact_text

    assignment = '{"api_key":"SYNTHETIC_TRANSCRIPT_CANARY"}'  # secret-scan: allow-this-line
    assert "SYNTHETIC_TRANSCRIPT_CANARY" not in _redact_text(assignment)
    digit_identity = "12345678-1234-1234-1234-123456789012"
    for key in ("blob_id", "ordinary_key"):
        value = json.dumps({key: digit_identity})
        assert _redact_text(value) == _redact_sensitive_content(value)
        assert digit_identity not in _redact_text(value)
    ordinary = '{ "safe": "ordinary visible value" }'
    assert _redact_text(ordinary) == ordinary


def test_cross_session_rows_and_forged_current_human_are_refused():
    sid = uuid4()
    current = _row(sid, "user", "Current", writer="compose_loop")
    with pytest.raises(AuditIntegrityError):
        AdvisorTranscriptHistory.from_records(
            session_id=str(sid),
            actor_user_id="actor",
            current_request_id=str(current.id),
            request=current.content,
            records=(current,),
        )
    human = _row(sid, "user", "Current", writer="route_user_message")
    with pytest.raises(AuditIntegrityError):
        AdvisorTranscriptHistory.from_records(
            session_id=str(sid),
            actor_user_id="actor",
            current_request_id=str(human.id),
            request=human.content,
            records=(_row(uuid4(), "assistant", "Other session"), human),
        )


def _read_history(*, offset=0, end=None, session_id=None):
    sid, blob = session_id or uuid4(), str(uuid4())
    operation = uuid4()
    text = "Generic normative instructions. " * 1000 + "REFERENCE_TAIL_CANARY"
    raw = text.encode()
    digest = hashlib.sha256(raw).hexdigest()
    end = len(text) if end is None else end
    assistant = _row(
        sid,
        "assistant",
        "",
        sequence_no=1,
        operation_id=operation,
        tool_calls=[
            {
                "id": "read-call",
                "type": "function",
                "function": {
                    "name": "get_blob_content",
                    "arguments": json.dumps({"blob_id": blob, "expected_content_hash": digest, "offset_chars": offset}),
                },
            }
        ],
    )
    result = {
        "success": True,
        "data": {
            "blob_id": "<redacted>",
            "content": {"_type": "str", "length": end - offset},
            "content_hash": digest,
            "size_bytes": len(raw),
            "total_chars": len(text),
            "start_char": offset,
            "end_char": end,
            "truncated": end < len(text),
            "created_by": "user",
            "creation_modality": "user_upload",
        },
    }
    tool = _row(
        sid, "tool", json.dumps(result), sequence_no=2, tool_call_id="read-call", parent_assistant_id=assistant.id, operation_id=operation
    )
    current = _row(sid, "user", "Continue", writer="route_user_message", sequence_no=3)
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(sid),
        actor_user_id="actor",
        current_request_id=str(current.id),
        request=current.content,
        records=(assistant, tool, current),
    )
    metadata = {
        "id": blob,
        "session_id": str(sid),
        "status": "ready",
        "size_bytes": len(raw),
        "content_hash": digest,
        "created_by": "user",
        "creation_modality": "user_upload",
    }
    return history, metadata, raw, text


@pytest.mark.parametrize("case", ["valid", "partial", "stale", "missing", "wrong_origin", "credential"])
def test_rehydrate_only_actual_recorded_read_under_current_custody(case):
    history, metadata, raw, text = _read_history(offset=17, end=73) if case == "partial" else _read_history()
    calls = []

    def reader(blob_id):
        calls.append(blob_id)
        assert blob_id == metadata["id"]  # parent argument, never result's redacted selector
        if case == "missing":
            return None
        if case == "stale":
            return metadata | {"content_hash": "a" * 64}, raw
        if case == "wrong_origin":
            return metadata | {"created_by": "llm"}, raw
        return metadata, raw

    if case == "credential":
        events = json.loads(history.events_json)
        secret = "sk-" + "x" * 48
        raw = (text + secret).encode()
        metadata |= {"size_bytes": len(raw), "content_hash": hashlib.sha256(raw).hexdigest()}
        result = json.loads(events[1]["content"])
        result["data"] |= {"size_bytes": len(raw), "content_hash": metadata["content_hash"], "total_chars": len(raw), "end_char": len(raw)}
        events[1]["content"] = json.dumps(result)
        args = json.loads(events[0]["tool_calls"][0]["effective_arguments"])
        args["expected_content_hash"] = metadata["content_hash"]
        events[0]["tool_calls"][0]["effective_arguments"] = json.dumps(args)
        from dataclasses import replace

        history = replace(history, events_json=json.dumps(events))
    owner = AdvisorTranscriptOwner(request="Continue", history=history, blob_reader=reader)
    snapshot = owner.snapshot()
    event = json.loads(snapshot.provider_json)["events"][1]
    if case in {"valid", "partial"}:
        assert event["recovered_read"]["content"] == (text[17:73] if case == "partial" else text)
        assert event["recovered_read"]["primary_delivery"] == "historical_provider_delivery_not_reconstructed"
    else:
        assert "recovered_read" not in event
        assert event["read_body_availability"].startswith("unavailable_")
    assert calls == [metadata["id"]]


def test_recovery_rechecks_current_access_and_invalidates_snapshot_without_new_graph():
    history, metadata, raw, _text = _read_history()
    available = True

    def reader(blob_id):
        assert blob_id == metadata["id"]
        return (metadata, raw) if available else None

    owner = AdvisorTranscriptOwner(request="Continue", history=history, blob_reader=reader)
    first = owner.snapshot()
    available = False
    second = owner.snapshot()
    assert first.binding != second.binding
    assert "REFERENCE_TAIL_CANARY" in first.provider_json and "REFERENCE_TAIL_CANARY" not in second.provider_json


def test_nested_unicode_escapes_are_guard_inspected_as_decoded_text():
    owner = AdvisorTranscriptOwner(request="Current")
    owner.begin_messages([])
    encoded = '{"content": "\\u0049\\u004e\\u0053\\u0054\\u0052\\u0055\\u0043\\u0054"}'
    owner.observe_messages([{"role": "tool", "tool_call_id": "encoded", "content": encoded}])
    snapshot = owner.snapshot()
    parts = snapshot.untrusted_content_parts()
    assert "".join(part.text for part in parts) == snapshot.provider_json
    assert "INSTRUCT" in "".join(part.inspection_text if part.inspection_text is not None else part.text for part in parts)


def test_visible_transcript_changes_exact_binding_but_repeated_evidence_does_not_fake_progress():
    owner = AdvisorTranscriptOwner(request="Current")
    owner.begin_messages([])
    first = {"role": "tool", "tool_call_id": "one", "content": json.dumps({"success": True, "data": {"schema_fact": "A declared field"}})}
    owner.observe_messages([first])
    original = owner.snapshot()
    owner.observe_messages([dict(first) | {"tool_call_id": "two"}])
    repeated = owner.snapshot()
    assert repeated.binding != original.binding
    assert repeated.semantic_binding == original.semantic_binding
    owner.observe_messages(
        [
            {
                "role": "tool",
                "tool_call_id": "three",
                "content": json.dumps({"success": True, "data": {"schema_fact": "A newly supplied constraint"}}),
            }
        ]
    )
    assert owner.snapshot().semantic_binding != repeated.semantic_binding


@pytest.mark.parametrize("stored_role", ["tool", "audit"])
def test_legacy_audit_recovery_checks_canonical_hashes_before_disclosure(stored_role):
    from dataclasses import replace

    history, metadata, raw, _text = _read_history()
    events = json.loads(history.events_json)
    canonical_arguments = events[0]["tool_calls"][0]["effective_arguments"]
    canonical_result = events[1]["content"]
    sid = uuid4()
    invocation = {
        "tool_call_id": "legacy-read",
        "tool_name": "get_blob_content",
        "status": "success",
        "arguments_canonical": canonical_arguments,
        "arguments_hash": hashlib.sha256(canonical_arguments.encode()).hexdigest(),
        "result_canonical": canonical_result,
        "result_hash": hashlib.sha256(canonical_result.encode()).hexdigest(),
    }
    tool = _row(sid, stored_role, canonical_result, sequence_no=1, tool_calls=[{"_kind": "audit", "invocation": invocation}])
    current = _row(sid, "user", "Continue", writer="route_user_message", sequence_no=2)
    legacy = AdvisorTranscriptHistory.from_records(
        session_id=str(sid), actor_user_id="actor", current_request_id=str(current.id), request="Continue", records=(tool, current)
    )
    owner = AdvisorTranscriptOwner(request="Continue", history=legacy, blob_reader=lambda _blob: (metadata | {"session_id": str(sid)}, raw))
    assert "REFERENCE_TAIL_CANARY" in owner.snapshot().provider_json
    tampered = replace(tool, tool_calls=[{"_kind": "audit", "invocation": invocation | {"arguments_hash": "0" * 64}}])
    with pytest.raises(AuditIntegrityError):
        AdvisorTranscriptHistory.from_records(
            session_id=str(sid), actor_user_id="actor", current_request_id=str(current.id), request="Continue", records=(tampered, current)
        )


def test_withheld_visible_prose_is_recovered_without_replaying_private_provider_packets():
    from dataclasses import replace

    from elspeth.web.composer.withheld_replies import withheld_reply_envelope

    sid = uuid4()
    text = "Visible provider words that were withheld from publication."
    withheld = _row(sid, "audit", text, sequence_no=1, tool_calls=[withheld_reply_envelope("planner_prose_unadmitted", text)])
    private = _row(sid, "audit", "PRIVATE_PACKET_NOT_A_TRANSCRIPT", sequence_no=2)
    current = _row(sid, "user", "Continue", writer="route_user_message", sequence_no=3)
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(sid),
        actor_user_id="actor",
        current_request_id=str(current.id),
        request="Continue",
        records=(withheld, private, current),
    )
    rendered = AdvisorTranscriptOwner(request="Continue", history=history).snapshot().provider_json
    assert rendered.count(text) == 1 and "withheld_visible_provider_reply" in rendered
    assert "PRIVATE_PACKET_NOT_A_TRANSCRIPT" not in rendered
    bad = replace(withheld, content="Changed words after their hash was stored")
    with pytest.raises(AuditIntegrityError):
        AdvisorTranscriptHistory.from_records(
            session_id=str(sid), actor_user_id="actor", current_request_id=str(current.id), request="Continue", records=(bad, current)
        )


def test_missing_parent_never_recovers_a_read_by_fuzzy_call_or_filename():
    from dataclasses import replace

    history, metadata, raw, _text = _read_history()
    events = json.loads(history.events_json)
    events[1]["parent_assistant_id"] = str(uuid4())
    calls = []

    def reader(blob):
        calls.append(blob)
        return metadata, raw

    owner = AdvisorTranscriptOwner(request="Continue", history=replace(history, events_json=json.dumps(events)), blob_reader=reader)
    result = json.loads(owner.snapshot().provider_json)["events"][1]
    assert result["read_body_availability"] == "unavailable_parent_request_binding"
    assert not calls and "recovered_read" not in result


@pytest.mark.parametrize("tamper", ["tool_writer", "parent_writer", "other_operation", "missing_operation"])
def test_historical_read_recovery_requires_owned_writer_and_exact_parent_operation(tamper):
    from dataclasses import replace

    history, metadata, raw, _text = _read_history()
    events = json.loads(history.events_json)
    if tamper == "tool_writer":
        events[1]["writer_principal"] = "admin_tool"
    elif tamper == "parent_writer":
        events[0]["writer_principal"] = "admin_tool"
    elif tamper == "other_operation":
        events[0]["operation_id"] = str(uuid4())
    else:
        events[0]["operation_id"] = None
    calls = []
    owner = AdvisorTranscriptOwner(
        request="Continue",
        history=replace(history, events_json=json.dumps(events)),
        blob_reader=lambda blob: calls.append(blob) or (metadata, raw),
    )
    event = json.loads(owner.snapshot().provider_json)["events"][1]
    assert event["read_body_availability"].startswith("unavailable_")
    assert not calls and "recovered_read" not in event
