"""Closed two-frame classifier PRIMARY V4 returned-incomplete terminal tail."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    prepare_snapshot_append,
    private_chunk_key,
)
from elspeth.web.sessions.continuation_paid_incomplete import (
    PaidIncompleteReceipt,
    encode_paid_incomplete_receipt,
    maximum_paid_incomplete_receipt_bytes,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk

_CODEC = "composer.paid-incomplete.v1"


@dataclass(frozen=True, slots=True)
class ClassifierPaidIncompleteTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    fallback_contract_sha256: str
    event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class ClassifierPaidIncompleteTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _terminal_shape(
    admitted: StoredContinuationSnapshot, identity: PaidOverflowIdentity, creator_sequence: int, receipt_digest: str
) -> tuple[str, tuple[ContinuationEvent, ContinuationEvent], str, str]:
    root = decode_continuation_snapshot(admitted.payload_json)
    applied_batch_cut(root.frames)
    matching = tuple(item for item in root.effect_frontier if item.effect_id == identity.effect_key)
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or root.logical.session_id != identity.session_id
        or root.logical.operation_id != identity.operation_id
        or root.cursor != admitted.cursor
        or admitted.cursor != identity.source_cursor + 1
        or root.policy_hash != identity.policy_sha256
        or len(matching) != 1
        or matching[0].disposition != "dispatched"
        or root.pending_settlement_ids
        or root.pending_owner_results
    ):
        raise AuditIntegrityError("classifier incomplete tail lost its original dispatched P4 source")
    receipt_key = f"paid-incomplete:{receipt_digest}"
    receipt_chunk_key = f"value:{_CODEC}:{receipt_digest}"
    frontier = tuple(
        EffectFrontierEntry(effect_id=item.effect_id, disposition="paid_incomplete", receipt_id=receipt_key)
        if item.effect_id == identity.effect_key
        else item
        for item in root.effect_frontier
    )
    terminal_json = encode_continuation_snapshot(root.model_copy(update={"effect_frontier": frontier}))
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": "composer_classifier_primary_paid_incomplete",
                    "version": 1,
                    "effect_key": identity.effect_key,
                    "attempt_id": identity.attempt_id,
                    "receipt_key": receipt_key,
                    "result_chunk_key": receipt_chunk_key,
                    "source_checkpoint_id": identity.source_checkpoint_id,
                    "source_cursor": identity.source_cursor,
                }
            ),
        ),
        ContinuationEvent(
            "settled",
            canonical_json(
                {
                    "version": 1,
                    "sequence": creator_sequence,
                    "generation": identity.ownership_generation,
                    "status": "failed",
                    "failure_code": "http_error",
                }
            ),
        ),
    )
    return terminal_json, events, receipt_key, receipt_chunk_key


def prepare_classifier_paid_incomplete_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> ClassifierPaidIncompleteTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("classifier incomplete plan requires original nominal source")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("classifier incomplete plan requires positive creator sequence")
    receipt_max = maximum_paid_incomplete_receipt_bytes(identity, "primary")
    terminal_json, events, _, _ = _terminal_shape(admitted, identity, creator_sequence, "0" * 64)
    event_bytes = tuple(len(event.payload_json.encode("utf-8")) for event in events)
    terminal_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(terminal_json).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = receipt_max + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.classifier-primary-paid-incomplete-tail.v1",
            "identity": identity.model_dump(mode="json"),
            "admitted_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "creator_sequence": creator_sequence,
            "receipt_max": receipt_max,
            "event_bytes": event_bytes,
            "terminal_snapshot_bytes": terminal_bytes,
            "reserved_bytes": reserved,
        }
    )
    return ClassifierPaidIncompleteTailPlan(
        identity,
        admitted,
        creator_sequence,
        receipt_max,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
    )


def build_classifier_paid_incomplete_tail_batch(
    plan: ClassifierPaidIncompleteTailPlan, *, receipt: PaidIncompleteReceipt, published_at: datetime
) -> ClassifierPaidIncompleteTailBatch:
    if type(plan) is not ClassifierPaidIncompleteTailPlan or type(receipt) is not PaidIncompleteReceipt:
        raise TypeError("classifier incomplete batch requires original nominal plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("classifier incomplete publication requires database time")
    if receipt.producer != "primary":
        raise AuditIntegrityError("classifier incomplete receipt changed PRIMARY producer")
    receipt_json = encode_paid_incomplete_receipt(receipt, expected=plan.identity)
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes:
        raise AuditIntegrityError("classifier incomplete receipt exceeded original closed bound")
    digest = sha256(receipt_json.encode("utf-8")).hexdigest()
    snapshot_json, events, receipt_key, receipt_chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest)
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    receipt_chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    if private_chunk_key(receipt_chunk) != receipt_chunk_key:
        raise AuditIntegrityError("classifier incomplete receipt changed its exact canonical key")
    prospective = (
        ProspectiveContinuationChunk(receipt_chunk_key, receipt_json.encode("utf-8"), _CODEC),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        tuple(len(event.payload_json.encode("utf-8")) for event in events) != plan.event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("classifier incomplete batch exceeded original complete tail bound")
    return ClassifierPaidIncompleteTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
