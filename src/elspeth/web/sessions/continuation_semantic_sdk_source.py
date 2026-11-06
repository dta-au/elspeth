"""Original semantic Advisor source proofs for the shared SDK terminal writer."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy import Connection, Row, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.sessions.continuation import (
    PreparedContinuationProvider,
    StoredContinuationSnapshot,
    _owed_continuation_p4_active,
    _require_current_continuation_running,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_source import SdkTerminalLiveSource, SdkTerminalSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan
from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission, _descriptor_data, _effect
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import prepare_semantic_paid_incomplete_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import prepare_semantic_no_sdk_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import prepare_semantic_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_semantic_terminal_reservation import semantic_sdk_terminal_reservation
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
    call: ComposerLLMCall | None,
) -> SdkTerminalSource:
    sid, operation = source.session_id, source.operation_id
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = root.frames[-1]
    original_input = _descriptor_data(conn, sid, operation, effect)
    original, physical_input = original_input.data, original_input.physical_input
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
        type(frame) is not AdvisorFrame
        or frame.phase != "awaiting_response"
        or frame.response is not None
        or frame.provider_effect_key != source.effect_key
        or frame.provider_input_sha256 != source.source_input_sha256
        or original.invocation.kind != "semantic"
        or effect.owner != "provider"
        or effect.effect_key != source.effect_key
        or effect.input_sha256 != source.source_input_sha256
        or effect.creator_sequence != source.creator_sequence
        or effect.creator_generation != source.creator_generation
        or effect.requested_model != physical_input.options["model"]
        or effect.provider_attempt_id != identity.attempt_id
        or physical_row.attempt_id != identity.attempt_id
        or (physical_row.session_id, physical_row.source, physical_row.run_id, physical_row.operation_id, physical_row.operation_epoch)
        != (sid, "composer", None, source.session_operation_id, source.session_operation_epoch)
        or physical_row.identity_id != principal
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or (creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token)
        != (source.session_operation_id, source.session_operation_epoch, physical_row.lease_token)
        or creator.claim_attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("semantic SDK terminal changed original request, creator or physical fence")
    if call is not None:
        wire, options = list(physical_input.messages), physical_input.options
        if (
            type(call) is not ComposerLLMCall
            or call.call_id != identity.attempt_id
            or call.started_at != restore_utc(physical_row.started_at)
            or call.model_requested != options["model"]
            or call.max_completion_tokens_requested != options["max_tokens"]
            or call.temperature != options.get("temperature")
            or call.seed != options.get("seed")
            or call.messages_hash != stable_hash(wire)
            or call.tools_spec_hash is not None
            or call.private_request != capture_provider_context(messages=wire, tools=None)
        ):
            raise AuditIntegrityError("semantic SDK audit changed its exact original physical request")
    paid = prepare_semantic_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    incomplete = prepare_semantic_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    no_sdk = prepare_semantic_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=identity.attempt_id,
        requested_model=effect.requested_model,
        input_sha256=effect.input_sha256,
        effect_key=source.effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
    )
    reserved, contract = semantic_sdk_terminal_reservation(paid, incomplete, no_sdk)
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != contract:
        raise AuditIntegrityError("semantic SDK terminal requires the original SDK-capable hold recipe")
    tail = prepare_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, family=SdkTerminalFamily.SEMANTIC
    )
    return SdkTerminalSource(source, admitted, effect.requested_model, tail)


def require_semantic_sdk_source_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> SdkTerminalLiveSource:
    prepared, admission = custody.continuation_semantic_v4, custody.continuation_semantic_v4_admission
    if type(prepared) is not PreparedContinuationProvider or type(admission) is not V4SemanticProviderAdmission:
        raise AuditIntegrityError("semantic SDK source requires original nominal admission")
    require_original_v4_sdk_failure(observation, custody=custody)
    running = prepared.binding.running
    if (
        custody.context is not running.session_operation_context
        or custody.attempt is not admission.attempt
        or observation.attempt is not admission.attempt
        or prepared.binding.effect_key != admission.hold.effect_key
        or prepared.binding.input_sha256 != admission.hold.source_input_sha256
        or prepared.snapshot.checkpoint_id != admission.hold.source_checkpoint_id
        or prepared.snapshot.cursor != admission.hold.source_cursor
        or admission.tail.admitted != admission.selected
        or admission.incomplete_tail.identity != admission.tail.identity
        or admission.no_sdk_tail.attempt_id != admission.attempt.attempt_id
    ):
        raise AuditIntegrityError("semantic SDK source changed its original custody")
    # The private actual-SDK witness above permits finishing the original paid
    # attempt after cancellation/expiry; it grants no new dispatch or P4 work.
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("semantic SDK finish cannot borrow owed P4 authority")
    selected = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=admission.selected.checkpoint_id,
        expected_cursor=admission.selected.cursor,
    )
    if selected != admission.selected:
        raise AuditIntegrityError("semantic SDK finish changed its original complete admitted root")
    root = decode_continuation_snapshot(selected.payload_json)
    effect = _effect(conn, running, root, prepared.binding.effect_key)
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    data = _source_data(
        conn,
        source=admission.hold,
        admitted=admission.selected,
        identity=admission.tail.identity,
        effect=effect,
        physical_row=physical_row,
        call=custody.call,
    )
    return SdkTerminalLiveSource(data, running)


def require_semantic_sdk_cold_source_on_connection(
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
    return _source_data(conn, source=source, admitted=admitted, identity=identity, effect=effect, physical_row=physical_row, call=None)
