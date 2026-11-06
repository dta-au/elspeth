"""Fixed, bounded zero-use PRIMARY refusal tail for an admitted V4 attempt.

This is a no-SDK terminal and carries no paid-result or cost assertion. The
original physical attempt is cancelled with zero usage in the writer's locked
transaction before this exact tail is consumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalPermit, PrimaryNoSdkRefusalReason
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    _effect_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
)
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk

_CODEC = "composer.primary-no-sdk-refusal.v1"
_PLACEHOLDER_RECEIPT = "00000000-0000-4000-8000-000000000000"
_V4_NO_SDK_REASONS = (
    PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED,
    PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE,
    PrimaryNoSdkRefusalReason.CHARGEABLE_REFUSED,
    PrimaryNoSdkRefusalReason.COST_REFUSED,
    PrimaryNoSdkRefusalReason.STORAGE_FAILURE,
    PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED,
    PrimaryNoSdkRefusalReason.CANCELLED_BEFORE_SDK,
    PrimaryNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED,
)


@dataclass(frozen=True, slots=True)
class PrimaryNoSdkTailPlan:
    admitted: StoredContinuationSnapshot
    attempt_id: str
    requested_model: str
    action_chunk: PrivateChunk | None
    maximum_receipt_bytes: int
    event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int
    reserved_bytes: int
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class PrimaryNoSdkTailBatch:
    receipt_json: str
    receipt_chunk_key: str
    action_chunk: PrivateChunk | None
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def fixed_v4_primary_no_sdk_error(reason: PrimaryNoSdkRefusalReason) -> ComposerOperationError:
    if type(reason) is not PrimaryNoSdkRefusalReason or reason not in _V4_NO_SDK_REASONS:
        raise TypeError("V4 PRIMARY no-SDK error requires its closed original reason")
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
    *,
    admitted: StoredContinuationSnapshot,
    receipt_key: str,
    receipt_chunk_key: str,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    action_chunk: PrivateChunk | None,
) -> tuple[str, tuple[ContinuationEvent, ContinuationEvent]]:
    root = decode_continuation_snapshot(admitted.payload_json)
    if len(root.frames) != 1 or type(root.frames[0]) is not ComposeFrame or root.frames[0].provider_effect_key != effect_key:
        raise AuditIntegrityError("V4 PRIMARY no-SDK tail changed its original admitted effect")
    refused = _effect_snapshot(admitted.payload_json, effect_key=effect_key, disposition="refused", receipt_id=receipt_key)
    if action_chunk is not None:
        refused_root = decode_continuation_snapshot(refused)
        refused = encode_continuation_snapshot(
            refused_root.model_copy(
                update={"semantic_frontier": action_chunk.reference, "active_action_json": None, "active_action_source": None}
            )
        )
    elif root.active_action_json is not None:
        raise AuditIntegrityError("V4 PRIMARY no-SDK tail omitted its prescribed action refusal")
    events = (
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
    return refused, events


def prepare_primary_no_sdk_tail_plan(
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
    action_chunk: PrivateChunk | None,
) -> PrimaryNoSdkTailPlan:
    from elspeth.web.sessions.continuation_primary import _PrimaryNoSdkRefusalData

    if type(admitted) is not StoredContinuationSnapshot or type(attempt_id) is not str or type(requested_model) is not str:
        raise TypeError("V4 PRIMARY no-SDK plan requires original admitted head, attempt and model")
    root = decode_continuation_snapshot(admitted.payload_json)
    sizes = []
    for reason in _V4_NO_SDK_REASONS:
        data = _PrimaryNoSdkRefusalData(
            version=1,
            session_id=root.logical.session_id,
            operation_id=root.logical.operation_id,
            effect_key=effect_key,
            input_sha256=input_sha256,
            creator_sequence=creator_sequence,
            creator_generation=creator_generation,
            claim_attempt=claim_attempt,
            operation_epoch=operation_epoch,
            source_checkpoint_id=admitted.checkpoint_id,
            source_cursor=admitted.cursor,
            source_disposition="dispatched",
            receipt_key=_PLACEHOLDER_RECEIPT,
            reason=reason.value,
            public_error_json=canonical_json(fixed_v4_primary_no_sdk_error(reason).model_dump(mode="json")),
            undispatched_attempt_id=attempt_id,
            undispatched_model=requested_model,
        )
        sizes.append(len(canonical_json(data.model_dump(mode="json", exclude_unset=True)).encode("utf-8")))
    maximum_receipt_bytes = max(sizes)
    snapshot_json, events = _shape(
        admitted=admitted,
        receipt_key=_PLACEHOLDER_RECEIPT,
        receipt_chunk_key=f"value:{_CODEC}:{'0' * 64}",
        input_sha256=input_sha256,
        effect_key=effect_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        action_chunk=action_chunk,
    )
    event_bytes = tuple(len(item.payload_json.encode("utf-8")) for item in events)
    terminal_bytes = len(
        encode_continuation_snapshot(decode_continuation_snapshot(snapshot_json).model_copy(update={"cursor": admitted.cursor + 2})).encode(
            "utf-8"
        )
    )
    action_bytes = 0 if action_chunk is None else len(action_chunk.canonical.encode("utf-8"))
    reserved = maximum_receipt_bytes + action_bytes + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.primary-v4-no-sdk-tail.v1",
            "admitted_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "attempt_id": attempt_id,
            "requested_model": requested_model,
            "effect_key": effect_key,
            "receipt_max": maximum_receipt_bytes,
            "action_digest": None if action_chunk is None else action_chunk.reference.digest,
            "event_bytes": event_bytes,
            "terminal_snapshot_bytes": terminal_bytes,
            "reserved_bytes": reserved,
        }
    )
    return PrimaryNoSdkTailPlan(
        admitted,
        attempt_id,
        requested_model,
        action_chunk,
        maximum_receipt_bytes,
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
    )


def build_primary_no_sdk_tail_batch(
    plan: PrimaryNoSdkTailPlan,
    *,
    permit: PrimaryNoSdkRefusalPermit,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    claim_attempt: int,
    operation_epoch: int,
    published_at: datetime,
) -> PrimaryNoSdkTailBatch:
    from elspeth.web.sessions.continuation_primary import _PrimaryNoSdkRefusalData

    if type(plan) is not PrimaryNoSdkTailPlan or type(permit) is not PrimaryNoSdkRefusalPermit:
        raise TypeError("V4 PRIMARY no-SDK batch requires its nominal original plan and permit")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("V4 PRIMARY no-SDK publication requires aware database time")
    if permit.undispatched_attempt_id != plan.attempt_id or permit.undispatched_model != plan.requested_model:
        raise AuditIntegrityError("V4 PRIMARY no-SDK permit changed the original unentered physical attempt")
    root = decode_continuation_snapshot(plan.admitted.payload_json)
    data = _PrimaryNoSdkRefusalData(
        version=1,
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        effect_key=effect_key,
        input_sha256=input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        claim_attempt=claim_attempt,
        operation_epoch=operation_epoch,
        source_checkpoint_id=plan.admitted.checkpoint_id,
        source_cursor=plan.admitted.cursor,
        source_disposition="dispatched",
        receipt_key=permit.receipt_key,
        reason=permit.reason.value,
        public_error_json=canonical_json(fixed_v4_primary_no_sdk_error(permit.reason).model_dump(mode="json")),
        undispatched_attempt_id=plan.attempt_id,
        undispatched_model=plan.requested_model,
    )
    receipt_json = canonical_json(data.model_dump(mode="json", exclude_unset=True))
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes or len(permit.receipt_key) != len(_PLACEHOLDER_RECEIPT):
        raise AuditIntegrityError("V4 PRIMARY no-SDK receipt exceeded its original fixed bound")
    chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    chunk_key = private_chunk_key(chunk)
    snapshot_json, events = _shape(
        admitted=plan.admitted,
        receipt_key=permit.receipt_key,
        receipt_chunk_key=chunk_key,
        input_sha256=input_sha256,
        effect_key=effect_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        action_chunk=plan.action_chunk,
    )
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    prospective = (
        ProspectiveContinuationChunk(chunk_key, receipt_json.encode("utf-8"), _CODEC),
        *(
            (
                ProspectiveContinuationChunk(
                    private_chunk_key(plan.action_chunk), plan.action_chunk.canonical.encode("utf-8"), plan.action_chunk.reference.codec
                ),
            )
            if plan.action_chunk is not None
            else ()
        ),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", item.payload_json.encode("utf-8"), "event.v2")
            for index, item in enumerate(events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        tuple(len(item.payload_json.encode("utf-8")) for item in events) != plan.event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("V4 PRIMARY no-SDK batch changed its complete original tail shape")
    return PrimaryNoSdkTailBatch(receipt_json, chunk_key, plan.action_chunk, append, prospective)
