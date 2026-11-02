"""Cold exact readback of an ambiguously returned V4 semantic admission SQL cut."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.sessions.continuation import PreparedContinuationProvider, load_snapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission, _descriptor_data, _effect_data
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import (
    prepare_semantic_paid_incomplete_tail_plan,
    semantic_combined_terminal_hold_contract,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import prepare_semantic_no_sdk_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import prepare_semantic_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import _stored_hold_source
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as history_table
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.time_normalization import restore_utc


def read_v4_semantic_admission_on_connection(
    conn: Connection, *, prepared: PreparedContinuationProvider, issuer: SemanticAdvisorNoSdkIssuer
) -> V4SemanticProviderAdmission | None:
    """Return only the original committed attempt/H/head; never infer SDK entry."""
    if type(prepared) is not PreparedContinuationProvider or type(issuer) is not SemanticAdvisorNoSdkIssuer:
        raise TypeError("V4 semantic cold admission requires original prepared and nominal issuer")
    if issuer.original_v4_admission_or_none(prepared) is not None:
        raise AuditIntegrityError("V4 semantic cold admission cannot replace an already projected admission")
    running, binding = prepared.binding.running, prepared.binding
    sid, operation, effect_key = str(running.claim.session_id), running.claim.operation_id, binding.effect_key
    effect_row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == effect_key)
    ).one_or_none()
    source = _stored_hold_source(conn, session_id=sid, operation_id=operation, effect_key=effect_key)
    if source is None:
        if effect_row is None or effect_row.disposition != "prepared":
            raise AuditIntegrityError("V4 semantic admission has dispatched work without its original H")
        return None
    candidate = issuer.original_v4_dispatched_candidate(prepared)
    if candidate is None:
        raise AuditIntegrityError("V4 semantic cold admission lacks its in-transaction original head candidate")
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    admitted = load_snapshot(conn, session_id=sid, operation_id=operation)
    if admitted != candidate:
        raise AuditIntegrityError("V4 semantic cold admission changed its original head bytes or cursor")
    root = decode_continuation_snapshot(admitted.payload_json)
    effect = _effect_data(conn, sid, operation, root, effect_key)
    _descriptor, _invocation, physical_input = _descriptor_data(conn, sid, operation, effect)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == source.creator_sequence
        )
    ).one()
    history = conn.execute(
        select(history_table).where(
            history_table.c.session_id == sid,
            history_table.c.operation_id == operation,
            history_table.c.sequence == source.creator_sequence,
            history_table.c.generation == source.creator_generation,
        )
    ).one()
    if type(effect.provider_attempt_id) is not str:
        raise AuditIntegrityError("V4 semantic cold admission lost original physical attempt locator")
    attempt_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    if (
        effect_row is None
        or effect != effect_row
        or effect.disposition != "dispatched"
        or effect.owner != "provider"
        or effect.input_sha256 != binding.input_sha256
        or effect.requested_model != physical_input.options["model"]
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or effect.creator_sequence != source.creator_sequence
        or effect.creator_generation != source.creator_generation
        or hold.state != "active"
        or hold.provider_attempt_id != effect.provider_attempt_id
        or source.source_checkpoint_id != prepared.snapshot.checkpoint_id
        or source.source_cursor != prepared.snapshot.cursor
        or source.source_snapshot_sha256 != sha256(prepared.snapshot.payload_json.encode("utf-8")).hexdigest()
        or source.source_input_sha256 != binding.input_sha256
        or source.claim_attempt != running.claim.attempt
        or source.creator_sequence != running.claim.segment_sequence
        or source.creator_generation != running.claim.ownership_generation
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or parent.checkpoint_id != admitted.checkpoint_id
        or parent.journal_cursor != admitted.cursor
        or segment.state != "running"
        or segment.generation != source.creator_generation
        or history.claim_attempt != source.claim_attempt
        or history.revoked_at is not None
        or attempt_row is None
        or (attempt_row.session_id, attempt_row.source, attempt_row.run_id, attempt_row.operation_id, attempt_row.operation_epoch)
        != (sid, "composer", None, source.session_operation_id, source.session_operation_epoch)
        or attempt_row.identity_id != principal
        or attempt_row.settled_at is not None
        or attempt_row.ledger_entry_id is not None
        or sha256(attempt_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or (history.session_operation_id, history.session_operation_epoch, history.session_operation_lease_token)
        != (source.session_operation_id, source.session_operation_epoch, attempt_row.lease_token)
    ):
        raise AuditIntegrityError("V4 semantic cold admission changed its original physical, effect, H or owner")
    event_row = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == admitted.cursor)
    ).one_or_none()
    expected_event = canonical_json({"effect_key": effect_key})
    if (
        event_row is None
        or event_row.tag != "may_dispatch"
        or event_row.payload_chunk_key != f"event:{admitted.cursor}"
        or admitted.cursor != prepared.snapshot.cursor + 1
    ):
        raise AuditIntegrityError("V4 semantic cold admission lost its exact may-dispatch event")
    from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import _bounded_bytes

    if _bounded_bytes(conn, source, event_row.payload_chunk_key, "event.v2") != expected_event.encode("utf-8"):
        raise AuditIntegrityError("V4 semantic cold admission changed its may-dispatch bytes")
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key=effect_key,
        attempt_id=effect.provider_attempt_id,
        session_operation_id=source.session_operation_id,
        session_operation_epoch=source.session_operation_epoch,
        ownership_generation=source.creator_generation,
        lease_sha256=source.lease_sha256,
        policy_sha256=source.policy_sha256,
        source_checkpoint_id=source.source_checkpoint_id,
        source_cursor=source.source_cursor,
        source_snapshot_sha256=source.source_snapshot_sha256,
    )
    paid_tail = prepare_semantic_paid_overflow_tail_plan(identity=identity, admitted=candidate, creator_sequence=source.creator_sequence)
    no_sdk_tail = prepare_semantic_no_sdk_tail_plan(
        admitted=candidate,
        attempt_id=effect.provider_attempt_id,
        requested_model=effect.requested_model,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
    )
    incomplete_tail = prepare_semantic_paid_incomplete_tail_plan(
        identity=paid_tail.identity, admitted=candidate, creator_sequence=source.creator_sequence
    )
    reserved, combined_hash = semantic_combined_terminal_hold_contract(paid_tail, incomplete_tail, no_sdk_tail)
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash:
        raise AuditIntegrityError("V4 semantic cold admission changed its original combined fallback")
    attempt = ProviderAttempt(effect.provider_attempt_id, restore_utc(attempt_row.started_at))
    return V4SemanticProviderAdmission(attempt, candidate, source, paid_tail, incomplete_tail, no_sdk_tail)
