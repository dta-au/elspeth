"""Original classifier PRIMARY source proofs for the shared SDK terminal core.

These factories replay DATA inside the common SQL sink. A normalized source
cannot independently grant SDK entry, quota settlement or a terminal write.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.composer.provider_gateway import PreparedContinuationPrimaryRequest
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.sessions.continuation import (
    ContinuationProviderBinding,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_classifier_primary import (
    PreparedClassifierPrimary,
    PreparedClassifierPrimaryProvider,
    V4ClassifierProviderAdmission,
    _chain,
    _read_source,
)
from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import (
    ClassifierPaidIncompleteTailPlan,
    prepare_classifier_paid_incomplete_tail_plan,
)
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkTailPlan, prepare_classifier_no_sdk_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import (
    ClassifierPaidOverflowTailPlan,
    prepare_classifier_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_primary import RetainedPrimaryInput, _input_value
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_sdk_source import SdkTerminalLiveSource, SdkTerminalSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical


@dataclass(frozen=True, slots=True)
class _ClassifierSdkReplay:
    source: SdkTerminalSource
    prepared: StoredContinuationSnapshot
    request: RetainedPrimaryInput
    paid: ClassifierPaidOverflowTailPlan
    incomplete: ClassifierPaidIncompleteTailPlan
    no_sdk: ClassifierNoSdkTailPlan


def _replay_classifier_sdk_source(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    effect: Row[Any],
    physical_row: Row[Any],
    parent: Row[Any],
    call: ComposerLLMCall | None,
) -> _ClassifierSdkReplay:
    from elspeth.web.sessions.continuation_classifier_primary_terminal_reservation import classifier_sdk_terminal_reservation

    if (
        type(source) is not ProviderTerminalHoldSource
        or type(admitted) is not StoredContinuationSnapshot
        or type(identity) is not PaidOverflowIdentity
    ):
        raise TypeError("classifier SDK source requires its nominal hold, original head and identity")
    sid, operation, key = source.session_id, source.operation_id, source.effect_key
    contract = _require_historical_hold_source(conn, source)
    _require_hold_fields(conn, source=source, contract=contract)
    original = _read_source(conn, UUID(sid), operation, key)
    prepared = _chain(conn, original, dispatched=False)
    root = decode_continuation_snapshot(admitted.payload_json)
    applied_batch_cut(root.frames)
    matches = tuple(item for item in root.effect_frontier if item.effect_id == key)
    expected_identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key=key,
        attempt_id=physical_row.attempt_id,
        session_operation_id=source.session_operation_id,
        session_operation_epoch=source.session_operation_epoch,
        ownership_generation=source.creator_generation,
        lease_sha256=source.lease_sha256,
        policy_sha256=source.policy_sha256,
        source_checkpoint_id=source.source_checkpoint_id,
        source_cursor=source.source_cursor,
        source_snapshot_sha256=source.source_snapshot_sha256,
    )
    if (
        _chain(conn, original, dispatched=True) != admitted
        or prepared.checkpoint_id != source.source_checkpoint_id
        or prepared.cursor != source.source_cursor
        or sha256(prepared.payload_json.encode("utf-8")).hexdigest() != source.source_snapshot_sha256
        or original.input_sha256 != source.source_input_sha256
        or (original.creator_sequence, original.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (effect.session_id, effect.operation_id, effect.effect_key) != (sid, operation, key)
        or (parent.session_id, parent.operation_id) != (sid, operation)
        or effect.owner != "provider"
        or effect.action_json is not None
        or effect.input_sha256 != original.input_sha256
        or identity != expected_identity
        or root.policy_hash != source.policy_sha256
        or (root.logical.session_id, root.logical.operation_id) != (sid, operation)
        or root.pending_owner_results
        or root.pending_settlement_ids
        or len(matches) != 1
        or matches[0].disposition != "dispatched"
    ):
        raise AuditIntegrityError("classifier SDK source changed its original applied P4, stage, request or creator")
    payload = read_bounded_retained_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"provider-input:{original.input_sha256}",
        allowance=source.allowance_bytes,
    )
    if payload is None or payload.codec != "composer.provider-input.v1":
        raise AuditIntegrityError("classifier SDK source lost its complete original request codec")
    request = _input_value(payload.payload.decode("utf-8", errors="strict"), original.input_sha256)
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=request, effect=effect, physical=physical_row, parent=parent, call=call
    )
    paid = prepare_classifier_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    incomplete = prepare_classifier_paid_incomplete_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
    )
    no_sdk = prepare_classifier_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=identity.attempt_id,
        requested_model=requested_model,
        input_sha256=original.input_sha256,
        effect_key=key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=source.claim_attempt,
        operation_epoch=source.session_operation_epoch,
    )
    reserved, recipe = classifier_sdk_terminal_reservation(paid, incomplete, no_sdk)
    if (source.reserved_bytes, source.fallback_contract_sha256) != (reserved, recipe):
        raise AuditIntegrityError("classifier SDK source requires its original SDK-capable combined hold recipe")
    tail = prepare_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, family=SdkTerminalFamily.CLASSIFIER
    )
    return _ClassifierSdkReplay(
        SdkTerminalSource(source, admitted, requested_model, tail, frozenset(original.env_ref_names)),
        prepared,
        request,
        paid,
        incomplete,
        no_sdk,
    )


def require_classifier_sdk_source_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> SdkTerminalLiveSource:
    require_original_v4_sdk_failure(observation, custody=custody)
    provider, admission = custody.continuation_classifier_v4, custody.continuation_classifier_v4_admission
    if (
        type(provider) is not PreparedClassifierPrimaryProvider
        or type(provider.prepared) is not PreparedClassifierPrimary
        or type(provider.prepared.binding) is not ContinuationProviderBinding
        or type(provider.request) is not PreparedContinuationPrimaryRequest
        or type(admission) is not V4ClassifierProviderAdmission
    ):
        raise AuditIntegrityError("classifier SDK source requires its original nominal classifier admission")
    prepared, binding = provider.prepared, provider.prepared.binding
    running, source = binding.running, admission.hold
    if (
        custody.context is not running.session_operation_context
        or custody.attempt is not admission.attempt
        or observation.attempt is not admission.attempt
        or admission.effect_key != binding.effect_key
        or source.effect_key != binding.effect_key
        or source.source_input_sha256 != binding.input_sha256
        or binding.family != 0
        or binding.effect_key != f"provider:0:{binding.ordinal}"
        or (source.creator_sequence, source.creator_generation, source.claim_attempt)
        != (running.claim.segment_sequence, running.claim.ownership_generation, running.claim.attempt)
    ):
        raise AuditIntegrityError("classifier SDK source changed its original custody, physical attempt or binding")
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    current = require_snapshot_cas(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        expected_checkpoint_id=admission.dispatched.checkpoint_id,
        expected_cursor=admission.dispatched.cursor,
    )
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)).one()
    replay = _replay_classifier_sdk_source(
        conn,
        source=source,
        admitted=current,
        identity=admission.paid_tail.identity,
        effect=effect,
        physical_row=physical_row,
        parent=parent,
        call=custody.call,
    )
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if (
        current != admission.dispatched
        or prepared.snapshot != replay.prepared
        or provider.request.input_sha256 != binding.input_sha256
        or continuation_provider_input_json(provider.request.kwargs) != replay.request.canonical
        or admission.paid_tail != replay.paid
        or admission.incomplete_tail != replay.incomplete
        or admission.no_sdk_tail != replay.no_sdk
        or custody.continuation_classifier_v4 is not provider
        or custody.continuation_classifier_v4_admission is not admission
        or hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
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
        raise AuditIntegrityError("classifier SDK source lost its original unsettled admission and complete request")
    return SdkTerminalLiveSource(replay.source, running)


def require_classifier_sdk_cold_source_on_connection(
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
    # The immutable classifier record supplies its original inventory. Caller
    # names cannot replace that source or grant current credential admission.
    return _replay_classifier_sdk_source(
        conn, source=source, admitted=admitted, identity=identity, effect=effect, physical_row=physical_row, parent=parent, call=None
    ).source
