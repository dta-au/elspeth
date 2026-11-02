"""Retain a complete malformed END attempt before admitting its one format retry.

The receipt closes only the physical child. The original semantic Review and
its outer END effect remain outstanding; this is neither a model verdict nor
permission to publish or dispatch another call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal

from pydantic import ValidationError, model_validator
from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
from elspeth.web.composer.audit import llm_call_audit_envelope, llm_call_audit_summary
from elspeth.web.composer.continuation import (
    ComposeFrame,
    ContinuationValue,
    EffectFrontierEntry,
    EndFrame,
    PrivateChunkRef,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_audit_buffer import RECORDER_CODEC, decode_recorder_snapshot, validate_recorder_projection
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    _usage_entry_id,
    settle_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    VerifiedCheckpointEndOwed,
    _stored_checkpoint,
    read_v4_checkpoint_end_owed_on_connection,
    read_v4_checkpoint_end_settled_owed_history_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseKind, CheckpointPhaseSource, PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    admit_checkpoint_paid_audit_source_on_connection,
    checkpoint_paid_audit_cohort_for_hold,
    require_checkpoint_paid_private_audit_on_connection,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


class EndFormatRetryMarker(ContinuationValue):
    owner: Literal["composer_checkpoint_end_format_retry"] = "composer_checkpoint_end_format_retry"
    version: Literal[1] = 1
    session_id: str
    operation_id: str
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    owed_checkpoint_id: str
    owed_cursor: int
    original_phase_checkpoint_id: str
    end_input_ref: PrivateChunkRef
    request_sha256: str
    response_chunk_key: str
    receipt_key: str
    recorder: PrivateChunkRef
    pass_index: int
    retry_ordinal: Literal[0] = 0
    next_retry_ordinal: Literal[1] = 1
    end_env_ref_names: tuple[str, ...]
    first_attempt_schema_valid: bool | None
    private_audit_sha256: str

    @model_validator(mode="after")
    def require_exact_retry_and_inventory(self) -> EndFormatRetryMarker:
        if (
            type(self.retry_ordinal) is not int
            or type(self.next_retry_ordinal) is not int
            or self.end_env_ref_names != tuple(sorted(set(self.end_env_ref_names)))
            or any(not name for name in self.end_env_ref_names)
        ):
            raise ValueError("END retry receipt changed its exact ordinal or credential inventory")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedEndFormatRetry:
    marker: EndFormatRetryMarker
    owed: VerifiedCheckpointEndOwed
    selected: StoredContinuationSnapshot


def _retry_recorder(conn: Connection, owed: VerifiedCheckpointEndOwed) -> PrivateChunk:
    root = decode_continuation_snapshot(owed.selected.payload_json)
    compose = root.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END retry lost its one original recorder owner")
    stored = read_bounded_retained_chunk(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        key=f"value:{compose.audit_buffer.codec}:{compose.audit_buffer.digest}",
        allowance=owed.new_hold.allowance_bytes,
    )
    if stored is None or stored.codec != compose.audit_buffer.codec:
        raise AuditIntegrityError("END retry lost its bounded original recorder")
    previous = decode_recorder_snapshot(PrivateChunk(compose.audit_buffer, stored.payload.decode("utf-8", errors="strict")))
    call = decode_retained_physical_audit_call(owed.response)
    if call.call_id != owed.attempt_id or any(item.call_id == call.call_id for item in previous.llm_calls):
        raise AuditIntegrityError("END retry duplicated or replaced its original paid audit")
    # Earlier physical audits retain their original drain semantics. The
    # finalizer independently reproves and deduplicates checkpointed calls.
    canonical, _restored = validate_recorder_projection(replace(previous, llm_calls=(*previous.llm_calls, call)))
    return encode_private_chunk(codec=RECORDER_CODEC, canonical=canonical)


def _retry_root(owed: VerifiedCheckpointEndOwed, receipt: str, recorder: PrivateChunkRef) -> str:
    root = decode_continuation_snapshot(owed.selected.payload_json)
    children = tuple(item for item in root.effect_frontier if item.effect_id == owed.effect_key)
    if (
        type(root.frames[0]) is not ComposeFrame
        or recorder.codec != "composer.audit-buffer.v2"
        or len(children) != 1
        or children[0].disposition != "dispatched"
        or children[0].receipt_id is not None
        or root.pending_settlement_ids != (owed.attempt_id,)
        or root.pending_owner_results
    ):
        raise AuditIntegrityError("END format retry lost its exact outstanding physical child")
    return encode_continuation_snapshot(
        root.model_copy(
            update={
                "frames": (root.frames[0].model_copy(update={"audit_buffer": recorder}), *root.frames[1:]),
                "effect_frontier": tuple(
                    EffectFrontierEntry(effect_id=item.effect_id, disposition="committed", receipt_id=receipt)
                    if item.effect_id == owed.effect_key
                    else item
                    for item in root.effect_frontier
                ),
                "pending_settlement_ids": (),
            }
        )
    )


def retain_end_format_retry_on_connection(
    conn: Connection,
    *,
    service: object,
    prepared: PreparedCheckpointProvider,
    owed_admission: CheckpointEndOwedAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> VerifiedEndFormatRetry:
    """Close the original retained complete attempt atomically, without SDK work."""
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(service) is not SessionServiceImpl
        or not conn.in_transaction()
        or type(prepared) is not PreparedCheckpointProvider
        or type(owed_admission) is not CheckpointEndOwedAdmission
        or type(returned) is not CheckpointReturnedResponseObservation
        or prepared.retry_ordinal != 0
        or prepared.composer._require_sessions_service() is not service
    ):
        raise AuditIntegrityError("END format retry requires its first original paid attempt")
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with service._session_write_lock(conn, sid):
        _require_current_continuation_running(conn, running)
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=owed_admission.original.attempt.attempt_id,
            names=prepared.env_ref_names,
        )
        if (
            owed is None
            or owed.retry_ordinal != 0
            or owed.selected != owed_admission.owed_selected
            or load_snapshot(conn, session_id=sid, operation_id=operation) != owed.selected
            or returned.response != owed.response
            or returned.attempt != owed_admission.original.attempt
        ):
            raise AuditIntegrityError("END format retry replaced its original retained H source")
        call = original_v4_checkpoint_returned_audit_call(
            returned,
            attempt_id=owed.attempt_id,
            request_sha256=prepared.request_sha256,
        )
        if type(returned.message.content) is not str:
            raise AuditIntegrityError("END format retry requires complete original text")
        verdict = _parse_advisor_checkpoint_guidance(returned.message.content, phase="end")
        if verdict.ok or verdict.failure_class != "malformed":
            raise AuditIntegrityError("END format retry cannot replace a usable or missing provider result")
        entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
        if entry is None or entry.call_id != owed.attempt_id:
            raise AuditIntegrityError("END format retry lost its original paid accounting")
        if owed.phase_source != prepared.prepared_source.source or owed.end_input_ref is None:
            raise AuditIntegrityError("END format retry lost its original phase/input closure")
        cohort = checkpoint_paid_audit_cohort_for_hold(call, owed.new_hold)
        recorder = _retry_recorder(conn, owed)
        receipt = f"checkpoint-end-format-retry:{owed.attempt_id}"
        marker = EndFormatRetryMarker(
            session_id=sid,
            operation_id=operation,
            attempt_id=owed.attempt_id,
            effect_key=owed.effect_key,
            outer_effect_key=owed.outer_effect_key,
            owed_checkpoint_id=owed.selected.checkpoint_id,
            owed_cursor=owed.selected.cursor,
            original_phase_checkpoint_id=prepared.prepared_source.source.checkpoint_id,
            end_input_ref=owed.end_input_ref,
            request_sha256=prepared.request_sha256,
            response_chunk_key=private_chunk_key(owed.response),
            receipt_key=receipt,
            recorder=recorder.reference,
            pass_index=owed.pass_index,
            end_env_ref_names=tuple(sorted(prepared.env_ref_names)),
            first_attempt_schema_valid=verdict.response_schema_valid,
            private_audit_sha256=cohort.private_sha256,
        )
        event = ContinuationEvent("checkpoint", canonical_json(marker.model_dump(mode="json")))
        now = database_now(conn)
        append = prepare_snapshot_append(
            owed.selected, snapshot_json=_retry_root(owed, receipt, recorder.reference), events=(event,), published_at=now
        )
        admit_retained_payload_chunks(
            conn,
            session_id=sid,
            operation_id=operation,
            retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
            consuming_hold=owed.new_hold,
            prospective=(
                ProspectiveContinuationChunk(private_chunk_key(recorder), recorder.canonical.encode("utf-8"), recorder.reference.codec),
                ProspectiveContinuationChunk(f"event:{owed.selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
                ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
            ),
        )
        admitted_audit = admit_checkpoint_paid_audit_source_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            owed=owed,
            call=call,
            cohort=cohort,
            names=prepared.env_ref_names,
        )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(recorder),
            payload=recorder.canonical.encode("utf-8"),
            codec=recorder.reference.codec,
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owed.effect_key,
                effects.c.owner == "provider",
                effects.c.provider_attempt_id == owed.attempt_id,
                effects.c.disposition == "dispatched",
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="committed",
                receipt_key=receipt,
                result_chunk_key=marker.response_chunk_key,
                response_finished_at=call.finished_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("END format retry lost its original child CAS")
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=owed.attempt_id, entry=entry)
        write_checkpoint_paid_audit_cohort_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            cohort=cohort,
            observed_at=now,
            admitted=admitted_audit,
        )
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=owed.selected.checkpoint_id,
            expected_cursor=owed.selected.cursor,
            snapshot_json=append.snapshot_json,
            event_batch=append.events,
            handoff=False,
            prepared_append=append,
            consuming_hold=owed.new_hold,
        )
        consume_provider_terminal_hold_after_complete_result(
            conn, source=owed.new_hold, provider_attempt_id=owed.attempt_id, consumed_at=now
        )
        return read_end_format_retry_on_connection(
            conn, session_id=sid, operation_id=operation, selected=stored, names=prepared.env_ref_names
        )


def read_end_format_retry_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    names: frozenset[str] | None,
) -> VerifiedEndFormatRetry:
    """Cold reproof of the exact complete malformed child and next retry head."""
    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if contract.version != "composer.continuation.policy.v4" or contract.allowance is None:
        raise AuditIntegrityError("END format retry requires its original bounded V4 contract")
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == selected.cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        raise AuditIntegrityError("END format retry lost its checkpoint event")
    event_chunk = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=row.payload_chunk_key,
        allowance=contract.allowance,
    )
    if event_chunk is None or event_chunk.codec != "event.v2":
        raise AuditIntegrityError("END format retry lost its bounded original event")
    canonical = event_chunk.payload.decode("utf-8", errors="strict")
    try:
        marker = EndFormatRetryMarker.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("END format retry lost its closed original receipt") from error
    if canonical_json(marker.model_dump(mode="json")) != canonical or (marker.session_id, marker.operation_id) != (
        session_id,
        operation_id,
    ):
        raise AuditIntegrityError("END format retry changed its canonical original identity")
    original_names = frozenset(marker.end_env_ref_names)
    if names is not None and names != original_names:
        raise AuditIntegrityError("END format retry changed its original credential inventory")
    owed = read_v4_checkpoint_end_settled_owed_history_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        attempt_id=marker.attempt_id,
        owed_checkpoint_id=marker.owed_checkpoint_id,
        names=original_names,
        child_disposition="committed",
        expected_retry_ordinal=0,
    )
    if (
        _stored_checkpoint(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            checkpoint_id=selected.checkpoint_id,
            allowance=owed.new_hold.allowance_bytes,
        )
        != selected
    ):
        raise AuditIntegrityError("END format retry replaced its original retained checkpoint")
    call = decode_retained_physical_audit_call(owed.response)
    content = decode_continuation_response(owed.response).message.content
    if type(content) is not str:
        raise AuditIntegrityError("END format retry lost its complete returned text")
    verdict = _parse_advisor_checkpoint_guidance(content, phase="end")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == marker.effect_key,
        )
    ).one_or_none()
    recorder = _retry_recorder(conn, owed)
    stored_recorder = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=private_chunk_key(recorder),
        allowance=contract.allowance,
    )
    if (
        marker.recorder != recorder.reference
        or stored_recorder is None
        or stored_recorder.codec != recorder.reference.codec
        or stored_recorder.payload != recorder.canonical.encode("utf-8")
    ):
        raise AuditIntegrityError("END retry lost its complete ordered paid recorder prefix")
    expected = decode_continuation_snapshot(_retry_root(owed, marker.receipt_key, recorder.reference))
    actual = decode_continuation_snapshot(selected.payload_json)
    if (
        verdict.ok
        or verdict.failure_class != "malformed"
        or verdict.response_schema_valid != marker.first_attempt_schema_valid
        or owed.retry_ordinal != 0
        or owed.effect_key != marker.effect_key
        or owed.outer_effect_key != marker.outer_effect_key
        or owed.pass_index != marker.pass_index
        or owed.request_sha256 != marker.request_sha256
        or owed.phase_source is None
        or owed.phase_source.checkpoint_id != marker.original_phase_checkpoint_id
        or owed.end_input_ref != marker.end_input_ref
        or owed.selected.cursor != marker.owed_cursor
        or selected.cursor != marker.owed_cursor + 1
        or private_chunk_key(owed.response) != marker.response_chunk_key
        or call.call_id != marker.attempt_id
        or marker.receipt_key != f"checkpoint-end-format-retry:{marker.attempt_id}"
        or child is None
        or child.receipt_key != marker.receipt_key
        or child.result_chunk_key != marker.response_chunk_key
        or actual != expected.model_copy(update={"cursor": selected.cursor})
    ):
        raise AuditIntegrityError("END format retry changed its complete paid consequence")
    usage = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
    entry_id = _usage_entry_id(session_id=session_id, source="composer", run_id=None, call_id=marker.attempt_id)
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == marker.attempt_id)).one_or_none()
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    if (
        usage is None
        or entry is None
        or attempt is None
        or entry.session_id != session_id
        or entry.identity_id != attempt.identity_id
        or entry.source != "composer"
        or entry.run_id is not None
        or entry.model != usage.model
        or entry.prompt_tokens != usage.prompt_tokens
        or entry.completion_tokens != usage.completion_tokens
        or entry.cached_prompt_tokens != usage.cached_prompt_tokens
        or entry.reasoning_tokens != usage.reasoning_tokens
        or attempt.ledger_entry_id != entry_id
        or attempt.operation_id != owed.new_hold.session_operation_id
        or attempt.operation_epoch != owed.new_hold.session_operation_epoch
        or type(attempt.lease_token) is not str
        or sha256(attempt.lease_token.encode("utf-8")).hexdigest() != owed.new_hold.lease_sha256
        or usage.recorded_at is None
        or restore_utc(entry.recorded_at) != restore_utc(usage.recorded_at)
    ):
        raise AuditIntegrityError("END format retry changed its original quota ledger")
    require_checkpoint_paid_private_audit_on_connection(
        conn,
        source=owed.new_hold,
        attempt_id=marker.attempt_id,
        private_sha256=marker.private_audit_sha256,
        incomplete=False,
    )
    public_rows = conn.execute(
        select(messages.c.tool_calls, messages.c.content)
        .where(
            messages.c.session_id == session_id,
            messages.c.role == "audit",
            messages.c.writer_principal == "compose_loop",
            messages.c.tool_calls[0]["_kind"].as_string() == "llm_call_audit",
            messages.c.tool_calls[0]["call"]["call_id"].as_string() == marker.attempt_id,
        )
        .limit(2)
    ).all()
    if (
        len(public_rows) != 1
        or public_rows[0].tool_calls != [llm_call_audit_envelope(call)]
        or public_rows[0].content != llm_call_audit_summary(call)
    ):
        raise AuditIntegrityError("END format retry changed its original public paid audit")
    return VerifiedEndFormatRetry(marker, owed, selected)


def read_end_format_retry_phase_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, selected: StoredContinuationSnapshot
) -> CheckpointPhaseSource | None:
    """Reprove retry-ready phase DATA; mint no gate, SDK custody or retry permit."""
    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if contract.version != "composer.continuation.policy.v4" or contract.allowance is None:
        return None
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == selected.cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        return None
    chunk = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=row.payload_chunk_key,
        allowance=contract.allowance,
    )
    if chunk is None or chunk.codec != "event.v2":
        raise AuditIntegrityError("END retry phase lost its bounded checkpoint event")
    value, error = parse_json_strict(chunk.payload.decode("utf-8", errors="strict"))
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("END retry phase lost its owned checkpoint object")
    if value.get("owner") != "composer_checkpoint_end_format_retry":
        return None
    verified = read_end_format_retry_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected, names=None)
    original = verified.owed.phase_source
    if original is None:
        raise AuditIntegrityError("END retry phase lost its original dispatched outer source")
    root = decode_continuation_snapshot(selected.payload_json)
    kind: CheckpointPhaseKind
    if type(root.frames[-1]) is EndFrame:
        kind = "end_review"
    elif type(root.frames[-1]) is ToolBatchFrame:
        kind = "iteration_end"
    else:
        raise AuditIntegrityError("END retry phase changed its original caller frame")
    return CheckpointPhaseSource("end", kind, original, selected, verified.marker.outer_effect_key, (verified.marker.effect_key,))
