"""Cold reconstruction of one original V4 PRIMARY pre-SDK admission."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.sessions.continuation import PreparedContinuationProvider, _effect_snapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_primary import (
    V4PrimaryProviderAdmission,
    _v4_dispatch_action_projection,
    selected_primary_effect_data_on_connection,
    stored_primary_input_data_on_connection,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import prepare_primary_no_sdk_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import prepare_primary_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import _bounded_bytes, _stored_hold_source
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc


def read_v4_primary_admission_on_connection(
    conn: Connection, *, prepared: PreparedContinuationProvider
) -> V4PrimaryProviderAdmission | None:
    """Return only an exact committed dispatch, or a proved predispatch head."""
    if type(prepared) is not PreparedContinuationProvider:
        raise TypeError("V4 PRIMARY admission read requires its actual prepared source")
    running = prepared.binding.running
    context = running.session_operation_context
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    head = load_snapshot(conn, session_id=sid, operation_id=operation)
    if head == prepared.snapshot:
        _selected, _root, _frame, effect = selected_primary_effect_data_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            checkpoint_id=head.checkpoint_id,
            cursor=head.cursor,
            effect_key=prepared.binding.effect_key,
            input_sha256=prepared.binding.input_sha256,
        )
        prior_hold = _stored_hold_source(conn, session_id=sid, operation_id=operation, effect_key=effect.effect_key)
        if effect.disposition != "prepared" or effect.provider_attempt_id is not None or prior_hold is not None:
            raise AuditIntegrityError("V4 PRIMARY admission read found a partial predispatch source")
        return None
    if head.cursor != prepared.snapshot.cursor + 1:
        raise AuditIntegrityError("V4 PRIMARY admission read cannot classify another current head")
    selected, root, frame, effect = selected_primary_effect_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation,
        checkpoint_id=head.checkpoint_id,
        cursor=head.cursor,
        effect_key=prepared.binding.effect_key,
        input_sha256=prepared.binding.input_sha256,
    )
    source = _stored_hold_source(conn, session_id=sid, operation_id=operation, effect_key=effect.effect_key)
    if source is None:
        raise AuditIntegrityError("V4 PRIMARY admission read lost its original paid H")
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    if (
        selected != head
        or effect.disposition != "dispatched"
        or frame.phase != "awaiting_response"
        or frame.pending_response is not None
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or type(effect.provider_attempt_id) is not str
        or effect.physical_sequence != source.creator_sequence
        or effect.physical_generation != source.creator_generation
        or source.source_checkpoint_id != prepared.snapshot.checkpoint_id
        or source.source_cursor != prepared.snapshot.cursor
        or source.source_snapshot_sha256 != sha256(prepared.snapshot.payload_json.encode("utf-8")).hexdigest()
        or source.source_input_sha256 != prepared.binding.input_sha256
        or hold.state != "active"
        or hold.provider_attempt_id != effect.provider_attempt_id
        or attempt is None
        or attempt.settled_at is not None
        or attempt.ledger_entry_id is not None
        or parent.status != "running"
        or root.policy_hash != source.policy_sha256
    ):
        raise AuditIntegrityError("V4 PRIMARY admission read changed its original pending physical source")
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, effect=effect, frame=frame
    )
    require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=attempt, parent=parent, call=None
    )
    dispatch = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == selected.cursor)
    ).one_or_none()
    if (
        dispatch is None
        or dispatch.tag != "may_dispatch"
        or _bounded_bytes(conn, source, dispatch.payload_chunk_key, "event.v2")
        != canonical_json({"effect_key": effect.effect_key}).encode("utf-8")
    ):
        raise AuditIntegrityError("V4 PRIMARY admission read lost its original dispatch event")
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key=effect.effect_key,
        attempt_id=effect.provider_attempt_id,
        session_operation_id=context.fence.operation_id,
        session_operation_epoch=context.fence.operation_epoch,
        ownership_generation=source.creator_generation,
        lease_sha256=source.lease_sha256,
        policy_sha256=source.policy_sha256,
        source_checkpoint_id=source.source_checkpoint_id,
        source_cursor=source.source_cursor,
        source_snapshot_sha256=source.source_snapshot_sha256,
    )
    paid = prepare_primary_paid_overflow_tail_plan(identity=identity, admitted=selected, creator_sequence=source.creator_sequence)
    refused_projection = _effect_snapshot(
        selected.payload_json,
        effect_key=effect.effect_key,
        disposition="refused",
        receipt_id="00000000-0000-4000-8000-000000000000",
    )
    _refused, action_chunk = _v4_dispatch_action_projection(
        conn, root=root, effect=effect, dispatched_json=refused_projection, disposition="refused"
    )
    no_sdk = prepare_primary_no_sdk_tail_plan(
        admitted=selected,
        attempt_id=effect.provider_attempt_id,
        requested_model=descriptor.model,
        input_sha256=effect.input_sha256,
        effect_key=effect.effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=source.claim_attempt,
        operation_epoch=source.session_operation_epoch,
        action_chunk=action_chunk,
    )
    reserved = max(paid.reserved_bytes, no_sdk.reserved_bytes)
    combined = sha256(
        canonical_json(
            {
                "version": "composer.primary-v4-combined-tail.v1",
                "paid": paid.fallback_contract_sha256,
                "no_sdk": no_sdk.contract_sha256,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined:
        raise AuditIntegrityError("V4 PRIMARY admission read changed its original combined H")
    return V4PrimaryProviderAdmission(
        ProviderAttempt(effect.provider_attempt_id, restore_utc(attempt.started_at)), selected, source, paid, no_sdk
    )
