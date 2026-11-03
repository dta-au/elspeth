"""Original PRIMARY family source proofs for the shared SDK terminal core."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.sessions.continuation import PreparedContinuationProvider, StoredContinuationSnapshot, _effect_snapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_primary import (
    V4PrimaryProviderAdmission,
    _selected_effect,
    _v4_dispatch_action_projection,
    stored_primary_input_data_on_connection,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import prepare_primary_no_sdk_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import prepare_primary_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_primary_sdk_tail import prepare_primary_sdk_tail_plan
from elspeth.web.sessions.continuation_primary_terminal_reservation import primary_terminal_reservation
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
)
from elspeth.web.sessions.continuation_sdk_source import SdkTerminalLiveSource, SdkTerminalSource
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import quota_provider_attempts_table as physical


def require_primary_sdk_source_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> SdkTerminalLiveSource:
    prepared, admission = custody.continuation_primary_v4, custody.continuation_primary_v4_admission
    if type(prepared) is not PreparedContinuationProvider or type(admission) is not V4PrimaryProviderAdmission:
        raise AuditIntegrityError("PRIMARY SDK source requires original nominal admission")
    call = custody.call
    require_original_v4_sdk_failure(observation, custody=custody)
    if (
        custody.continuation_primary_v4_admission is not admission
        or custody.context is not prepared.binding.running.session_operation_context
    ):
        raise AuditIntegrityError("PRIMARY SDK terminal changed its original admission owner")
    if call is not custody.call:
        raise AuditIntegrityError("PRIMARY SDK terminal changed its original audit observation")
    running = prepared.binding.running
    source = admission.hold
    identity = admission.tail.identity
    sdk_tail = prepare_primary_sdk_tail_plan(
        identity=identity, admitted=admission.selected, creator_sequence=admission.tail.creator_sequence
    )
    _, reserved, contract_hash = primary_terminal_reservation(admission.tail, admission.no_sdk_tail)
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != contract_hash:
        raise AuditIntegrityError("PRIMARY SDK terminal requires its original SDK-capable hold recipe")
    sid, operation = source.session_id, source.operation_id
    selected, _root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=admission.selected.checkpoint_id,
        cursor=admission.selected.cursor,
        effect_key=prepared.binding.effect_key,
        input_sha256=prepared.binding.input_sha256,
    )
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, effect=effect, frame=frame
    )
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=call
    )
    if (
        selected != admission.selected
        or hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or observation.attempt is not admission.attempt
        or identity.attempt_id != admission.attempt.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("V4 PRIMARY SDK terminal lacks its original unsettled dispatched source")
    return SdkTerminalLiveSource(SdkTerminalSource(source, selected, requested_model, sdk_tail), running)


def require_primary_sdk_cold_source_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    effect: Row[Any],
    physical_row: Row[Any],
    parent: Row[Any],
    names: frozenset[str],
) -> SdkTerminalSource:
    sid, operation_id, effect_key = source.session_id, source.operation_id, source.effect_key
    session_id = UUID(sid)
    admitted_root = decode_continuation_snapshot(admitted.payload_json)
    frame = admitted_root.frames[-1]
    admitted_matches = tuple(item for item in admitted_root.effect_frontier if item.effect_id == effect_key)
    if (
        len(admitted_root.frames) != 1
        or type(frame) is not ComposeFrame
        or frame.provider_effect_key != effect_key
        or frame.provider_input_sha256 != source.source_input_sha256
        or frame.phase != "awaiting_response"
        or frame.pending_response is not None
        or admitted_root.policy_hash != source.policy_sha256
        or admitted_root.logical.session_id != sid
        or admitted_root.logical.operation_id != operation_id
        or len(admitted_matches) != 1
        or admitted_matches[0].disposition != "dispatched"
    ):
        raise AuditIntegrityError("PRIMARY SDK terminal changed its original admitted result source")
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect=effect, frame=frame
    )
    require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=None
    )
    paid_plan = prepare_primary_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    refused_projection = _effect_snapshot(
        admitted.payload_json,
        effect_key=effect_key,
        disposition="refused",
        receipt_id="00000000-0000-4000-8000-000000000000",
    )
    _refused_json, no_sdk_action_chunk = _v4_dispatch_action_projection(
        conn, root=admitted_root, effect=effect, dispatched_json=refused_projection, disposition="refused"
    )
    no_sdk_plan = prepare_primary_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=identity.attempt_id,
        requested_model=descriptor.model,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=source.claim_attempt,
        operation_epoch=source.session_operation_epoch,
        action_chunk=no_sdk_action_chunk,
    )
    overflow_plan = prepare_primary_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    _incomplete_tail, reserved_bytes, combined_hash = primary_terminal_reservation(overflow_plan, no_sdk_plan)
    if source.reserved_bytes != reserved_bytes or source.fallback_contract_sha256 != combined_hash:
        raise AuditIntegrityError("PRIMARY SDK terminal changed its original combined terminal hold")
    return SdkTerminalSource(source, admitted, descriptor.model, paid_plan)
