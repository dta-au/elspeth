"""Retain genuine EARLY returns and settle their original held physical child.

H is complete returned DATA, never a redispatch permit. The live writer proves
its original current fence and head again; cold readers replay only the finite
E/P/D/H/C chain named by the immutable events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.advisor_audit import (
    AdvisorCheckpointPassAuditEnvelope,
    AdvisorCheckpointPassRecord,
    advisor_checkpoint_pass_audit_envelope,
)
from elspeth.web.composer.advisor_transcript import _wire_event
from elspeth.web.composer.continuation import ComposeFrame, ContinuationValue, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_checkpoint_early_observation import (
    early_returned_checkpoint_verdict,
    require_v4_checkpoint_early_returned_audit,
)
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_checkpoint_projection import (
    CheckpointPassAccounting,
    ProjectedEarlyCheckpoint,
    require_original_early_checkpoint_projection,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, RetainedObservedMessage, encode_transcript_object_json
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_paid import (
    PreparedCheckpointProvider,
    V4CheckpointProviderAdmission,
    require_current_checkpoint_dispatched_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    CheckpointPaidOverflowTailPlan,
    prepare_checkpoint_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_checkpoint_sdk_reservation import prepare_checkpoint_sdk_terminal_hold_plan
from elspeth.web.sessions.continuation_checkpoint_source_replay import (
    CheckpointProviderSourceData,
    read_checkpoint_provider_source_on_connection,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    _require_active_provider_hold,
    _require_hold_fields,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning


class _EarlyReturnEvent(ContinuationValue):
    owner: Literal["composer_checkpoint_early_return"] = "composer_checkpoint_early_return"
    version: Literal[1] = 1
    stage: Literal["complete_return"] = "complete_return"
    session_id: str
    operation_id: str
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    retry_ordinal: Literal[0, 1]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    early_input: PrivateChunkRef
    response: PrivateChunkRef
    dispatched_checkpoint_id: str
    dispatched_cursor: int = Field(ge=0)
    old_hold_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_hold_reserved_bytes: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class EarlyCompleteReturnAdmission:
    original: V4CheckpointProviderAdmission
    owed_selected: StoredContinuationSnapshot
    owed_hold: ProviderTerminalHoldSource
    owed_tail: CheckpointPaidOverflowTailPlan
    response: PrivateChunk


@dataclass(frozen=True, slots=True)
class VerifiedEarlyCompleteReturn:
    session_id: str
    operation_id: str
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    retry_ordinal: Literal[0, 1]
    selected: StoredContinuationSnapshot
    original_selected: StoredContinuationSnapshot
    source: CheckpointProviderSourceData
    response: PrivateChunk
    new_hold: ProviderTerminalHoldSource
    terminal_tail: CheckpointPaidOverflowTailPlan
    old_hold_contract_sha256: str
    old_hold_reserved_bytes: int
    call: ComposerLLMCall


def _stored_checkpoint(
    conn: Connection, *, session_id: str, operation_id: str, checkpoint_id: str, allowance: int
) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    if row is None or row.snapshot_chunk_key != f"snapshot:{checkpoint_id}":
        raise AuditIntegrityError("EARLY return lost its exact historical checkpoint")
    value = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=row.snapshot_chunk_key, allowance=allowance
    )
    if value is None or value.codec != "snapshot.v2":
        raise AuditIntegrityError("EARLY return lost its bounded historical root")
    text = value.payload.decode("utf-8", errors="strict")
    root = decode_continuation_snapshot(text)
    if (root.logical.session_id, root.logical.operation_id, root.cursor) != (session_id, operation_id, row.cursor):
        raise AuditIntegrityError("EARLY return changed its historical root identity")
    return StoredContinuationSnapshot(text, row.cursor, checkpoint_id)


def _return_event(
    conn: Connection, *, session_id: str, operation_id: str, selected: StoredContinuationSnapshot, allowance: int
) -> _EarlyReturnEvent:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == selected.cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint" or row.payload_chunk_key != f"event:{selected.cursor}":
        raise AuditIntegrityError("EARLY return lost its exact complete-return event")
    value = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=row.payload_chunk_key, allowance=allowance
    )
    if value is None or value.codec != "event.v2" or len(value.payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("EARLY return lost its bounded complete-return event")
    text = value.payload.decode("utf-8", errors="strict")
    try:
        event = _EarlyReturnEvent.model_validate_json(text, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("EARLY return changed its closed complete-return event") from error
    if (
        canonical_json(event.model_dump(mode="json")) != text
        or event.session_id != session_id
        or event.operation_id != operation_id
        or event.response.codec != "composer.response.v1"
        or event.early_input.codec != "composer.checkpoint-early-input.v1"
        or selected.cursor != event.dispatched_cursor + 1
    ):
        raise AuditIntegrityError("EARLY return changed its original request or response locator")
    return event


def _hold_identity(hold: ProviderTerminalHoldSource, attempt_id: str) -> PaidOverflowIdentity:
    return PaidOverflowIdentity(
        session_id=hold.session_id,
        operation_id=hold.operation_id,
        effect_key=hold.effect_key,
        attempt_id=attempt_id,
        session_operation_id=hold.session_operation_id,
        session_operation_epoch=hold.session_operation_epoch,
        ownership_generation=hold.creator_generation,
        lease_sha256=hold.lease_sha256,
        policy_sha256=hold.policy_sha256,
        source_checkpoint_id=hold.source_checkpoint_id,
        source_cursor=hold.source_cursor,
        source_snapshot_sha256=hold.source_snapshot_sha256,
    )


def _read_early_complete_return(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
    child_disposition: Literal["dispatched", "committed", "paid_overflow"],
    hold_state: Literal["active", "consumed"],
) -> VerifiedEarlyCompleteReturn:
    if type(selected) is not StoredContinuationSnapshot or type(names) is not frozenset:
        raise AuditIntegrityError("EARLY return requires its exact original source and environment names")
    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if contract.version != "composer.continuation.policy.v4" or type(contract.allowance) is not int:
        raise AuditIntegrityError("EARLY return lost its original bounded V4 policy")
    actual = _stored_checkpoint(
        conn, session_id=session_id, operation_id=operation_id, checkpoint_id=selected.checkpoint_id, allowance=contract.allowance
    )
    if actual != selected:
        raise AuditIntegrityError("EARLY return substituted its exact complete-return head")
    event = _return_event(conn, session_id=session_id, operation_id=operation_id, selected=selected, allowance=contract.allowance)
    original = _stored_checkpoint(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        checkpoint_id=event.dispatched_checkpoint_id,
        allowance=contract.allowance,
    )
    before, root = decode_continuation_snapshot(original.payload_json), decode_continuation_snapshot(selected.payload_json)
    if original.cursor != event.dispatched_cursor or root != before.model_copy(update={"cursor": selected.cursor}):
        raise AuditIntegrityError("EARLY return changed its complete original pending graph")
    hold_row = conn.execute(
        select(holds).where(holds.c.session_id == session_id, holds.c.operation_id == operation_id, holds.c.effect_key == event.effect_key)
    ).one_or_none()
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id, effects.c.operation_id == operation_id, effects.c.effect_key == event.effect_key
        )
    ).one_or_none()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == event.attempt_id)).one_or_none()
    if hold_row is None or effect is None or attempt is None:
        raise AuditIntegrityError("EARLY return lost its original child, physical attempt or hold")
    hold = ProviderTerminalHoldSource(**{name: hold_row._mapping[name] for name in ProviderTerminalHoldSource.__dataclass_fields__})
    if (
        hold_row.state != hold_state
        or hold_row.provider_attempt_id != event.attempt_id
        or hold.source_input_sha256 != event.request_sha256
        or effect.owner != "provider"
        or effect.disposition != child_disposition
        or effect.provider_attempt_id != event.attempt_id
        or effect.input_sha256 != event.request_sha256
        or (effect.receipt_key is None) != (child_disposition == "dispatched")
        or (effect.result_chunk_key is None) != (child_disposition == "dispatched")
        or (attempt.settled_at is None) != (child_disposition == "dispatched")
        or (attempt.ledger_entry_id is None) != (child_disposition == "dispatched")
        or (attempt.session_id, attempt.source, attempt.operation_id, attempt.operation_epoch)
        != (session_id, "composer", hold.session_operation_id, hold.session_operation_epoch)
        or type(attempt.lease_token) is not str
        or sha256(attempt.lease_token.encode("utf-8")).hexdigest() != hold.lease_sha256
    ):
        raise AuditIntegrityError("EARLY return changed its pending original physical custody")
    replay = read_checkpoint_provider_source_on_connection(conn, source=hold, effect=effect, names=names)
    early = replay.early_input
    if (
        replay.phase != "early"
        or replay.kind != "iteration_early"
        or replay.admitted != original
        or replay.pass_index != 0
        or early is None
        or replay.early_input_ref != event.early_input
        or replay.outer_effect_key != event.outer_effect_key
        or replay.retry_ordinal != event.retry_ordinal
        or early.messages_sha256 != event.request_sha256
        or early.request_identity != event.request_identity
        or replay.env_ref_names != names
        or effect.requested_model != early.requested_model
    ):
        raise AuditIntegrityError("EARLY return lacks its original versioned request and inventory proof")
    old_plan = prepare_checkpoint_sdk_terminal_hold_plan(
        identity=replay.identity,
        admitted=original,
        phase="early",
        creator_sequence=hold.creator_sequence,
        requested_model=replay.requested_model,
        historical_contract_sha256=event.old_hold_contract_sha256,
        early_input=replay.early_input,
    )
    tail = prepare_checkpoint_paid_overflow_tail_plan(
        identity=replay.identity, admitted=selected, phase="early", creator_sequence=hold.creator_sequence
    )
    if (
        old_plan.reserved_bytes != event.old_hold_reserved_bytes
        or old_plan.contract_sha256 != event.old_hold_contract_sha256
        or hold.reserved_bytes != tail.reserved_bytes
        or hold.fallback_contract_sha256 != tail.fallback_contract_sha256
        or _require_hold_fields(conn, source=hold, contract=contract).state != hold_state
        or (hold_state == "active" and _require_active_provider_hold(conn, source=hold, contract=contract) != hold.reserved_bytes)
    ):
        raise AuditIntegrityError("EARLY return lost its exact old/new original hold bridge")
    chunk = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=f"value:{event.response.codec}:{event.response.digest}",
        allowance=contract.allowance,
    )
    if (
        chunk is None
        or chunk.codec != "composer.response.v1"
        or len(chunk.payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or len(chunk.payload) != event.response.byte_length
        or sha256(chunk.payload).hexdigest() != event.response.digest
    ):
        raise AuditIntegrityError("EARLY return changed its complete bounded response")
    response = PrivateChunk(event.response, chunk.payload.decode("utf-8", errors="strict"))
    decoded = decode_continuation_response(response)
    call = decode_retained_physical_audit_call(response)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or policy.policy_hash != hold.policy_sha256
        or root.pending_settlement_ids != (event.attempt_id,)
        or call.call_id != event.attempt_id
        or call.messages_hash != event.request_sha256
        or call.model_requested != replay.requested_model
        or call.started_at != restore_utc(attempt.started_at)
        or call.status not in (ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.MALFORMED_RESPONSE)
    ):
        raise AuditIntegrityError("EARLY complete return lacks its actual returned response/audit source")
    require_v4_checkpoint_early_returned_audit(
        call,
        decoded,
        policy=policy,
        attempt_id=event.attempt_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )
    return VerifiedEarlyCompleteReturn(
        session_id,
        operation_id,
        event.attempt_id,
        event.effect_key,
        event.outer_effect_key,
        event.retry_ordinal,
        selected,
        original,
        replay,
        response,
        hold,
        tail,
        event.old_hold_contract_sha256,
        event.old_hold_reserved_bytes,
        call,
    )


def read_early_complete_return_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, selected: StoredContinuationSnapshot, names: frozenset[str]
) -> VerifiedEarlyCompleteReturn:
    """Read only the exact current H with its original unresolved paid attempt."""
    if load_snapshot(conn, session_id=session_id, operation_id=operation_id) != selected:
        raise AuditIntegrityError("EARLY complete return changed its actual current head")
    return _read_early_complete_return(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        selected=selected,
        names=names,
        child_disposition="dispatched",
        hold_state="active",
    )


def read_early_settled_return_history_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, selected: StoredContinuationSnapshot, names: frozenset[str]
) -> VerifiedEarlyCompleteReturn:
    """Prove one exact consumed H; the caller separately proves its C/K/R child."""
    return _read_early_complete_return(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        selected=selected,
        names=names,
        child_disposition="committed",
        hold_state="consumed",
    )


def read_early_overflow_return_history_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
    hold_state: Literal["active", "consumed"],
) -> VerifiedEarlyCompleteReturn:
    """Historical H for its paid-overflow child; never current business authority."""
    kind = conn.execute(
        select(checkpoints.c.kind).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == selected.checkpoint_id,
        )
    ).scalar_one_or_none()
    if kind != "transition":
        raise AuditIntegrityError("EARLY overflow lost its exact original H transition")
    return _read_early_complete_return(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        selected=selected,
        names=names,
        child_disposition="paid_overflow",
        hold_state=hold_state,
    )


def retain_early_complete_return_on_connection(
    conn: Connection,
    *,
    running: ComposerOperationRunning,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> EarlyCompleteReturnAdmission:
    """Retain complete returned DATA and rebind its one H atomically before C."""
    if (
        not conn.in_transaction()
        or type(prepared) is not PreparedCheckpointProvider
        or type(admission) is not V4CheckpointProviderAdmission
        or type(returned) is not CheckpointReturnedResponseObservation
        or prepared.running is not running
        or admission.original_source is not prepared.prepared_source
        or admission.original_source.phase != "early"
        or admission.original_source.early_input_ref is None
        or prepared.publication_binding is not None
        or returned.phase != "early"
        or returned.attempt is not admission.attempt
        or returned.audit_call.messages_hash != prepared.request_sha256
        or returned.audit_call.status not in (ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.MALFORMED_RESPONSE)
        or decode_retained_physical_audit_call(returned.response) != returned.audit_call
        or decode_continuation_response(returned.response).message != returned.message
        or decode_continuation_response(returned.response).tool_batch != returned.tool_batch
    ):
        raise AuditIntegrityError("EARLY H requires its actual original complete returned response")
    require_original_v4_checkpoint_returned_response(
        returned, attempt_id=admission.attempt.attempt_id, request_sha256=prepared.request_sha256
    )
    selected = require_current_checkpoint_dispatched_on_connection(conn, running, admission)
    hold = admission.hold
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == admission.effect_key
        )
    ).one()
    replay = read_checkpoint_provider_source_on_connection(conn, source=hold, effect=effect, names=prepared.env_ref_names)
    if replay.early_input is None or replay.early_input_ref != admission.original_source.early_input_ref:
        raise AuditIntegrityError("EARLY H lost its original versioned request source")
    event = _EarlyReturnEvent(
        session_id=sid,
        operation_id=operation,
        attempt_id=admission.attempt.attempt_id,
        effect_key=admission.effect_key,
        outer_effect_key=admission.original_source.outer_effect_key,
        retry_ordinal=replay.early_input.retry_ordinal,
        request_sha256=prepared.request_sha256,
        request_identity=replay.early_input.request_identity,
        early_input=admission.original_source.early_input_ref,
        response=returned.response.reference,
        dispatched_checkpoint_id=selected.checkpoint_id,
        dispatched_cursor=selected.cursor,
        old_hold_contract_sha256=hold.fallback_contract_sha256,
        old_hold_reserved_bytes=hold.reserved_bytes,
    )
    transition = ContinuationEvent("checkpoint", canonical_json(event.model_dump(mode="json")))
    now = database_now(conn)
    append = prepare_snapshot_append(selected, snapshot_json=selected.payload_json, events=(transition,), published_at=now)
    owed_selected = StoredContinuationSnapshot(append.snapshot_json, selected.cursor + 1, append.checkpoint_id)
    tail = prepare_checkpoint_paid_overflow_tail_plan(
        identity=admission.paid_tail.identity, admitted=owed_selected, phase="early", creator_sequence=hold.creator_sequence
    )
    owed_hold = replace(hold, reserved_bytes=tail.reserved_bytes, fallback_contract_sha256=tail.fallback_contract_sha256)
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    if contract.version != "composer.continuation.policy.v4" or contract.allowance != hold.allowance_bytes:
        raise AuditIntegrityError("EARLY H lost its original retained allowance")
    admit_retained_payload_chunks(
        conn,
        session_id=sid,
        operation_id=operation,
        retained_payload_limit_bytes=hold.allowance_bytes,
        consuming_hold=hold,
        _new_hold_bytes=owed_hold.reserved_bytes,
        prospective=(
            ProspectiveContinuationChunk(
                private_chunk_key(returned.response), returned.response.canonical.encode("utf-8"), returned.response.reference.codec
            ),
            ProspectiveContinuationChunk(f"event:{owed_selected.cursor}", transition.payload_json.encode("utf-8"), "event.v2"),
            ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
        ),
    )
    changed = conn.execute(
        update(holds)
        .where(
            holds.c.session_id == sid,
            holds.c.operation_id == operation,
            holds.c.effect_key == admission.effect_key,
            holds.c.provider_attempt_id == admission.attempt.attempt_id,
            holds.c.state == "active",
            holds.c.reserved_bytes == hold.reserved_bytes,
            holds.c.fallback_contract_sha256 == hold.fallback_contract_sha256,
        )
        .values(reserved_bytes=owed_hold.reserved_bytes, fallback_contract_sha256=owed_hold.fallback_contract_sha256)
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("EARLY H lost its original active hold CAS")
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(returned.response),
        payload=returned.response.canonical.encode("utf-8"),
        codec=returned.response.reference.codec,
    )
    stored = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=append.snapshot_json,
        event_batch=append.events,
        handoff=False,
        prepared_append=append,
    )
    verified = read_early_complete_return_on_connection(
        conn, session_id=sid, operation_id=operation, selected=stored, names=prepared.env_ref_names
    )
    if stored != owed_selected or verified.response != returned.response or verified.new_hold != owed_hold:
        raise AuditIntegrityError("EARLY H failed independent postwrite cold verification")
    return EarlyCompleteReturnAdmission(admission, stored, owed_hold, tail, returned.response)


class _EarlyChildEvent(ContinuationValue):
    owner: Literal["composer_checkpoint_early_child"] = "composer_checkpoint_early_child"
    version: Literal[1] = 1
    stage: Literal["child_complete"] = "child_complete"
    session_id: str
    operation_id: str
    owed_checkpoint_id: str
    owed_cursor: int = Field(ge=0)
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    original_stage_checkpoint_id: str
    response: PrivateChunkRef
    receipt_key: str
    retry_pending: bool
    provider_attempts: Literal[1, 2]
    first_attempt_schema_valid: bool | None
    first_attempt_accepted: bool | None
    format_reprompt_sent: bool
    private_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pass_message_id: str | None


@dataclass(frozen=True, slots=True)
class EarlyCheckpointChildData:
    """Cold-reverified C DATA. The parent writer still owes one atomic K/R."""

    selected: StoredContinuationSnapshot
    original_stage: StoredContinuationSnapshot
    owed: VerifiedEarlyCompleteReturn
    response: PrivateChunk
    retry_pending: bool
    first_attempt_schema_valid: bool | None
    accounting: CheckpointPassAccounting
    names: frozenset[str]
    pass_message_id: str | None


@dataclass(frozen=True, slots=True)
class EarlyCheckpointChildCommitted:
    selected: StoredContinuationSnapshot
    original_stage: StoredContinuationSnapshot
    captured: CapturedContinuationRuntime
    owed: VerifiedEarlyCompleteReturn
    response: PrivateChunk
    retry_pending: bool
    first_attempt_schema_valid: bool | None
    accounting: CheckpointPassAccounting
    names: frozenset[str]
    pass_message_id: str | None


def _held_chunk(conn: Connection, owed: VerifiedEarlyCompleteReturn, reference: PrivateChunkRef) -> PrivateChunk:
    value = read_bounded_retained_chunk(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        key=f"value:{reference.codec}:{reference.digest}",
        allowance=owed.new_hold.allowance_bytes,
    )
    if (
        value is None
        or value.codec != reference.codec
        or len(value.payload) != reference.byte_length
        or sha256(value.payload).hexdigest() != reference.digest
    ):
        raise AuditIntegrityError("EARLY consequence changed a bounded original graph member")
    return PrivateChunk(reference, value.payload.decode("utf-8", errors="strict"))


def _expected_shelf(previous: MessageShelfSnapshot, injection: str | None) -> MessageShelfSnapshot:
    """Reproduce the existing transcript interning using old alias indexes."""
    if injection is None:
        return previous
    # The original shelf stores identity by integer index. A fresh injection
    # object is a fresh alias even if equal text already occurs in that shelf.
    order: list[int] = []
    positions: dict[int, int] = {}
    for old in (*previous.current_indexes, -1, *previous.excluded_indexes, *(entry.message_index for entry in previous.observed)):
        if old not in positions:
            positions[old] = len(order)
            order.append(old)
    wire = {"role": "user", "content": injection}
    event = _wire_event(wire, f"current:{len(previous.observed) + 1}")
    texts = tuple(encode_transcript_object_json(wire) if old == -1 else previous.messages_json[old] for old in order)
    recipes = tuple(None if old == -1 else previous.producer_recipes[old] for old in order) if previous.producer_recipes else ()
    observed = tuple(entry.model_copy(update={"message_index": positions[entry.message_index]}) for entry in previous.observed)
    observed = (
        *observed,
        RetainedObservedMessage(message_index=positions[-1], event_json=encode_transcript_object_json(event), pending_adoption=False),
    )
    return previous.model_copy(
        update={
            "messages_json": texts,
            "producer_recipes": recipes,
            "current_indexes": (*tuple(positions[index] for index in previous.current_indexes), positions[-1]),
            "excluded_indexes": tuple(positions[index] for index in previous.excluded_indexes),
            "observed": observed,
        }
    )


def _expected_child_frames(
    conn: Connection,
    owed: VerifiedEarlyCompleteReturn,
    *,
    retry_pending: bool,
) -> tuple[ComposeFrame, ToolBatchFrame, tuple[PrivateChunk, ...]]:
    from elspeth.web.composer.advisor_checkpoint import early_checkpoint_injection
    from elspeth.web.composer.continuation_audit_buffer import RECORDER_CODEC, decode_recorder_snapshot, validate_recorder_projection
    from elspeth.web.composer.continuation_evidence import admit_turn_evidence, decode_turn_evidence, encode_turn_evidence
    from elspeth.web.composer.continuation_message_shelf import decode_message_shelf, encode_message_shelf
    from elspeth.web.sessions.continuation_selection import load_private_chunks

    root = decode_continuation_snapshot(owed.selected.payload_json)
    if len(root.frames) != 2:
        raise AuditIntegrityError("EARLY consequence lost its original applied Compose/batch pair")
    frame, batch = root.frames[0], root.frames[1]
    if not isinstance(frame, ComposeFrame) or not isinstance(batch, ToolBatchFrame):
        raise AuditIntegrityError("EARLY consequence lost its original applied Compose/batch pair")
    decoded = decode_continuation_response(owed.response)
    verdict = early_returned_checkpoint_verdict(decoded)
    if retry_pending != (not verdict.ok and owed.retry_ordinal == 0):
        raise AuditIntegrityError("EARLY consequence changed its original bounded format retry decision")
    injection = None if retry_pending else early_checkpoint_injection(verdict)
    previous_recorder = decode_recorder_snapshot(_held_chunk(conn, owed, frame.audit_buffer))
    if any(call.call_id == owed.attempt_id for call in previous_recorder.llm_calls):
        raise AuditIntegrityError("EARLY consequence duplicated its original returned physical audit")
    canonical, _restored = validate_recorder_projection(replace(previous_recorder, llm_calls=(*previous_recorder.llm_calls, owed.call)))
    recorder = encode_private_chunk(codec=RECORDER_CODEC, canonical=canonical, env_ref_names=owed.source.env_ref_names)
    previous_shelf = decode_message_shelf(_held_chunk(conn, owed, frame.transcript))
    shelf = _expected_shelf(previous_shelf, injection)
    transcript = encode_message_shelf(shelf, env_ref_names=owed.source.env_ref_names)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json({"messages_json": shelf.messages_json, "current_indexes": shelf.current_indexes}),
        env_ref_names=owed.source.env_ref_names,
    )
    chunks = load_private_chunks(conn, session_id=owed.session_id, operation_id=owed.operation_id, stack=root)
    previous_evidence = decode_turn_evidence(
        _held_chunk(conn, owed, frame.evidence), env_ref_names=owed.source.env_ref_names, source_chunks=chunks
    )
    refreshed = admit_turn_evidence(
        previous_evidence.snapshot.model_copy(update={"recorder": recorder.reference, "transcript": shelf}),
        env_ref_names=owed.source.env_ref_names,
        source_chunks=(*chunks, recorder),
        mandatory_retention=True,
    )
    evidence = encode_turn_evidence(refreshed, env_ref_names=owed.source.env_ref_names, mandatory_retention=True)
    expected = frame.model_copy(
        update={
            "messages": messages.reference,
            "audit_buffer": recorder.reference,
            "transcript": transcript.reference,
            "evidence": evidence.reference,
            "advisor_repair_context_introduced": frame.advisor_repair_context_introduced or injection is not None,
        }
    )
    return expected, batch, (recorder, transcript, messages, evidence)


def _child_event(
    conn: Connection, *, session_id: str, operation_id: str, selected: StoredContinuationSnapshot, allowance: int
) -> _EarlyChildEvent:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id, events.c.operation_id == operation_id, events.c.sequence == selected.cursor
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint" or row.payload_chunk_key != f"event:{selected.cursor}":
        raise AuditIntegrityError("EARLY child lost its exact complete consequence event")
    value = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=row.payload_chunk_key, allowance=allowance
    )
    if value is None or value.codec != "event.v2" or len(value.payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("EARLY child lost its bounded consequence event")
    text = value.payload.decode("utf-8", errors="strict")
    try:
        marker = _EarlyChildEvent.model_validate_json(text, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("EARLY child lost its closed original consequence marker") from error
    if canonical_json(marker.model_dump(mode="json")) != text or (marker.session_id, marker.operation_id) != (session_id, operation_id):
        raise AuditIntegrityError("EARLY child changed its canonical original marker")
    return marker


def read_original_early_child_source_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    names: frozenset[str] | None = None,
) -> EarlyCheckpointChildData:
    """Re-derive C from exact H and actual response; no ancestor search or grant."""
    from elspeth.web.composer.audit import llm_call_audit_envelope
    from elspeth.web.coordination.quota_authority import _usage_entry_id, token_usage_entry_from_llm_call_envelope
    from elspeth.web.sessions.continuation_checkpoint_early_source import resolve_early_original_stage_on_connection
    from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import require_checkpoint_paid_private_audit_on_connection

    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if type(contract.allowance) is not int:
        raise AuditIntegrityError("EARLY child lost its original bounded read contract")
    marker = _child_event(conn, session_id=session_id, operation_id=operation_id, selected=selected, allowance=contract.allowance)
    actual = _stored_checkpoint(
        conn, session_id=session_id, operation_id=operation_id, checkpoint_id=selected.checkpoint_id, allowance=contract.allowance
    )
    owed_selected = _stored_checkpoint(
        conn, session_id=session_id, operation_id=operation_id, checkpoint_id=marker.owed_checkpoint_id, allowance=contract.allowance
    )
    # Names belong to this exact attempt; a later retry can have a different
    # current environment inventory. Recover the original sorted names from
    # its committed request input when the caller reads historical DATA.
    if names is None:
        hold_row = conn.execute(
            select(holds).where(
                holds.c.session_id == session_id, holds.c.operation_id == operation_id, holds.c.effect_key == marker.effect_key
            )
        ).one()
        hold = ProviderTerminalHoldSource(**{name: hold_row._mapping[name] for name in ProviderTerminalHoldSource.__dataclass_fields__})
        effect = conn.execute(
            select(effects).where(
                effects.c.session_id == session_id, effects.c.operation_id == operation_id, effects.c.effect_key == marker.effect_key
            )
        ).one()
        replay = read_checkpoint_provider_source_on_connection(conn, source=hold, effect=effect, names=None)
        names = replay.env_ref_names
    owed = read_early_settled_return_history_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=owed_selected, names=names
    )
    original = resolve_early_original_stage_on_connection(conn, source=owed.source, names=names)
    decoded = decode_continuation_response(owed.response)
    verdict = early_returned_checkpoint_verdict(decoded)
    expected_first_schema = verdict.response_schema_valid if type(decoded.message.content) is str else None
    expected_first_accepted = verdict.ok if type(decoded.message.content) is str else None
    expected_reprompt_sent = False
    if owed.retry_ordinal == 1:
        from elspeth.web.sessions.continuation_checkpoint_early_retry import read_early_format_retry_on_connection

        retry = read_early_format_retry_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=owed.source.origin)
        if retry is None or marker.first_attempt_schema_valid != retry.marker.first_attempt_schema_valid:
            raise AuditIntegrityError("EARLY second consequence changed its original first response accounting")
        expected_reprompt_sent = retry.marker.format_reprompt_required
        expected_first_schema = retry.marker.first_attempt_schema_valid
        expected_first_accepted = retry.child.accounting.first_attempt_accepted
    expected_frame, batch, _expected_chunks = _expected_child_frames(conn, owed, retry_pending=marker.retry_pending)
    root = decode_continuation_snapshot(owed.selected.payload_json)
    frontier = tuple(
        item.model_copy(update={"disposition": "committed", "receipt_id": marker.receipt_key})
        if item.effect_id == owed.effect_key
        else item
        for item in root.effect_frontier
    )
    expected = root.model_copy(
        update={"cursor": selected.cursor, "frames": (expected_frame, batch), "effect_frontier": frontier, "pending_settlement_ids": ()}
    )
    if (
        actual != selected
        or selected.cursor != marker.owed_cursor + 1
        or owed.selected.cursor != marker.owed_cursor
        or marker.attempt_id != owed.attempt_id
        or marker.effect_key != owed.effect_key
        or marker.outer_effect_key != owed.outer_effect_key
        or marker.original_stage_checkpoint_id != original.checkpoint_id
        or marker.response != owed.response.reference
        or marker.receipt_key != f"checkpoint-early-child:{owed.attempt_id}"
        or decode_continuation_snapshot(selected.payload_json) != expected
        or marker.provider_attempts != owed.retry_ordinal + 1
        or marker.format_reprompt_sent != expected_reprompt_sent
        or marker.first_attempt_schema_valid != expected_first_schema
        or marker.first_attempt_accepted != expected_first_accepted
        or (marker.pass_message_id is None) != marker.retry_pending
    ):
        raise AuditIntegrityError("EARLY child changed its exact complete return consequence")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id, effects.c.operation_id == operation_id, effects.c.effect_key == owed.effect_key
        )
    ).one()
    usage = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(owed.call))
    entry_id = _usage_entry_id(session_id=session_id, source="composer", run_id=None, call_id=owed.attempt_id)
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == owed.attempt_id)).one()
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    if (
        child.disposition != "committed"
        or child.receipt_key != marker.receipt_key
        or child.result_chunk_key != private_chunk_key(owed.response)
        or usage is None
        or entry is None
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
        or usage.recorded_at is None
        or restore_utc(entry.recorded_at) != restore_utc(usage.recorded_at)
    ):
        raise AuditIntegrityError("EARLY child changed its actual paid accounting or receipt")
    require_checkpoint_paid_private_audit_on_connection(
        conn,
        source=owed.new_hold,
        attempt_id=owed.attempt_id,
        private_sha256=marker.private_audit_sha256,
        incomplete=owed.call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE,
    )
    accounting = CheckpointPassAccounting(
        0, marker.provider_attempts, marker.first_attempt_schema_valid, marker.first_attempt_accepted, marker.format_reprompt_sent
    )
    _require_early_pass(conn, owed=owed, marker=marker, accounting=accounting)
    return EarlyCheckpointChildData(
        selected,
        original,
        owed,
        owed.response,
        marker.retry_pending,
        marker.first_attempt_schema_valid,
        accounting,
        names,
        marker.pass_message_id,
    )


def _early_pass(
    conn: Connection,
    *,
    owed: VerifiedEarlyCompleteReturn,
    accounting: CheckpointPassAccounting,
) -> tuple[AdvisorCheckpointPassRecord, AdvisorCheckpointPassAuditEnvelope, str]:
    from elspeth.web.composer.advisor_checkpoint import checkpoint_pass_record
    from elspeth.web.composer.continuation_evidence import state_from_evidence_json

    root = decode_continuation_snapshot(owed.selected.payload_json)
    frame = root.frames[0]
    if not isinstance(frame, ComposeFrame):
        raise AuditIntegrityError("EARLY pass lost its original composition")
    verdict = early_returned_checkpoint_verdict(decode_continuation_response(owed.response))
    record = checkpoint_pass_record(
        verdict,
        phase="early",
        source="model",
        state=state_from_evidence_json(_held_chunk(conn, owed, frame.state).canonical),
        pass_index=0,
        provider_attempts=accounting.provider_attempts,
        first_attempt_schema_valid=accounting.first_attempt_schema_valid,
        first_attempt_accepted=accounting.first_attempt_accepted,
        format_reprompt_sent=accounting.format_reprompt_sent,
    )
    envelope = advisor_checkpoint_pass_audit_envelope(record)
    text = json.dumps(envelope, sort_keys=True)
    if len(text.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("EARLY pass exceeds its unchanged complete evidence bound")
    return record, envelope, text


def _require_early_pass(
    conn: Connection,
    *,
    owed: VerifiedEarlyCompleteReturn,
    marker: _EarlyChildEvent,
    accounting: CheckpointPassAccounting,
) -> None:
    if marker.retry_pending:
        if marker.pass_message_id is not None:
            raise AuditIntegrityError("EARLY format retry cannot publish an intermediate pass")
        return
    if marker.pass_message_id is None:
        raise AuditIntegrityError("EARLY final consequence lost its actual model pass")
    _record, envelope, content = _early_pass(conn, owed=owed, accounting=accounting)
    row = conn.execute(
        select(messages).where(messages.c.session_id == owed.session_id, messages.c.id == marker.pass_message_id)
    ).one_or_none()
    published_at = conn.execute(
        select(checkpoints.c.created_at).where(
            checkpoints.c.session_id == owed.session_id,
            checkpoints.c.operation_id == owed.operation_id,
            checkpoints.c.cursor == owed.selected.cursor + 1,
        )
    ).scalar_one()
    if (
        row is None
        or row.role != "audit"
        or row.content != content
        or row.raw_content is not None
        or row.tool_calls != [envelope]
        or row.writer_principal != "compose_loop"
        or row.composition_state_id is not None
        or row.tool_call_id is not None
        or row.parent_assistant_id is not None
        or restore_utc(row.created_at) != restore_utc(published_at)
    ):
        raise AuditIntegrityError("EARLY consequence changed its original final model pass row")


def complete_early_checkpoint_on_connection(
    conn: Connection,
    *,
    service: object,
    running: ComposerOperationRunning,
    prepared: PreparedCheckpointProvider,
    owed_admission: EarlyCompleteReturnAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpoint,
) -> StoredContinuationSnapshot:
    """Settle original H to C then K/R in the caller's one SQL transaction."""
    from elspeth.web.composer.audit import llm_call_audit_envelope
    from elspeth.web.composer.continuation_env_inventory import ContinuationEnvSelection, CurrentContinuationEnvInventory
    from elspeth.web.composer.visible_tool_event import validate_visible_audit_row
    from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection, token_usage_entry_from_llm_call_envelope
    from elspeth.web.sessions.continuation import _require_current_continuation_running
    from elspeth.web.sessions.continuation_checkpoint_early_source import resolve_early_original_stage_on_connection
    from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
        admit_checkpoint_paid_audit_source_on_connection,
        checkpoint_paid_audit_cohort_for_hold,
        write_checkpoint_paid_audit_cohort_on_connection,
    )
    from elspeth.web.sessions.continuation_iteration_return import retain_iteration_early_paid_completed_on_connection
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        not conn.in_transaction()
        or type(service) is not SessionServiceImpl
        or type(prepared) is not PreparedCheckpointProvider
        or prepared.running is not running
        or prepared.composer._require_sessions_service() is not service
        or type(owed_admission) is not EarlyCompleteReturnAdmission
        or owed_admission.original.original_source is not prepared.prepared_source
        or returned.attempt is not owed_admission.original.attempt
    ):
        raise AuditIntegrityError("EARLY completion changed its actual original SQL or paid owner")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    service._assert_session_write_lock_held(conn, sid, caller="early_complete")
    _require_current_continuation_running(conn, running)
    captured = require_original_early_checkpoint_projection(
        projected, prepared=prepared, admission=owed_admission.original, returned=returned
    )
    owed = read_early_complete_return_on_connection(
        conn, session_id=sid, operation_id=operation, selected=owed_admission.owed_selected, names=prepared.env_ref_names
    )
    if owed.new_hold != owed_admission.owed_hold or owed.response != returned.response or owed.response != owed_admission.response:
        raise AuditIntegrityError("EARLY completion replaced its exact original complete H")
    original_stage = resolve_early_original_stage_on_connection(conn, source=owed.source, names=prepared.env_ref_names)
    expected_frame, batch, expected_chunks = _expected_child_frames(conn, owed, retry_pending=projected.retry_pending)
    current_root = decode_continuation_snapshot(owed.selected.payload_json)
    if (
        captured.stack.frames != (expected_frame, batch)
        or captured.stack.model_copy(update={"frames": current_root.frames, "cursor": current_root.cursor}) != current_root
        or any(chunk not in captured.chunks for chunk in expected_chunks)
        or projected.accounting.provider_attempts != owed.retry_ordinal + 1
    ):
        raise AuditIntegrityError("EARLY completion substituted its exact cold-derived projection")
    accounting = projected.accounting
    first_schema = accounting.first_attempt_schema_valid
    first_accepted = accounting.first_attempt_accepted
    entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(owed.call))
    if entry is None or entry.call_id != owed.attempt_id:
        raise AuditIntegrityError("EARLY completion lost its original complete paid accounting")
    cohort = checkpoint_paid_audit_cohort_for_hold(owed.call, owed.new_hold)
    audit_source = admit_checkpoint_paid_audit_source_on_connection(
        conn,
        service=service,
        context=running.session_operation_context,
        owed=owed,
        call=owed.call,
        cohort=cohort,
        names=prepared.env_ref_names,
    )
    pass_id = None if projected.retry_pending else str(uuid4())
    marker = _EarlyChildEvent(
        session_id=sid,
        operation_id=operation,
        owed_checkpoint_id=owed.selected.checkpoint_id,
        owed_cursor=owed.selected.cursor,
        attempt_id=owed.attempt_id,
        effect_key=owed.effect_key,
        outer_effect_key=owed.outer_effect_key,
        original_stage_checkpoint_id=original_stage.checkpoint_id,
        response=owed.response.reference,
        receipt_key=f"checkpoint-early-child:{owed.attempt_id}",
        retry_pending=projected.retry_pending,
        provider_attempts=1 if owed.retry_ordinal == 0 else 2,
        first_attempt_schema_valid=first_schema,
        first_attempt_accepted=first_accepted,
        format_reprompt_sent=accounting.format_reprompt_sent,
        private_audit_sha256=cohort.private_sha256,
        pass_message_id=pass_id,
    )
    frontier = tuple(
        item.model_copy(update={"disposition": "committed", "receipt_id": marker.receipt_key})
        if item.effect_id == owed.effect_key
        else item
        for item in current_root.effect_frontier
    )
    root = current_root.model_copy(update={"frames": (expected_frame, batch), "effect_frontier": frontier, "pending_settlement_ids": ()})
    event = ContinuationEvent("checkpoint", canonical_json(marker.model_dump(mode="json")))
    now = database_now(conn)
    append = prepare_snapshot_append(owed.selected, snapshot_json=encode_continuation_snapshot(root), events=(event,), published_at=now)
    admit_retained_payload_chunks(
        conn,
        session_id=sid,
        operation_id=operation,
        retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
        consuming_hold=owed.new_hold,
        prospective=(
            *(
                ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
                for chunk in captured.chunks
            ),
            ProspectiveContinuationChunk(f"event:{owed.selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
            ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
        ),
    )
    for chunk in captured.chunks:
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == owed.effect_key,
            effects.c.owner == "provider",
            effects.c.disposition == "dispatched",
            effects.c.provider_attempt_id == owed.attempt_id,
            effects.c.receipt_key.is_(None),
            effects.c.result_chunk_key.is_(None),
        )
        .values(
            disposition="committed",
            receipt_key=marker.receipt_key,
            result_chunk_key=private_chunk_key(owed.response),
            response_finished_at=owed.call.finished_at,
        )
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("EARLY completion lost its original dispatched child CAS")
    child_selected = append_complete_snapshot(
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
    settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=owed.attempt_id, entry=entry)
    write_checkpoint_paid_audit_cohort_on_connection(
        conn, service=service, context=running.session_operation_context, cohort=cohort, observed_at=now, admitted=audit_source
    )
    if pass_id is not None:
        record, envelope, content = _early_pass(conn, owed=owed, accounting=accounting)
        if projected.pass_record != record:
            raise AuditIntegrityError("EARLY completion replaced its original final-only pass")
        validate_visible_audit_row(
            envelopes=(envelope,),
            content=content,
            role="audit",
            writer_principal="compose_loop",
            session_id=sid,
            context=running.session_operation_context,
        )
        sequence = service._reserve_sequence_range(conn, sid, count=1)
        inserted = service._insert_chat_message(
            conn,
            session_id=sid,
            role="audit",
            content=content,
            raw_content=None,
            tool_calls=[envelope],
            sequence_no=sequence,
            writer_principal="compose_loop",
            composition_state_id=None,
            tool_call_id=None,
            parent_assistant_id=None,
            created_at=now,
            session_operation_context=running.session_operation_context,
            message_id=pass_id,
        )
        if inserted != pass_id:
            raise AuditIntegrityError("EARLY pass changed its original final row identity")
    elif projected.pass_record is not None or projected.injection is not None:
        raise AuditIntegrityError("EARLY retry cannot publish an intermediate pass or guidance")
    consume_provider_terminal_hold_after_complete_result(conn, source=owed.new_hold, provider_attempt_id=owed.attempt_id, consumed_at=now)
    fresh = read_original_early_child_source_on_connection(
        conn, session_id=sid, operation_id=operation, selected=child_selected, names=prepared.env_ref_names
    )
    child = EarlyCheckpointChildCommitted(
        child_selected,
        original_stage,
        captured,
        fresh.owed,
        fresh.response,
        fresh.retry_pending,
        fresh.first_attempt_schema_valid,
        fresh.accounting,
        fresh.names,
        fresh.pass_message_id,
    )
    inventory = CurrentContinuationEnvInventory(
        running,
        ContinuationEnvSelection(child_selected.checkpoint_id, child_selected.cursor, child_selected.payload_json),
        prepared.env_ref_names,
    )
    # This fixed shared writer appends K or the one typed R in this same SQL
    # transaction. The intermediate consumed-H child can never escape alone.
    return retain_iteration_early_paid_completed_on_connection(
        conn,
        running=running,
        selected=child_selected,
        inventory=inventory,
        captured=captured,
        original_stage=original_stage,
        completion_proof=child,
    )
