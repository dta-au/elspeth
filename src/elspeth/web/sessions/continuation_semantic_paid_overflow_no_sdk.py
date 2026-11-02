"""Fixed, pre-reserved V4 semantic Advisor refusal after an unentered attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkRefusalPermit, SemanticAdvisorNoSdkRefusalReason
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

_CODEC = "composer.semantic-no-sdk-refusal.v1"
_PLACEHOLDER_RECEIPT = "00000000-0000-4000-8000-000000000000"
_MAX_GUARD_DISPOSITION = "unavailable"


@dataclass(frozen=True, slots=True)
class SemanticNoSdkTailPlan:
    admitted: StoredContinuationSnapshot
    attempt_id: str
    requested_model: str
    maximum_receipt_bytes: int
    event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int
    reserved_bytes: int
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class SemanticNoSdkTailBatch:
    receipt_json: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def fixed_v4_semantic_no_sdk_error(reason: SemanticAdvisorNoSdkRefusalReason) -> ComposerOperationError:
    if type(reason) is not SemanticAdvisorNoSdkRefusalReason:
        raise TypeError("V4 semantic no-SDK error requires its original closed reason")
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


def _receipt_data(
    *,
    admitted: StoredContinuationSnapshot,
    effect_key: str,
    input_sha256: str,
    creator_sequence: int,
    creator_generation: int,
    receipt_key: str,
    reason: SemanticAdvisorNoSdkRefusalReason,
    attempt_id: str,
    requested_model: str,
    guard_audit_id: str | None,
    guard_disposition: str | None,
) -> str:
    root = decode_continuation_snapshot(admitted.payload_json)
    if reason is SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED:
        failure_class = "PromptGuardRefused"
    elif reason is SemanticAdvisorNoSdkRefusalReason.COST_REFUSED:
        failure_class = "CommonProviderCostRefused"
    elif reason is SemanticAdvisorNoSdkRefusalReason.CHARGEABLE_REFUSED:
        failure_class = "ChargeableAdmissionRefused"
    elif reason is SemanticAdvisorNoSdkRefusalReason.PRE_SDK_DEADLINE:
        failure_class = "TimeoutError"
    elif reason is SemanticAdvisorNoSdkRefusalReason.CANCELLED_BEFORE_SDK:
        failure_class = "CancelledError"
    else:
        raise AuditIntegrityError("V4 semantic no-SDK reason is not closed")
    return canonical_json(
        {
            "schema_version": _CODEC,
            "session_id": root.logical.session_id,
            "operation_id": root.logical.operation_id,
            "effect_key": effect_key,
            "input_sha256": input_sha256,
            "creator_sequence": creator_sequence,
            "creator_generation": creator_generation,
            "source_checkpoint_id": admitted.checkpoint_id,
            "source_cursor": admitted.cursor,
            "receipt_key": receipt_key,
            "reason": reason.value,
            "original_failure_class": failure_class,
            "undispatched_attempt_id": attempt_id,
            "undispatched_model": requested_model,
            "guard_audit_id": guard_audit_id,
            "guard_disposition": guard_disposition,
        }
    )


def _shape(
    *,
    admitted: StoredContinuationSnapshot,
    effect_key: str,
    input_sha256: str,
    receipt_key: str,
    receipt_chunk_key: str,
    creator_sequence: int,
    creator_generation: int,
) -> tuple[str, tuple[ContinuationEvent, ContinuationEvent]]:
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = root.frames[-1]
    if (
        type(frame) is not AdvisorFrame
        or frame.phase != "awaiting_response"
        or frame.provider_effect_key != effect_key
        or frame.response is not None
    ):
        raise AuditIntegrityError("V4 semantic no-SDK tail changed its original admitted effect")
    refused = _effect_snapshot(admitted.payload_json, effect_key=effect_key, disposition="refused", receipt_id=receipt_key)
    refused_root = decode_continuation_snapshot(refused)
    advisor = frame.model_copy(
        update={"phase": "response_retained", "response": encode_private_chunk(codec=_CODEC, canonical="{}").reference}
    )
    # The receipt digest varies; the final reference is installed by the caller.
    refused = encode_continuation_snapshot(refused_root.model_copy(update={"frames": (*root.frames[:-1], advisor)}))
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


def prepare_semantic_no_sdk_tail_plan(
    *,
    admitted: StoredContinuationSnapshot,
    attempt_id: str,
    requested_model: str,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
) -> SemanticNoSdkTailPlan:
    if type(admitted) is not StoredContinuationSnapshot or type(attempt_id) is not str or type(requested_model) is not str:
        raise TypeError("V4 semantic no-SDK plan requires original admitted head, attempt and model")
    sizes = tuple(
        len(
            _receipt_data(
                admitted=admitted,
                effect_key=effect_key,
                input_sha256=input_sha256,
                creator_sequence=creator_sequence,
                creator_generation=creator_generation,
                receipt_key=_PLACEHOLDER_RECEIPT,
                reason=reason,
                attempt_id=attempt_id,
                requested_model=requested_model,
                guard_audit_id=_PLACEHOLDER_RECEIPT if reason is SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED else None,
                guard_disposition=_MAX_GUARD_DISPOSITION if reason is SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED else None,
            ).encode("utf-8")
        )
        for reason in SemanticAdvisorNoSdkRefusalReason
    )
    receipt_max = max(sizes)
    snapshot_json, events = _shape(
        admitted=admitted,
        effect_key=effect_key,
        input_sha256=input_sha256,
        receipt_key=_PLACEHOLDER_RECEIPT,
        receipt_chunk_key=f"value:{_CODEC}:{'0' * 64}",
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
    )
    event_bytes = tuple(len(item.payload_json.encode("utf-8")) for item in events)
    terminal_bytes = len(
        encode_continuation_snapshot(decode_continuation_snapshot(snapshot_json).model_copy(update={"cursor": admitted.cursor + 2})).encode(
            "utf-8"
        )
    )
    reserved = receipt_max + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.semantic-v4-no-sdk-tail.v1",
            "admitted_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "attempt_id": attempt_id,
            "requested_model": requested_model,
            "effect_key": effect_key,
            "receipt_max": receipt_max,
            "event_bytes": event_bytes,
            "terminal_snapshot_bytes": terminal_bytes,
            "reserved_bytes": reserved,
        }
    )
    return SemanticNoSdkTailPlan(
        admitted,
        attempt_id,
        requested_model,
        receipt_max,
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
    )


def build_semantic_no_sdk_tail_batch(
    plan: SemanticNoSdkTailPlan,
    *,
    permit: SemanticAdvisorNoSdkRefusalPermit,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    published_at: datetime,
) -> SemanticNoSdkTailBatch:
    from elspeth.web.composer.prompt_guard import PromptGuardRefused

    if type(plan) is not SemanticNoSdkTailPlan or type(permit) is not SemanticAdvisorNoSdkRefusalPermit:
        raise TypeError("V4 semantic no-SDK batch requires original plan and permit")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("V4 semantic no-SDK publication requires aware database time")
    if permit.undispatched_attempt_id != plan.attempt_id or permit.undispatched_model != plan.requested_model:
        raise AuditIntegrityError("V4 semantic no-SDK permit changed its unentered attempt")
    guard = permit.original_failure if type(permit.original_failure) is PromptGuardRefused else None
    receipt_json = _receipt_data(
        admitted=plan.admitted,
        effect_key=effect_key,
        input_sha256=input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        receipt_key=permit.receipt_key,
        reason=permit.reason,
        attempt_id=plan.attempt_id,
        requested_model=plan.requested_model,
        guard_audit_id=guard.audit_id if guard is not None else None,
        guard_disposition=guard.disposition if guard is not None else None,
    )
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes or len(permit.receipt_key) != len(_PLACEHOLDER_RECEIPT):
        raise AuditIntegrityError("V4 semantic no-SDK receipt exceeded its original bound")
    chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    chunk_key = private_chunk_key(chunk)
    snapshot_json, events = _shape(
        admitted=plan.admitted,
        effect_key=effect_key,
        input_sha256=input_sha256,
        receipt_key=permit.receipt_key,
        receipt_chunk_key=chunk_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
    )
    root = decode_continuation_snapshot(snapshot_json)
    advisor = root.frames[-1].model_copy(update={"response": chunk.reference})
    snapshot_json = encode_continuation_snapshot(root.model_copy(update={"frames": (*root.frames[:-1], advisor)}))
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    prospective = (
        ProspectiveContinuationChunk(chunk_key, receipt_json.encode("utf-8"), _CODEC),
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
        raise AuditIntegrityError("V4 semantic no-SDK batch changed its complete original tail shape")
    return SemanticNoSdkTailBatch(receipt_json, chunk_key, append, prospective)


def reconstruct_semantic_no_sdk_tail_batch(
    plan: SemanticNoSdkTailPlan,
    *,
    receipt_json: str,
    input_sha256: str,
    effect_key: str,
    creator_sequence: int,
    creator_generation: int,
    published_at: datetime,
) -> SemanticNoSdkTailBatch:
    """Cold exact tail reconstruction from a separately verified persisted receipt."""
    from elspeth.web.sessions.continuation_semantic_advisor import _decode, _NoSdk

    if type(plan) is not SemanticNoSdkTailPlan or type(receipt_json) is not str:
        raise TypeError("V4 semantic no-SDK cold tail requires its admitted plan and receipt bytes")
    data = _decode(_NoSdk, receipt_json)
    if (
        data.source_checkpoint_id != plan.admitted.checkpoint_id
        or data.source_cursor != plan.admitted.cursor
        or data.undispatched_attempt_id != plan.attempt_id
        or data.undispatched_model != plan.requested_model
        or data.input_sha256 != input_sha256
        or data.effect_key != effect_key
        or data.creator_sequence != creator_sequence
        or data.creator_generation != creator_generation
        or len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes
    ):
        raise AuditIntegrityError("V4 semantic no-SDK cold receipt changed its exact original source")
    chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    chunk_key = private_chunk_key(chunk)
    snapshot_json, events = _shape(
        admitted=plan.admitted,
        effect_key=effect_key,
        input_sha256=input_sha256,
        receipt_key=data.receipt_key,
        receipt_chunk_key=chunk_key,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
    )
    root = decode_continuation_snapshot(snapshot_json)
    advisor = root.frames[-1].model_copy(update={"response": chunk.reference})
    snapshot_json = encode_continuation_snapshot(root.model_copy(update={"frames": (*root.frames[:-1], advisor)}))
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    prospective = (
        ProspectiveContinuationChunk(chunk_key, receipt_json.encode("utf-8"), _CODEC),
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
        raise AuditIntegrityError("V4 semantic no-SDK cold tail exceeded its original H")
    return SemanticNoSdkTailBatch(receipt_json, chunk_key, append, prospective)
