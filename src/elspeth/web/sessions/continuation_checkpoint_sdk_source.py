"""Original END checkpoint source proof for the shared SDK terminal core."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy import Connection, Row, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_end_physical import checkpoint_end_physical_custody, require_checkpoint_end_prepared_clock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_paid import (
    PreparedCheckpointProvider,
    V4CheckpointProviderAdmission,
    require_current_checkpoint_dispatched_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_sdk_reservation import prepare_checkpoint_sdk_terminal_hold_plan
from elspeth.web.sessions.continuation_checkpoint_source_replay import (
    CheckpointProviderSourceData,
    read_checkpoint_provider_source_on_connection,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_source import SdkTerminalLiveSource, SdkTerminalSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.time_normalization import restore_utc


def _source_data(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    effect: Row[Any],
    physical_row: Row[Any],
    replay: CheckpointProviderSourceData,
    call: ComposerLLMCall | None,
) -> SdkTerminalSource:
    if replay.phase != "end" or replay.admitted != admitted or replay.identity != identity:
        raise AuditIntegrityError("SDK checkpoint source requires the exact original END dispatch")
    sid, operation = source.session_id, source.operation_id
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one()
    if (
        physical_row.attempt_id != identity.attempt_id
        or physical_row.identity_id != principal
        or (physical_row.session_id, physical_row.source, physical_row.run_id, physical_row.operation_id, physical_row.operation_epoch)
        != (sid, "composer", None, source.session_operation_id, source.session_operation_epoch)
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or (creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token)
        != (source.session_operation_id, source.session_operation_epoch, physical_row.lease_token)
        or creator.claim_attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("SDK END source lost its original principal and physical fence")
    if call is not None and (
        type(call) is not ComposerLLMCall
        or call.call_id != identity.attempt_id
        or call.started_at != restore_utc(physical_row.started_at)
        or call.model_requested != replay.requested_model
        or call.messages_hash != source.source_input_sha256
    ):
        raise AuditIntegrityError("SDK END audit changed its original actual request")
    root = decode_continuation_snapshot(admitted.payload_json)
    origin = decode_continuation_snapshot(replay.origin.payload_json)
    outer = tuple(
        item.effect_id for item in root.effect_frontier if item.effect_id != identity.effect_key and item.disposition == "dispatched"
    )
    original_outer = tuple(item.effect_id for item in origin.effect_frontier if item.disposition == "dispatched")
    if (
        outer != original_outer
        or replay.outer_effect_key not in outer
        or any(item.disposition == "prepared" for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("SDK END changed its complete original unpaid outer vector")
    if effect.disposition == "dispatched":
        expected_outer = "dispatched"
    elif effect.disposition == "sdk_terminal":
        expected_outer = "unknown"
    else:
        raise AuditIntegrityError("SDK END cannot borrow a returned or replacement provider disposition")
    for key in outer:
        row = conn.execute(
            select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key)
        ).one()
        if (
            row.owner not in ("composer_end", "composer_iteration")
            or row.disposition != expected_outer
            or row.provider_attempt_id is not None
            or row.receipt_key is not None
            or row.result_chunk_key is not None
            or (key == replay.outer_effect_key and row.owner != ("composer_end" if replay.kind == "end_review" else "composer_iteration"))
        ):
            raise AuditIntegrityError("SDK END outer vector includes a different paid or completed effect")
    combined = prepare_checkpoint_sdk_terminal_hold_plan(
        identity=identity, admitted=admitted, phase="end", creator_sequence=source.creator_sequence, requested_model=replay.requested_model
    )
    if source.reserved_bytes != combined.reserved_bytes or source.fallback_contract_sha256 != combined.contract_sha256:
        raise AuditIntegrityError("SDK END requires its original SDK-capable H recipe")
    tail = prepare_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, family=SdkTerminalFamily.CHECKPOINT_END
    )
    return SdkTerminalSource(source, admitted, replay.requested_model, tail, replay.env_ref_names, outer)


def require_checkpoint_sdk_source_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> SdkTerminalLiveSource:
    require_original_v4_sdk_failure(observation, custody=custody)
    prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
    if type(prepared) is not PreparedCheckpointProvider or type(admission) is not V4CheckpointProviderAdmission:
        raise AuditIntegrityError("SDK END requires its original nominal prepared request/admission")
    require_checkpoint_end_prepared_clock(prepared)
    physical_source = prepared.physical_source()
    if (
        physical_source is None
        or checkpoint_end_physical_custody(physical_source) is not custody
        or prepared.prepared_source.phase != "end"
        or admission.original_source is not prepared.prepared_source
        or custody.attempt is not admission.attempt
        or observation.attempt is not admission.attempt
        or custody.context is not prepared.running.session_operation_context
        or custody.required_work is not prepared.work
        or prepared.request_sha256 != admission.hold.source_input_sha256
    ):
        raise AuditIntegrityError("SDK END lost its original weak-issued physical custody")
    selected = require_current_checkpoint_dispatched_on_connection(
        conn, prepared.running, admission, allow_cancel=True, allow_expired_segment=True
    )
    source = admission.hold
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    replay = read_checkpoint_provider_source_on_connection(conn, source=source, effect=effect, names=prepared.env_ref_names)
    original = prepared.prepared_source
    if (
        replay.origin != original.source
        or replay.prepared != original.prepared_selected
        or replay.kind != original.kind
        or replay.outer_effect_key != original.outer_effect_key
        or replay.pass_index != original.pass_index
        or replay.retry_ordinal != prepared.retry_ordinal
        or replay.end_input_ref != original.end_input_ref
        or replay.env_ref_names != prepared.env_ref_names
        or replay.requested_model != prepared.requested_model
    ):
        raise AuditIntegrityError("SDK END replay changed original gate input, retry or request")
    data = _source_data(
        conn,
        source=source,
        admitted=selected,
        identity=admission.paid_tail.identity,
        effect=effect,
        physical_row=physical_row,
        replay=replay,
        call=custody.call,
    )
    return SdkTerminalLiveSource(data, prepared.running)


def require_checkpoint_sdk_cold_source_on_connection(
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
    replay = read_checkpoint_provider_source_on_connection(conn, source=source, effect=effect, names=names)
    return _source_data(
        conn, source=source, admitted=admitted, identity=identity, effect=effect, physical_row=physical_row, replay=replay, call=None
    )
