"""Closed PRIMARY V4 paid terminal tail and its predispatch byte bound."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
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

_RECEIPT_CODEC = "composer.paid-incomplete.v1"


@dataclass(frozen=True, slots=True)
class PrimaryPaidIncompleteTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    fallback_contract_sha256: str
    terminal_event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class PrimaryPaidIncompleteTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _terminal_shape(
    admitted: StoredContinuationSnapshot, identity: PaidOverflowIdentity, creator_sequence: int, receipt_digest: str
) -> tuple[str, tuple[ContinuationEvent, ContinuationEvent], str, str]:
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = root.frames[-1]
    matches = tuple(item for item in root.effect_frontier if item.effect_id == identity.effect_key)
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or not identity.effect_key.startswith("provider:0:")
        or root.logical.session_id != identity.session_id
        or root.logical.operation_id != identity.operation_id
        or root.cursor != admitted.cursor
        or admitted.cursor != identity.source_cursor + 1
        or root.policy_hash != identity.policy_sha256
        or len(root.frames) != 1
        or type(frame) is not ComposeFrame
        or frame.phase != "awaiting_response"
        or frame.provider_effect_key != identity.effect_key
        or frame.pending_response is not None
        or root.pending_settlement_ids
        or root.pending_owner_results
        or len(matches) != 1
        or matches[0].disposition != "dispatched"
    ):
        raise AuditIntegrityError("PRIMARY terminal tail lost its one dispatched original source")
    receipt_key = f"paid-incomplete:{receipt_digest}"
    receipt_chunk_key = f"value:{_RECEIPT_CODEC}:{receipt_digest}"
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
                    "owner": "composer_primary_paid_incomplete",
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


def prepare_primary_paid_incomplete_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> PrimaryPaidIncompleteTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("PRIMARY terminal plan requires owned original identity and head")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("PRIMARY terminal plan requires its positive creator sequence")
    maximum_receipt_bytes = maximum_paid_incomplete_receipt_bytes(identity, "primary")
    snapshot_json, events, _, _ = _terminal_shape(admitted, identity, creator_sequence, "0" * 64)
    event_bytes = tuple(len(event.payload_json.encode("utf-8")) for event in events)
    terminal_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(snapshot_json).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = maximum_receipt_bytes + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.primary-paid-incomplete-tail.v1",
            "identity": identity.model_dump(mode="json"),
            "admitted_snapshot_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "creator_sequence": creator_sequence,
            "receipt_max": maximum_receipt_bytes,
            "event_bytes": event_bytes,
            "terminal_snapshot_bytes": terminal_bytes,
            "reserved_bytes": reserved,
        }
    )
    return PrimaryPaidIncompleteTailPlan(
        identity,
        admitted,
        creator_sequence,
        maximum_receipt_bytes,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
    )


def build_primary_paid_incomplete_tail_batch(
    plan: PrimaryPaidIncompleteTailPlan, *, receipt: PaidIncompleteReceipt, published_at: datetime
) -> PrimaryPaidIncompleteTailBatch:
    if type(plan) is not PrimaryPaidIncompleteTailPlan or type(receipt) is not PaidIncompleteReceipt:
        raise TypeError("PRIMARY terminal batch requires original nominal plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("PRIMARY terminal publication requires aware database time")
    if receipt.producer != "primary":
        raise AuditIntegrityError("PRIMARY incomplete receipt changed its producer")
    receipt_json = encode_paid_incomplete_receipt(receipt, expected=plan.identity)
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes:
        raise AuditIntegrityError("PRIMARY terminal receipt exceeded the original closed bound")
    digest = sha256(receipt_json.encode("utf-8")).hexdigest()
    snapshot_json, events, receipt_key, receipt_chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest)
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    receipt_chunk = encode_private_chunk(codec=_RECEIPT_CODEC, canonical=receipt_json)
    if private_chunk_key(receipt_chunk) != receipt_chunk_key:
        raise AuditIntegrityError("PRIMARY terminal receipt key changed its canonical bytes")
    prospective = (
        ProspectiveContinuationChunk(receipt_chunk_key, receipt_json.encode("utf-8"), _RECEIPT_CODEC),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        tuple(len(event.payload_json.encode("utf-8")) for event in events) != plan.terminal_event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("PRIMARY terminal batch exceeded its original complete tail bound")
    return PrimaryPaidIncompleteTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
