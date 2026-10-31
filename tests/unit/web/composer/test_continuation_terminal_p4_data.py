"""Terminal DATA relations, without claiming SQL commit or live ownership."""

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolBatch, _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.continuation import EffectFrontierEntry, LogicalRequestBinding, PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import (
    RecorderDrainCursors,
    RecorderSnapshot,
    RetainedVisibleLink,
    encode_recorder_snapshot,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_response, encode_private_chunk
from elspeth.web.composer.continuation_evidence_application_values import P4EffectProducerData
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_p4_outcome import RetainedAuditOutcome
from elspeth.web.composer.continuation_phase_transitions import ToolIssueSource
from elspeth.web.composer.continuation_prepared_p4 import capture_prepared_p4, encode_prepared_p4, p4_semantic_input, prepared_p4_members
from elspeth.web.composer.continuation_terminal_p4_data import (
    TerminalP4Document,
    TerminalP4Input,
    TerminalP4Reference,
    TerminalP4Result,
    decode_terminal_p4_input,
    decode_terminal_p4_result,
    encode_terminal_p4_input,
    encode_terminal_p4_result,
    terminal_p4_input_key,
)
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record
from tests.unit.web.composer.test_continuation_response import completion


def terminal_input(*, reason="service_failure", error_class=None, error_message=None):
    """Real codec/audit producers; historical locators require later SQL joins."""
    session = str(UUID(int=11))
    context = SessionOperationContext(SessionOperationFence(session, "physical", "lease", 7), SessionOperationKind.COMPOSE)
    arguments = '{"patch":{"description":"café"}}'
    call = _AdmittedToolCall("issued", _AdmittedToolFunction("set_metadata", arguments))
    suffix = _AdmittedToolCall("must-not-run", _AdmittedToolFunction("set_metadata", arguments))
    response = encode_continuation_response(
        replace(completion(), tool_batch=_AdmittedToolBatch((call, suffix), frozenset({call.id, suffix.id})))
    )
    controlled = {"success": False, "status": "SERVICE_FAILURE", "reason": reason}
    authoring = PrivateToolAuthoring(call.id, call.function.name, arguments, None, arguments)
    invocation = replace(
        finish_success(
            begin_dispatch(
                call.id,
                call.function.name,
                json.loads(arguments),
                version_before=3,
                actor="composer",
                strict_sent=True,
                wire_conformant=True,
            ),
            result_payload=controlled,
            version_after=3,
        ),
        private_authoring=authoring,
        error_class=error_class,
        error_message=error_message,
    )
    visible = VisibleToolEvent.capture(
        context=context,
        request_id="request",
        tool_call_id=call.id,
        tool_name=call.function.name,
        received_arguments=arguments,
        effective_arguments=arguments,
        selected_result=canonical_json(controlled),
        display_arguments=arguments,
        display_result=canonical_json(controlled),
    )
    rows = (RedactedToolRow(call.id, canonical_json(controlled), None, visible),)
    evidence = build_private_tool_evidence(
        session_id=session,
        operation_id="physical",
        operation_epoch=7,
        tool_call_id=call.id,
        tool_name=call.function.name,
        status=invocation.status.value,
        arguments_canonical=invocation.arguments_canonical,
        result_canonical=invocation.result_canonical,
        raw_arguments=arguments,
    )
    withheld = WithheldReply("planner_prose_unadmitted", "retained café")
    prepared = capture_prepared_p4(
        context=context,
        assistant_content="café",
        raw_content=None,
        redacted_calls=(
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": arguments},
                "strict_sent": True,
                "wire_conformant": True,
            },
        ),
        redacted_rows=rows,
        revoked_rows=rows,
        mutation_ceiling=ComposerMutationCeiling(session, "request", "auto_commit"),
        authority_revocation=None,
        private_tool_evidence=(evidence,),
        private_tool_failures=(),
        rejection_records=(),
        withheld_replies=(withheld,),
        invocations=(invocation,),
        authorings=(authoring,),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        assistant_row_uses_current_dispatch=True,
        crash_pending=True,
    )
    llm, _metadata = actual_record()
    recorder = RecorderSnapshot(
        invocations=(invocation,),
        llm_calls=(llm, replace(llm, call_id=str(UUID(int=42)))),
        planner_attempts=(),
        withheld_replies=(withheld, WithheldReply("planner_prose_unadmitted", "undrained tail")),
        visible_tool_events=(RetainedVisibleLink(0, visible),),
        private_candidates=(),
        private_authorings=(authoring,),
        drained=RecorderDrainCursors(llm_calls=1),
    )
    issue = ToolIssueSource(
        previous_checkpoint_id="before-issue",
        cursor=8,
        sequence=2,
        generation=3,
        creator_operation_id="physical",
        creator_operation_epoch=7,
        response=response.reference,
        prepared=PrivateChunkRef(codec="composer.tool-batch.v1", digest="a" * 64, byte_length=100),
        tool_index=0,
        tool_call_id=call.id,
        input_sha256=stable_hash({"tool_call_id": call.id, "name": call.function.name, "arguments": authoring.arguments_canonical}),
    )
    key = f"tool:{response.reference.digest}:0"
    return TerminalP4Input(
        logical=LogicalRequestBinding(
            session_id=session,
            operation_id="logical",
            original_user_message_id="request",
            request_hash="c" * 64,
            kind="compose_message",
            base_state_id=None,
        ),
        actor_user_id="actor",
        claim_attempt=1,
        producer=P4EffectProducerData(2, 3, "physical", 7),
        checkpoint_id="after-issue",
        cursor=8,
        issue=issue,
        issued_effect_key=key,
        prior_prefix=PrivateChunkRef(codec="composer.coupled-batch-data.v1", digest="d" * 64, byte_length=100),
        source_evidence=PrivateChunkRef(codec="composer.evidence.v2", digest="e" * 64, byte_length=100),
        response=response,
        recorder=encode_recorder_snapshot(recorder),
        effect_frontier=(EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),),
        prepared=prepared,
        semantic_input_sha256=sha256(p4_semantic_input(prepared).encode("utf-8")).hexdigest(),
    )


def terminal_result(value, document, *, unavailable=False):
    """Consistent receipt DATA, not a substitute for actual SQL receiving."""
    prepared = value.prepared
    members = prepared_p4_members(prepared)
    assistant = None if unavailable else UUID(int=1)
    outcome = RetainedAuditOutcome(
        assistant_id=None if assistant is None else str(assistant),
        unwind_audit_failed=unavailable,
        checkpoint_disposition="unavailable" if unavailable else "empty",
        checkpoint=None,
        current_state_id=None,
        authority_revocation_json=None,
        published_tool_rows_json="[]" if unavailable else prepared.redacted_rows_json,
        published_assistant_content=None if unavailable else prepared.assistant_content,
    )
    receipt = PersistedComposerTurnReceipt(
        effect_key=f"batch:{value.response.reference.digest}:p4",
        input_sha256=value.semantic_input_sha256,
        assistant_id=assistant,
        current_state_id=None,
        checkpoint_disposition=outcome.checkpoint_disposition,
        creator_operation_id="physical",
        inserted_state_ids=(),
        authored_state_hash=None,
        chat_message_ids=() if unavailable else (UUID(int=1), UUID(int=2), UUID(int=3)),
        schema_version="composer.persisted-turn.v2",
        prepared_p4_json=encode_prepared_p4(prepared),
        audit_outcome_json=canonical_json(outcome.model_dump(mode="json")),
        unwind_audit_failed=unavailable,
        invocation_ids=() if unavailable else tuple(item.tool_call_id for item in members.invocations),
        invocation_hashes=()
        if unavailable
        else tuple(
            sha256(canonical_json(redacted_tool_invocation_content_and_envelope(item)[1]).encode("utf-8")).hexdigest()
            for item in members.invocations
        ),
        withheld_hashes=() if unavailable else tuple(sha256(item.content.encode("utf-8")).hexdigest() for item in members.withheld_replies),
        chat_message_hashes=() if unavailable else ("1" * 64, "2" * 64, "3" * 64),
    )
    return TerminalP4Result(
        input=document.reference,
        receipt=receipt,
        drained=RecorderDrainCursors(llm_calls=1)
        if unavailable
        else RecorderDrainCursors(invocations=1, llm_calls=1, withheld_replies=1, visible_tool_events=1, private_authorings=1),
    )


def rebound(document, value):
    """Rehash malformed DATA so oppositions reach beyond the outer digest."""
    canonical = canonical_json(value)
    return TerminalP4Document(
        document.reference.model_copy(
            update={"digest": sha256(canonical.encode("utf-8")).hexdigest(), "byte_length": len(canonical.encode("utf-8"))}
        ),
        canonical,
    )


@pytest.mark.parametrize("unavailable", (False, True))
def test_terminal_documents_roundtrip_actual_codecs_without_commit_authority(unavailable):
    value = terminal_input()
    document = encode_terminal_p4_input(value)
    assert decode_terminal_p4_input(document) == value
    assert encode_terminal_p4_input(value) == document
    assert document.reference.digest != value.semantic_input_sha256
    assert document.reference.byte_length == len(document.canonical.encode("utf-8")) > len(document.canonical)
    result = terminal_result(value, document, unavailable=unavailable)
    encoded = encode_terminal_p4_result(result, input_document=document)
    assert decode_terminal_p4_result(encoded, input_document=document) == result
    assert encode_terminal_p4_result(result, input_document=document) == encoded
    assert result.drained.llm_calls == 1
    assert "committed" not in json.loads(encoded.canonical)
    assert result.receipt.unwind_audit_failed is unavailable
    with pytest.raises(ValidationError):
        value.actor_user_id = "foreign"
    with pytest.raises(ValidationError):
        PrivateChunkRef.model_validate(document.reference.model_dump())
    with pytest.raises(AuditIntegrityError, match="mode"):
        decode_terminal_p4_input(encoded)
    with pytest.raises(AuditIntegrityError, match="mode"):
        decode_terminal_p4_result(document, input_document=document)


@pytest.mark.parametrize(
    "member",
    (
        "schema_version",
        "prepared",
        "issue",
        "response",
        "recorder",
        "producer",
        "prior_prefix",
        "source_evidence",
        "effect_frontier",
        "claim_attempt",
    ),
)
def test_terminal_input_missing_fields_cannot_default_or_disappear(member):
    original = encode_terminal_p4_input(terminal_input())
    payload = json.loads(original.canonical)
    del payload[member]
    with pytest.raises(AuditIntegrityError, match=r"closed contract|omitted or normalized"):
        decode_terminal_p4_input(rebound(original, payload))
    assert encode_terminal_p4_input(decode_terminal_p4_input(original)) == original


@pytest.mark.parametrize("member", ("exception", "error_class", "cause", "traceback", "batch", "committed"))
def test_terminal_input_forbids_extra_failure_or_authority_fields(member):
    original = encode_terminal_p4_input(terminal_input())
    payload = json.loads(original.canonical)
    payload[member] = "private original"
    with pytest.raises(AuditIntegrityError, match="closed contract"):
        decode_terminal_p4_input(rebound(original, payload))


@pytest.mark.parametrize(
    "change", ("creator", "cursor", "bookmark", "role", "response", "prefix", "semantic", "issued_hash", "issued", "frontier", "coercion")
)
def test_terminal_input_foreign_bindings_refuse_after_genuine_positive(change):
    original = encode_terminal_p4_input(terminal_input())
    payload = json.loads(original.canonical)
    if change == "creator":
        payload["producer"]["creator_operation_epoch"] += 1
    elif change == "cursor":
        payload["cursor"] += 1
    elif change == "bookmark":
        payload["checkpoint_id"] = payload["issue"]["previous_checkpoint_id"]
    elif change == "role":
        payload["prior_prefix"]["codec"] = "composer.p4-pending-batch.v1"
    elif change == "response":
        payload["issue"]["response"]["digest"] = "0" * 64
    elif change == "prefix":
        payload["issue"]["tool_index"] = 1
        payload["issued_effect_key"] = f"tool:{payload['issue']['response']['digest']}:1"
    elif change == "semantic":
        payload["semantic_input_sha256"] = "0" * 64
    elif change == "issued_hash":
        payload["issue"]["input_sha256"] = "0" * 64
    elif change == "issued":
        payload["issue"]["tool_call_id"] = "must-not-run"
    elif change == "frontier":
        payload["effect_frontier"][0]["disposition"] = "committed"
    else:
        payload["claim_attempt"] = True
    with pytest.raises(AuditIntegrityError):
        decode_terminal_p4_input(rebound(original, payload))
    assert decode_terminal_p4_input(original).semantic_input_sha256 == json.loads(original.canonical)["semantic_input_sha256"]


@pytest.mark.parametrize("change", ("bytes", "length", "digest", "duplicate", "whitespace", "oversize"))
def test_terminal_document_complete_bytes_are_not_normalized(change):
    original = encode_terminal_p4_input(terminal_input())
    if change == "bytes":
        changed = replace(original, canonical=original.canonical[:-1] + " ")
    elif change == "length":
        changed = replace(original, reference=original.reference.model_copy(update={"byte_length": original.reference.byte_length + 1}))
    elif change == "digest":
        changed = replace(original, reference=original.reference.model_copy(update={"digest": "0" * 64}))
    else:
        canonical = (
            '{"schema_version":"one","schema_version":"two"}'
            if change == "duplicate"
            else original.canonical + " "
            if change == "whitespace"
            else " " * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1)
        )
        changed = replace(
            original,
            canonical=canonical,
            reference=original.reference.model_copy(
                update={"digest": sha256(canonical.encode()).hexdigest(), "byte_length": len(canonical.encode())}
            ),
        )
    with pytest.raises(AuditIntegrityError):
        decode_terminal_p4_input(changed)
    assert encode_terminal_p4_input(decode_terminal_p4_input(original)) == original


@pytest.mark.parametrize(
    "channel",
    ("invocations", "llm_calls", "planner_attempts", "withheld_replies", "visible_tool_events", "private_candidates", "private_authorings"),
)
@pytest.mark.parametrize("unavailable", (False, True))
def test_terminal_result_cannot_advance_any_unpublished_recorder_channel(channel, unavailable):
    value = terminal_input()
    original = encode_terminal_p4_input(value)
    result = terminal_result(value, original, unavailable=unavailable)
    encoded = encode_terminal_p4_result(result, input_document=original)
    payload = json.loads(encoded.canonical)
    payload["drained"][channel] += 1
    with pytest.raises(AuditIntegrityError, match="published recorder slices"):
        decode_terminal_p4_result(rebound(encoded, payload), input_document=original)
    assert decode_terminal_p4_result(encoded, input_document=original) == result


def test_terminal_result_binds_full_manifest_beyond_equal_semantic_input():
    value = terminal_input()
    original = encode_terminal_p4_input(value)
    result = encode_terminal_p4_result(terminal_result(value, original), input_document=original)
    other = encode_terminal_p4_input(value.model_copy(update={"actor_user_id": "another-actor"}))
    assert decode_terminal_p4_input(other).semantic_input_sha256 == value.semantic_input_sha256
    assert other.reference.digest != original.reference.digest
    with pytest.raises(AuditIntegrityError, match="complete input manifest"):
        decode_terminal_p4_result(result, input_document=other)
    assert decode_terminal_p4_result(result, input_document=original).input == original.reference


@pytest.mark.parametrize("change", ("effect", "hash", "legacy", "input", "rows", "extra", "missing"))
def test_terminal_result_refuses_foreign_or_incomplete_receipt(change):
    value = terminal_input()
    original = encode_terminal_p4_input(value)
    encoded = encode_terminal_p4_result(terminal_result(value, original), input_document=original)
    payload = json.loads(encoded.canonical)
    if change == "effect":
        payload["receipt"]["effect_key"] = "batch:" + "0" * 64 + ":p4"
    elif change == "hash":
        payload["receipt"]["input_sha256"] = "0" * 64
    elif change == "legacy":
        payload["receipt"]["schema_version"] = None
    elif change == "input":
        payload["receipt"]["prepared_p4_json"] = "{}"
    elif change == "rows":
        payload["receipt"]["chat_message_hashes"] = []
    elif change == "extra":
        payload["committed"] = True
    else:
        del payload["receipt"]["withheld_hashes"]
    with pytest.raises(AuditIntegrityError):
        decode_terminal_p4_result(rebound(encoded, payload), input_document=original)


def test_terminal_encoding_bounds_the_whole_envelope_and_refuses_credentials():
    value = terminal_input()
    original = encode_terminal_p4_input(value)
    with pytest.raises(AuditIntegrityError, match="complete byte bound"):
        encode_terminal_p4_input(value.model_copy(update={"actor_user_id": "x" * CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES}))
    with pytest.raises(CredentialMaterialRefused):
        encode_terminal_p4_input(value.model_copy(update={"actor_user_id": "Bearer terminal-private-token-value"}))
    assert decode_terminal_p4_input(original) == value


def test_terminal_storage_slot_is_deterministic_closed_and_separate_from_normal_chunks():
    original = encode_terminal_p4_input(terminal_input())
    value = decode_terminal_p4_input(original)
    effect = f"batch:{value.response.reference.digest}:p4"
    assert terminal_p4_input_key(effect) == terminal_p4_input_key(effect)
    assert len(terminal_p4_input_key(effect)) <= 128
    assert terminal_p4_input_key(effect) != terminal_p4_input_key("batch:" + "0" * 64 + ":p4")
    for foreign in ("tool:foreign:0", "batch:" + "G" * 64 + ":p4", effect + ":extra"):
        with pytest.raises(AuditIntegrityError):
            terminal_p4_input_key(foreign)
    normal = encode_private_chunk(codec="composer.messages.v1", canonical="[]")
    with pytest.raises(AuditIntegrityError, match="closed document owner"):
        decode_terminal_p4_input(normal)


@pytest.mark.parametrize("reason", ("service_unavailable", "PrivateOriginal", "private original exception text"))
def test_terminal_service_result_is_closed_even_with_consistent_prepared_hashes(reason):
    original = encode_terminal_p4_input(terminal_input())
    altered = terminal_input(reason=reason)
    with pytest.raises(AuditIntegrityError, match="closed value-free service completion"):
        encode_terminal_p4_input(altered)
    assert encode_terminal_p4_input(decode_terminal_p4_input(original)) == original


@pytest.mark.parametrize("owner", ("input", "prepared", "result", "receipt"))
def test_unchecked_model_copy_cannot_hide_live_failure_in_an_omitted_extra(owner):
    value = terminal_input()
    document = encode_terminal_p4_input(value)
    original = RuntimeError("private original")
    if owner == "input":
        changed = value.model_copy(update={"service_failure": original})
        with pytest.raises(AuditIntegrityError, match="extra or missing fields"):
            encode_terminal_p4_input(changed)
    elif owner == "prepared":
        changed = value.model_copy(update={"prepared": value.prepared.model_copy(update={"service_failure": original})})
        with pytest.raises(AuditIntegrityError, match="extra or missing fields"):
            encode_terminal_p4_input(changed)
    else:
        result = terminal_result(value, document)
        changed = (
            result.model_copy(update={"service_failure": original})
            if owner == "result"
            else result.model_copy(update={"receipt": result.receipt.model_copy(update={"service_failure": original})})
        )
        with pytest.raises(AuditIntegrityError, match="extra or missing fields"):
            encode_terminal_p4_result(changed, input_document=document)
    assert decode_terminal_p4_input(document) == value


def test_terminal_recorder_corruption_cannot_be_hidden_by_rehashing_outer_manifest():
    value = terminal_input()
    original = encode_terminal_p4_input(value)
    corrupted = replace(value.recorder, canonical=value.recorder.canonical[:-1] + " ")
    with pytest.raises(AuditIntegrityError):
        encode_terminal_p4_input(value.model_copy(update={"recorder": corrupted}))
    assert decode_terminal_p4_input(original) == value


@pytest.mark.parametrize("projection", ("class", "message"))
def test_terminal_service_invocation_refuses_native_exception_projection(projection):
    original = encode_terminal_p4_input(terminal_input())
    altered = (
        terminal_input(error_class="PrivateOriginal") if projection == "class" else terminal_input(error_message="private original message")
    )
    with pytest.raises(AuditIntegrityError, match="closed value-free service completion"):
        encode_terminal_p4_input(altered)
    assert encode_terminal_p4_input(decode_terminal_p4_input(original)) == original


def test_terminal_recorder_visible_alias_is_checked_before_publication():
    original = encode_terminal_p4_input(terminal_input())
    payload = json.loads(original.canonical)
    recorder = json.loads(payload["recorder"]["canonical"])
    recorder["visible_tool_events"][0]["invocation_index"] = 1
    changed = canonical_json(recorder)
    payload["recorder"]["canonical"] = changed
    payload["recorder"]["reference"]["digest"] = sha256(changed.encode()).hexdigest()
    payload["recorder"]["reference"]["byte_length"] = len(changed.encode())
    with pytest.raises(AuditIntegrityError, match="unique invocation alias"):
        decode_terminal_p4_input(rebound(original, payload))
    assert encode_terminal_p4_input(decode_terminal_p4_input(original)) == original


def test_terminal_unavailable_receipt_cannot_invent_published_rows():
    value = terminal_input()
    original = encode_terminal_p4_input(value)
    unavailable = terminal_result(value, original, unavailable=True)
    encoded = encode_terminal_p4_result(unavailable, input_document=original)
    payload = json.loads(encoded.canonical)
    payload["receipt"]["chat_message_ids"] = [str(UUID(int=1))]
    with pytest.raises(AuditIntegrityError, match="closed contract"):
        decode_terminal_p4_result(rebound(encoded, payload), input_document=original)
    assert decode_terminal_p4_result(encoded, input_document=original) == unavailable


@pytest.mark.parametrize(
    "owner",
    (
        "logical",
        "issue",
        "issue_response",
        "issue_prepared",
        "prior_prefix",
        "source_evidence",
        "response_reference",
        "recorder_reference",
        "effect_frontier",
    ),
)
def test_terminal_nested_model_copy_cannot_omit_original(owner):
    value = terminal_input()
    document = encode_terminal_p4_input(value)
    assert decode_terminal_p4_input(document) == value
    assert encode_terminal_p4_input(decode_terminal_p4_input(document)) == document
    original = RuntimeError("same private original")
    if owner == "logical":
        nested = value.logical.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"logical": nested})
    elif owner == "issue":
        nested = value.issue.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"issue": nested})
    elif owner == "issue_response":
        nested = value.issue.response.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"issue": value.issue.model_copy(update={"response": nested})})
    elif owner == "issue_prepared":
        nested = value.issue.prepared.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"issue": value.issue.model_copy(update={"prepared": nested})})
    elif owner == "prior_prefix":
        nested = value.prior_prefix.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"prior_prefix": nested})
    elif owner == "source_evidence":
        nested = value.source_evidence.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"source_evidence": nested})
    elif owner == "response_reference":
        nested = value.response.reference.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"response": replace(value.response, reference=nested)})
    elif owner == "recorder_reference":
        nested = value.recorder.reference.model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"recorder": replace(value.recorder, reference=nested)})
    else:
        assert owner == "effect_frontier"
        nested = value.effect_frontier[0].model_copy(update={"service_failure": original})
        changed = value.model_copy(update={"effect_frontier": (nested,)})
    assert nested.__dict__["service_failure"] is original
    try:
        with pytest.raises(AuditIntegrityError, match="terminal P4 nested owner"):
            encode_terminal_p4_input(changed)
    finally:
        assert nested.__dict__["service_failure"] is original
        assert decode_terminal_p4_input(document) == value
        assert encode_terminal_p4_input(value) == document


def test_terminal_result_input_model_copy_cannot_omit_original():
    value = terminal_input()
    input_document = encode_terminal_p4_input(value)
    result = terminal_result(value, input_document)
    document = encode_terminal_p4_result(result, input_document=input_document)
    assert decode_terminal_p4_result(document, input_document=input_document) == result
    assert (
        encode_terminal_p4_result(decode_terminal_p4_result(document, input_document=input_document), input_document=input_document)
        == document
    )
    original = RuntimeError("same private original")
    nested = result.input.model_copy(update={"service_failure": original})
    changed = result.model_copy(update={"input": nested})
    assert nested.__dict__["service_failure"] is original
    try:
        with pytest.raises(AuditIntegrityError, match="terminal P4 nested owner"):
            encode_terminal_p4_result(changed, input_document=input_document)
    finally:
        assert nested.__dict__["service_failure"] is original
        assert encode_terminal_p4_result(result, input_document=input_document) == document
        assert decode_terminal_p4_result(document, input_document=input_document) == result


@pytest.mark.parametrize("mode", ("input", "result"))
def test_terminal_document_reference_model_copy_cannot_omit_original(mode):
    value = terminal_input()
    input_document = encode_terminal_p4_input(value)
    result = terminal_result(value, input_document)
    result_document = encode_terminal_p4_result(result, input_document=input_document)
    assert decode_terminal_p4_input(input_document) == value
    assert decode_terminal_p4_result(result_document, input_document=input_document) == result
    assert encode_terminal_p4_input(value) == input_document
    assert encode_terminal_p4_result(result, input_document=input_document) == result_document
    document = input_document if mode == "input" else result_document
    original = RuntimeError("same private original")
    nested = document.reference.model_copy(update={"service_failure": original})
    changed = replace(document, reference=nested)
    assert nested.__dict__["service_failure"] is original
    try:
        with pytest.raises(AuditIntegrityError, match="terminal P4 nested owner"):
            if mode == "input":
                decode_terminal_p4_input(changed)
            else:
                decode_terminal_p4_result(changed, input_document=input_document)
    finally:
        assert nested.__dict__["service_failure"] is original
        assert encode_terminal_p4_input(decode_terminal_p4_input(input_document)) == input_document
        assert (
            encode_terminal_p4_result(
                decode_terminal_p4_result(result_document, input_document=input_document), input_document=input_document
            )
            == result_document
        )


class _LogicalWithOriginal(LogicalRequestBinding):
    service_failure: object


class _IssueWithOriginal(ToolIssueSource):
    service_failure: object


class _PrivateReferenceWithOriginal(PrivateChunkRef):
    service_failure: object


class _EffectWithOriginal(EffectFrontierEntry):
    service_failure: object


class _TerminalReferenceWithOriginal(TerminalP4Reference):
    service_failure: object


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProducerWithOriginal(P4EffectProducerData):
    service_failure: object


@dataclass(frozen=True, slots=True, kw_only=True)
class _ChunkWithOriginal(PrivateChunk):
    service_failure: object


@dataclass(frozen=True, slots=True, kw_only=True)
class _DrainsWithOriginal(RecorderDrainCursors):
    service_failure: object


@pytest.mark.parametrize("owner", ("logical", "issue", "reference", "effect", "producer", "response", "recorder"))
def test_terminal_nested_owned_subclass_cannot_project_away_original(owner):
    value = terminal_input()
    document = encode_terminal_p4_input(value)
    assert decode_terminal_p4_input(document) == value
    assert encode_terminal_p4_input(decode_terminal_p4_input(document)) == document
    original = RuntimeError("same private original")
    if owner == "logical":
        nested = _LogicalWithOriginal(**value.logical.model_dump(), service_failure=original)
        changed = value.model_copy(update={"logical": nested})
    elif owner == "issue":
        nested = _IssueWithOriginal(**value.issue.model_dump(), service_failure=original)
        changed = value.model_copy(update={"issue": nested})
    elif owner == "reference":
        nested = _PrivateReferenceWithOriginal(**value.prior_prefix.model_dump(), service_failure=original)
        changed = value.model_copy(update={"prior_prefix": nested})
    elif owner == "effect":
        nested = _EffectWithOriginal(**value.effect_frontier[0].model_dump(), service_failure=original)
        changed = value.model_copy(update={"effect_frontier": (nested,)})
    elif owner == "producer":
        nested = _ProducerWithOriginal(**asdict(value.producer), service_failure=original)
        changed = value.model_copy(update={"producer": nested})
    elif owner == "response":
        nested = _ChunkWithOriginal(value.response.reference, value.response.canonical, service_failure=original)
        changed = value.model_copy(update={"response": nested})
    else:
        assert owner == "recorder"
        nested = _ChunkWithOriginal(value.recorder.reference, value.recorder.canonical, service_failure=original)
        changed = value.model_copy(update={"recorder": nested})
    assert nested.service_failure is original
    try:
        with pytest.raises(AuditIntegrityError, match="terminal P4 nested owner"):
            encode_terminal_p4_input(changed)
    finally:
        assert nested.service_failure is original
        assert decode_terminal_p4_input(document) == value
        assert encode_terminal_p4_input(value) == document


@pytest.mark.parametrize("owner", ("input", "drained"))
def test_terminal_result_owned_subclass_cannot_project_away_original(owner):
    value = terminal_input()
    input_document = encode_terminal_p4_input(value)
    result = terminal_result(value, input_document)
    document = encode_terminal_p4_result(result, input_document=input_document)
    assert decode_terminal_p4_result(document, input_document=input_document) == result
    assert (
        encode_terminal_p4_result(decode_terminal_p4_result(document, input_document=input_document), input_document=input_document)
        == document
    )
    original = RuntimeError("same private original")
    if owner == "input":
        nested = _TerminalReferenceWithOriginal(**result.input.model_dump(), service_failure=original)
        changed = result.model_copy(update={"input": nested})
    else:
        nested = _DrainsWithOriginal(**asdict(result.drained), service_failure=original)
        changed = result.model_copy(update={"drained": nested})
    assert nested.service_failure is original
    try:
        with pytest.raises(AuditIntegrityError, match="terminal P4 nested owner"):
            encode_terminal_p4_result(changed, input_document=input_document)
    finally:
        assert nested.service_failure is original
        assert decode_terminal_p4_result(document, input_document=input_document) == result
        assert encode_terminal_p4_result(result, input_document=input_document) == document
