"""Bounded zero-use refusal for an original two-frame classifier PRIMARY."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_classifier_no_sdk import (
    ClassifierNoSdkRefusalPermit,
    ClassifierNoSdkRefusalReason,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.coordination.composer_operation_authority import _record_from_row, _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    _undispatched_cancellation_event_id,
    _usage_entry_id,
    cancel_undispatched_provider_attempt_on_connection,
)
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord, composer_operation_result_hash
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _finalize_terminal_rows_after_append,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_classifier_primary import _chain, _read_source
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import prepare_classifier_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
    record_primary_hold_consumed,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import _bounded_bytes, _stored_hold_source
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    _require_historical_hold_source,
    _require_hold_fields,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as segment_attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc

_CODEC = "composer.classifier-primary-no-sdk-refusal.v1"
_PLACEHOLDER_RECEIPT = "00000000-0000-4000-8000-000000000000"


class ClassifierNoSdkReceiptData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    version: Literal[1]
    session_id: str
    operation_id: str
    effect_key: str
    input_sha256: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    operation_epoch: int
    source_checkpoint_id: str
    source_cursor: int
    receipt_key: str
    reason: Literal[
        "preparation_expired",
        "pre_sdk_deadline",
        "chargeable_refused",
        "cost_refused",
        "storage_failure",
        "integrity_refused",
        "cancelled_before_sdk",
        "retained_tail_unbounded",
    ]
    public_error_json: str
    undispatched_attempt_id: str
    undispatched_model: str


@dataclass(frozen=True, slots=True)
class ClassifierNoSdkTailPlan:
    admitted: StoredContinuationSnapshot
    attempt_id: str
    requested_model: str
    maximum_receipt_bytes: int
    event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int
    reserved_bytes: int
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class ClassifierNoSdkTailBatch:
    receipt_json: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


@dataclass(frozen=True, slots=True)
class ClassifierNoSdkRefusalReceipt:
    terminal: ComposerOperationRecord
    refused: StoredContinuationSnapshot
    effect_key: str
    receipt_key: str
    result_chunk_key: str
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


def fixed_classifier_no_sdk_error(reason: ClassifierNoSdkRefusalReason) -> ComposerOperationError:
    if type(reason) is not ClassifierNoSdkRefusalReason:
        raise TypeError("classifier no-SDK error requires its closed reason")
    return ComposerOperationError(
        http_status=503,
        failure_code="http_error",
        error_type="composer_admission_refused",
        body={
            "error_type": "composer_admission_refused",
            "detail": "The provider call was not entered; this operation was closed before SDK dispatch.",
            "reason": reason.value,
            "retryable": False,
        },
        diagnostic_id=None,
    )


def _shape(
    admitted: StoredContinuationSnapshot,
    *,
    receipt_key: str,
    receipt_chunk_key: str,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
) -> tuple[str, tuple[ContinuationEvent, ContinuationEvent]]:
    root = decode_continuation_snapshot(admitted.payload_json)
    applied_batch_cut(root.frames)
    matched = tuple(item for item in root.effect_frontier if item.effect_id == effect_key)
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or root.cursor != admitted.cursor
        or len(matched) != 1
        or matched[0].disposition != "dispatched"
        or root.active_action_json is not None
        or root.active_action_source is not None
        or root.pending_owner_results
        or root.pending_settlement_ids
    ):
        raise AuditIntegrityError("classifier no-SDK tail changed its original dispatched P4 source")
    terminal_json = _effect_snapshot(admitted.payload_json, effect_key=effect_key, disposition="refused", receipt_id=receipt_key)
    tail_events = (
        ContinuationEvent(
            "effect_refused",
            canonical_json(
                {
                    "version": 1,
                    "effect_key": effect_key,
                    "input_sha256": input_sha256,
                    "receipt_key": receipt_key,
                    "result_chunk_key": receipt_chunk_key,
                    "source_disposition": "dispatched",
                }
            ),
        ),
        ContinuationEvent(
            "settled",
            canonical_json(
                {
                    "version": 1,
                    "sequence": creator_sequence,
                    "generation": creator_generation,
                    "status": "failed",
                    "failure_code": "http_error",
                }
            ),
        ),
    )
    return terminal_json, tail_events


def _receipt_data(
    admitted: StoredContinuationSnapshot,
    *,
    attempt_id: str,
    requested_model: str,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    claim_attempt: int,
    operation_epoch: int,
    receipt_key: str,
    reason: ClassifierNoSdkRefusalReason,
) -> ClassifierNoSdkReceiptData:
    root = decode_continuation_snapshot(admitted.payload_json)
    return ClassifierNoSdkReceiptData(
        version=1,
        session_id=str(root.logical.session_id),
        operation_id=root.logical.operation_id,
        effect_key=effect_key,
        input_sha256=input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        claim_attempt=claim_attempt,
        operation_epoch=operation_epoch,
        source_checkpoint_id=admitted.checkpoint_id,
        source_cursor=admitted.cursor,
        receipt_key=receipt_key,
        reason=reason.value,
        public_error_json=canonical_json(fixed_classifier_no_sdk_error(reason).model_dump(mode="json")),
        undispatched_attempt_id=attempt_id,
        undispatched_model=requested_model,
    )


def prepare_classifier_no_sdk_tail_plan(
    *,
    admitted: StoredContinuationSnapshot,
    attempt_id: str,
    requested_model: str,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    claim_attempt: int,
    operation_epoch: int,
) -> ClassifierNoSdkTailPlan:
    if type(admitted) is not StoredContinuationSnapshot or type(attempt_id) is not str or type(requested_model) is not str:
        raise TypeError("classifier no-SDK tail requires nominal admitted source and physical identity")
    maximum_receipt_bytes = max(
        len(
            canonical_json(
                _receipt_data(
                    admitted,
                    attempt_id=attempt_id,
                    requested_model=requested_model,
                    input_sha256=input_sha256,
                    effect_key=effect_key,
                    creator_sequence=creator_sequence,
                    creator_generation=creator_generation,
                    claim_attempt=claim_attempt,
                    operation_epoch=operation_epoch,
                    receipt_key=_PLACEHOLDER_RECEIPT,
                    reason=reason,
                ).model_dump(mode="json")
            ).encode("utf-8")
        )
        for reason in ClassifierNoSdkRefusalReason
    )
    terminal_json, tail_events = _shape(
        admitted,
        receipt_key=_PLACEHOLDER_RECEIPT,
        receipt_chunk_key=f"value:{_CODEC}:{'0' * 64}",
        input_sha256=input_sha256,
        effect_key=effect_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
    )
    event_bytes = tuple(len(event.payload_json.encode("utf-8")) for event in tail_events)
    terminal_bytes = len(
        encode_continuation_snapshot(decode_continuation_snapshot(terminal_json).model_copy(update={"cursor": admitted.cursor + 2})).encode(
            "utf-8"
        )
    )
    reserved = maximum_receipt_bytes + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.classifier-primary-v4-no-sdk-tail.v1",
            "admitted_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "attempt_id": attempt_id,
            "requested_model": requested_model,
            "effect_key": effect_key,
            "receipt_max": maximum_receipt_bytes,
            "event_bytes": event_bytes,
            "terminal_snapshot_bytes": terminal_bytes,
            "reserved_bytes": reserved,
        }
    )
    return ClassifierNoSdkTailPlan(
        admitted,
        attempt_id,
        requested_model,
        maximum_receipt_bytes,
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
    )


def build_classifier_no_sdk_tail_batch(
    plan: ClassifierNoSdkTailPlan,
    *,
    permit: ClassifierNoSdkRefusalPermit,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    claim_attempt: int,
    operation_epoch: int,
    published_at: datetime,
) -> ClassifierNoSdkTailBatch:
    if type(plan) is not ClassifierNoSdkTailPlan or type(permit) is not ClassifierNoSdkRefusalPermit:
        raise TypeError("classifier no-SDK batch requires nominal plan and permit")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("classifier no-SDK publication requires aware database time")
    if plan.attempt_id != permit.admission.attempt.attempt_id or plan.requested_model != permit.admission.no_sdk_tail.requested_model:
        raise AuditIntegrityError("classifier no-SDK permit changed the original unentered physical attempt")
    data = _receipt_data(
        plan.admitted,
        attempt_id=plan.attempt_id,
        requested_model=plan.requested_model,
        input_sha256=input_sha256,
        effect_key=effect_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        claim_attempt=claim_attempt,
        operation_epoch=operation_epoch,
        receipt_key=permit.receipt_key,
        reason=permit.reason,
    )
    receipt_json = canonical_json(data.model_dump(mode="json"))
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes or len(permit.receipt_key) != len(_PLACEHOLDER_RECEIPT):
        raise AuditIntegrityError("classifier no-SDK receipt exceeded its original fixed bound")
    chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    chunk_key = private_chunk_key(chunk)
    terminal_json, tail_events = _shape(
        plan.admitted,
        receipt_key=permit.receipt_key,
        receipt_chunk_key=chunk_key,
        input_sha256=input_sha256,
        effect_key=effect_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
    )
    append = prepare_snapshot_append(plan.admitted, snapshot_json=terminal_json, events=tail_events, published_at=published_at)
    prospective = (
        ProspectiveContinuationChunk(chunk_key, receipt_json.encode("utf-8"), _CODEC),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(tail_events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        tuple(len(event.payload_json.encode("utf-8")) for event in tail_events) != plan.event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("classifier no-SDK batch changed its complete reserved tail shape")
    return ClassifierNoSdkTailBatch(receipt_json, chunk_key, append, prospective)


def refuse_classifier_no_sdk_on_connection(
    conn: Connection,
    permit: ClassifierNoSdkRefusalPermit,
    *,
    append_audit_event: Callable[[str, str, datetime], None],
) -> ClassifierNoSdkRefusalReceipt:
    """Atomically cancel the unentered attempt and close its effect, H and owner."""
    if type(permit) is not ClassifierNoSdkRefusalPermit:
        raise AuditIntegrityError("classifier no-SDK writer requires its actual live permit")
    permit.issuer.require_refusal_permit(permit)
    prepared, admission = permit.prepared, permit.admission
    running = prepared.binding.running
    source = _read_source(conn, running.claim.session_id, running.claim.operation_id, admission.effect_key)
    selected = _chain(conn, source, dispatched=True)
    hold_source = admission.hold
    contract = _require_historical_hold_source(conn, hold_source)
    hold = _require_hold_fields(conn, source=hold_source, contract=contract)
    parent = conn.execute(
        select(jobs).where(jobs.c.session_id == hold_source.session_id, jobs.c.operation_id == hold_source.operation_id)
    ).one()
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == hold_source.session_id,
            effects.c.operation_id == hold_source.operation_id,
            effects.c.effect_key == hold_source.effect_key,
        )
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    session_owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == hold_source.session_id)).scalar_one()
    if (
        selected != admission.dispatched
        or prepared.snapshot != _chain(conn, source, dispatched=False)
        or prepared.binding.running is not permit.issuer.running
        or source.effect_key != prepared.binding.effect_key
        or source.input_sha256 != prepared.binding.input_sha256
        or hold_source.effect_key != source.effect_key
        or hold_source.source_input_sha256 != source.input_sha256
        or hold_source.policy_sha256 != decode_continuation_snapshot(selected.payload_json).policy_hash
        or hold_source.creator_sequence != source.creator_sequence
        or hold_source.creator_generation != source.creator_generation
        or hold_source.claim_attempt != running.claim.attempt
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.input_sha256 != source.input_sha256
        or effect.requested_model != admission.no_sdk_tail.requested_model
        or effect.owner != "provider"
        or effect.action_json is not None
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or physical_row.session_id != hold_source.session_id
        or physical_row.identity_id != session_owner
        or physical_row.source != "composer"
        or physical_row.operation_id != hold_source.session_operation_id
        or physical_row.operation_epoch != hold_source.session_operation_epoch
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("classifier no-SDK writer lost its original physical effect, hold or owner")
    paid = prepare_classifier_paid_overflow_tail_plan(
        identity=admission.paid_tail.identity, admitted=selected, creator_sequence=source.creator_sequence
    )
    no_sdk = prepare_classifier_no_sdk_tail_plan(
        admitted=selected,
        attempt_id=admission.attempt.attempt_id,
        requested_model=effect.requested_model,
        input_sha256=source.input_sha256,
        effect_key=source.effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=running.claim.attempt,
        operation_epoch=hold_source.session_operation_epoch,
    )
    reserved = max(paid.reserved_bytes, no_sdk.reserved_bytes)
    combined_hash = sha256(
        canonical_json(
            {
                "version": "composer.classifier-primary-v4-combined-tail.v1",
                "paid": paid.fallback_contract_sha256,
                "no_sdk": no_sdk.contract_sha256,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    if (
        paid != admission.paid_tail
        or no_sdk != admission.no_sdk_tail
        or paid.identity.attempt_id != admission.attempt.attempt_id
        or hold_source.reserved_bytes != reserved
        or hold_source.fallback_contract_sha256 != combined_hash
    ):
        raise AuditIntegrityError("classifier no-SDK writer changed its combined original tail reserve")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == hold_source.session_id,
            segments.c.operation_id == hold_source.operation_id,
            segments.c.sequence == source.creator_sequence,
        )
    ).one()
    history = conn.execute(
        select(segment_attempts).where(
            segment_attempts.c.session_id == hold_source.session_id,
            segment_attempts.c.operation_id == hold_source.operation_id,
            segment_attempts.c.sequence == source.creator_sequence,
            segment_attempts.c.generation == source.creator_generation,
        )
    ).one()
    if segment.generation != source.creator_generation or segment.state != "running" or history.revoked_at is not None:
        raise AuditIntegrityError("classifier no-SDK writer changed its live segment")
    now = database_now(conn)
    batch = build_classifier_no_sdk_tail_batch(
        no_sdk,
        permit=permit,
        input_sha256=source.input_sha256,
        effect_key=source.effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=running.claim.attempt,
        operation_epoch=hold_source.session_operation_epoch,
        published_at=now,
    )
    result_batch = V4PrimaryResultBatch(hold_source, selected, batch.prospective[:1], batch.append, batch.prospective)
    with admit_v4_primary_result_batch_on_connection(conn, batch=result_batch):
        cancel_undispatched_provider_attempt_on_connection(
            conn,
            session_operation_context=running.session_operation_context,
            attempt_id=admission.attempt.attempt_id,
            requested_model=no_sdk.requested_model,
            append_audit_event=append_audit_event,
        )
        put_chunk(
            conn,
            session_id=hold_source.session_id,
            operation_id=hold_source.operation_id,
            key=batch.receipt_chunk_key,
            payload=batch.receipt_json.encode("utf-8"),
            codec=_CODEC,
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == hold_source.session_id,
                effects.c.operation_id == hold_source.operation_id,
                effects.c.effect_key == hold_source.effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == admission.attempt.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(disposition="refused", receipt_key=permit.receipt_key, result_chunk_key=batch.receipt_chunk_key)
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("classifier no-SDK writer lost its original effect CAS")
        stored = append_complete_snapshot(
            conn,
            session_id=hold_source.session_id,
            operation_id=hold_source.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=batch.append.snapshot_json,
            event_batch=batch.append.events,
            handoff=False,
            prepared_append=batch.append,
        )
        error = fixed_classifier_no_sdk_error(permit.reason)
        _finalize_terminal_rows_after_append(
            conn,
            session_id=hold_source.session_id,
            operation_id=hold_source.operation_id,
            parent=parent,
            segment=segment,
            history=history,
            retained=stored,
            terminal_values=_terminal_values(error, now=now, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=now,
        )
        changed_hold = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == hold_source.session_id,
                holds.c.operation_id == hold_source.operation_id,
                holds.c.effect_key == hold_source.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == admission.attempt.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if changed_hold.rowcount != 1:
            raise AuditIntegrityError("classifier no-SDK writer lost its original active H")
        record_primary_hold_consumed(conn, session_id=hold_source.session_id, operation_id=hold_source.operation_id, source=hold_source)
        terminal = _record_from_row(
            conn.execute(
                select(jobs).where(
                    jobs.c.session_id == hold_source.session_id,
                    jobs.c.operation_id == hold_source.operation_id,
                )
            ).one()
        )
        if terminal.status != "failed" or terminal.journal_cursor != stored.cursor or terminal.checkpoint_id != stored.checkpoint_id:
            raise AuditIntegrityError("classifier no-SDK writer lost its exact failed terminal")
        return ClassifierNoSdkRefusalReceipt(terminal, stored, source.effect_key, permit.receipt_key, batch.receipt_chunk_key)


def read_classifier_no_sdk_refusal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> ClassifierNoSdkRefusalReceipt | None:
    """Cold proof of the exact two-frame refusal, zero-use cohort and consumed H."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(effect_key) is not str:
        raise TypeError("classifier no-SDK cold proof requires nominal SQL identity")
    sid = str(session_id)
    hold_source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)
    if hold_source is None:
        return None
    contract = _require_historical_hold_source(conn, hold_source)
    hold = _require_hold_fields(conn, source=hold_source, contract=contract)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one_or_none()
    if effect is None or effect.disposition != "refused":
        return None
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    admitted_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == hold_source.source_cursor + 2,
        )
    ).one_or_none()
    if admitted_row is None or admitted_row.kind != "transition" or terminal_row.kind != "transition":
        raise AuditIntegrityError("classifier no-SDK cold proof lost original dispatch and terminal heads")
    admitted_text = _bounded_bytes(conn, hold_source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    admitted = StoredContinuationSnapshot(admitted_text, admitted_row.cursor, admitted_row.checkpoint_id)
    source = _read_source(conn, session_id, operation_id, effect_key)
    if _chain(conn, source, dispatched=True) != admitted:
        raise AuditIntegrityError("classifier no-SDK cold proof changed its two-frame original dispatch")
    admitted_root = decode_continuation_snapshot(admitted.payload_json)
    terminal_root = decode_continuation_snapshot(terminal.payload_json)
    applied_batch_cut(admitted_root.frames)
    applied_batch_cut(terminal_root.frames)
    if (
        effect.provider_attempt_id != hold.provider_attempt_id
        or type(effect.provider_attempt_id) is not str
        or type(effect.receipt_key) is not str
        or type(effect.result_chunk_key) is not str
        or effect.action_json is not None
        or effect.response_finished_at is not None
        or effect.input_sha256 != source.input_sha256
        or effect.requested_model is None
        or parent.status != "failed"
        or parent.settled_by != "owner_terminal"
        or parent.journal_cursor != terminal.cursor
        or parent.checkpoint_id != terminal.checkpoint_id
        or hold.state != "consumed"
        or terminal.cursor != admitted.cursor + 2
        or terminal_root.policy_hash != hold_source.policy_sha256
        or terminal_root.logical != admitted_root.logical
        or _bounded_bytes(conn, hold_source, terminal_row.snapshot_chunk_key, "snapshot.v2") != terminal.payload_json.encode("utf-8")
    ):
        raise AuditIntegrityError("classifier no-SDK cold proof changed terminal, effect or H")
    try:
        receipt_text = _bounded_bytes(conn, hold_source, effect.result_chunk_key, _CODEC).decode("utf-8", errors="strict")
        data = ClassifierNoSdkReceiptData.model_validate_json(receipt_text, strict=True)
        reason = ClassifierNoSdkRefusalReason(data.reason)
    except (UnicodeDecodeError, ValueError, ValidationError) as error:
        raise AuditIntegrityError("classifier no-SDK cold proof lost its closed receipt") from error
    no_sdk = prepare_classifier_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=effect.provider_attempt_id,
        requested_model=effect.requested_model,
        input_sha256=source.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=hold_source.claim_attempt,
        operation_epoch=hold_source.session_operation_epoch,
    )
    from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity

    fence_identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation_id,
        effect_key=effect_key,
        attempt_id=effect.provider_attempt_id,
        session_operation_id=hold_source.session_operation_id,
        session_operation_epoch=hold_source.session_operation_epoch,
        ownership_generation=hold_source.creator_generation,
        lease_sha256=hold_source.lease_sha256,
        policy_sha256=hold_source.policy_sha256,
        source_checkpoint_id=hold_source.source_checkpoint_id,
        source_cursor=hold_source.source_cursor,
        source_snapshot_sha256=hold_source.source_snapshot_sha256,
    )
    paid = prepare_classifier_paid_overflow_tail_plan(
        identity=fence_identity,
        admitted=admitted,
        creator_sequence=source.creator_sequence,
    )
    reserved = max(paid.reserved_bytes, no_sdk.reserved_bytes)
    combined_hash = sha256(
        canonical_json(
            {
                "version": "composer.classifier-primary-v4-combined-tail.v1",
                "paid": paid.fallback_contract_sha256,
                "no_sdk": no_sdk.contract_sha256,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    projected, projected_events = _shape(
        admitted,
        receipt_key=data.receipt_key,
        receipt_chunk_key=effect.result_chunk_key,
        input_sha256=source.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
    )
    expected_terminal = encode_continuation_snapshot(decode_continuation_snapshot(projected).model_copy(update={"cursor": terminal.cursor}))
    if (
        canonical_json(data.model_dump(mode="json")) != receipt_text
        or (data.session_id, data.operation_id, data.effect_key, data.input_sha256) != (sid, operation_id, effect_key, source.input_sha256)
        or (data.creator_sequence, data.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (data.claim_attempt, data.operation_epoch) != (hold_source.claim_attempt, hold_source.session_operation_epoch)
        or (data.source_checkpoint_id, data.source_cursor) != (admitted.checkpoint_id, admitted.cursor)
        or data.receipt_key != effect.receipt_key
        or data.undispatched_attempt_id != effect.provider_attempt_id
        or data.undispatched_model != effect.requested_model
        or data.public_error_json != canonical_json(fixed_classifier_no_sdk_error(reason).model_dump(mode="json"))
        or private_chunk_key(encode_private_chunk(codec=_CODEC, canonical=receipt_text)) != effect.result_chunk_key
        or parent.result_json != data.public_error_json
        or type(parent.result_json) is not str
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
        or parent.failure_code != "http_error"
        or hold_source.reserved_bytes != reserved
        or hold_source.fallback_contract_sha256 != combined_hash
        or len(receipt_text.encode("utf-8")) > no_sdk.maximum_receipt_bytes
        or expected_terminal != terminal.payload_json
    ):
        raise AuditIntegrityError("classifier no-SDK cold receipt contradicts its bounded terminal")
    for index, expected in enumerate(projected_events, start=1):
        event = conn.execute(
            select(events).where(
                events.c.session_id == sid,
                events.c.operation_id == operation_id,
                events.c.sequence == admitted.cursor + index,
            )
        ).one_or_none()
        if (
            event is None
            or event.tag != expected.tag
            or _bounded_bytes(conn, hold_source, event.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("classifier no-SDK cold proof lost its exact two-event journal")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    session_owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one_or_none()
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=effect.provider_attempt_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
    audit = conn.execute(
        select(messages).where(messages.c.id == _undispatched_cancellation_event_id(effect.provider_attempt_id))
    ).one_or_none()
    if (
        physical_row is None
        or ledger is None
        or audit is None
        or physical_row.session_id != sid
        or physical_row.identity_id != session_owner
        or physical_row.source != "composer"
        or physical_row.run_id is not None
        or physical_row.operation_id != hold_source.session_operation_id
        or physical_row.operation_epoch != hold_source.session_operation_epoch
        or physical_row.settled_at is None
        or physical_row.ledger_entry_id != entry_id
        or ledger.session_id != sid
        or ledger.source != "composer"
        or ledger.run_id is not None
        or ledger.identity_id != physical_row.identity_id
        or ledger.model != effect.requested_model
        or any(
            value != 0
            for value in (
                ledger.prompt_tokens,
                ledger.completion_tokens,
                ledger.cached_prompt_tokens,
                ledger.reasoning_tokens,
            )
        )
        or audit.session_id != sid
        or audit.role != "audit"
        or audit.writer_principal != "compose_loop"
        or audit.raw_content is not None
        or audit.tool_calls is not None
        or restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)
        or restore_utc(audit.created_at) != restore_utc(physical_row.settled_at)
        or audit.content
        != canonical_json(
            {
                "kind": "provider_attempt.cancelled_before_dispatch",
                "attempt_id": effect.provider_attempt_id,
                "source": "composer",
                "run_id": None,
                "requested_model": effect.requested_model,
                "operation_id": hold_source.session_operation_id,
                "operation_epoch": hold_source.session_operation_epoch,
                "lease_token_sha256": sha256(physical_row.lease_token.encode("utf-8")).hexdigest(),
            }
        )
    ):
        raise AuditIntegrityError("classifier no-SDK cold proof lost its zero-use physical cancellation cohort")
    refusal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == admitted.cursor + 1,
        )
    ).one_or_none()
    if refusal_row is None or refusal_row.kind != "transition":
        raise AuditIntegrityError("classifier no-SDK cold proof lost its refused checkpoint")
    refused_text = _bounded_bytes(conn, hold_source, refusal_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    refused = StoredContinuationSnapshot(refused_text, refusal_row.cursor, refusal_row.checkpoint_id)
    if refused.payload_json != encode_continuation_snapshot(
        decode_continuation_snapshot(projected).model_copy(update={"cursor": refused.cursor})
    ):
        raise AuditIntegrityError("classifier no-SDK cold proof changed its intermediate refused root")
    return ClassifierNoSdkRefusalReceipt(
        _record_from_row(parent),
        terminal,
        effect_key,
        effect.receipt_key,
        effect.result_chunk_key,
    )
