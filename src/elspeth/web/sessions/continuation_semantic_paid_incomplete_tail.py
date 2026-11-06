"""Predispatch terminal reserve for one returned, unretainable semantic result."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import AdvisorFrame, EffectFrontierEntry
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
    paid_incomplete_receipt_keys,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import SemanticNoSdkTailPlan
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import SemanticPaidOverflowTailPlan

_CODEC = "composer.paid-incomplete.v1"


@dataclass(frozen=True, slots=True)
class SemanticPaidIncompleteTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    fallback_contract_sha256: str
    terminal_event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class SemanticPaidIncompleteTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def semantic_combined_terminal_hold_contract(
    paid: SemanticPaidOverflowTailPlan,
    incomplete: SemanticPaidIncompleteTailPlan,
    no_sdk: SemanticNoSdkTailPlan,
) -> tuple[int, str]:
    """Bind all mutually exclusive fallback tails to one original H."""
    if (
        type(paid) is not SemanticPaidOverflowTailPlan
        or type(incomplete) is not SemanticPaidIncompleteTailPlan
        or type(no_sdk) is not SemanticNoSdkTailPlan
        or paid.identity != incomplete.identity
        or paid.admitted != incomplete.admitted
        or paid.admitted != no_sdk.admitted
        or paid.identity.attempt_id != no_sdk.attempt_id
    ):
        raise AuditIntegrityError("Semantic H changed one of its original terminal tails")
    reserved = max(paid.reserved_bytes, incomplete.reserved_bytes, no_sdk.reserved_bytes)
    contract = canonical_json(
        {
            "version": "composer.semantic-v4-combined-tail.v2",
            "paid": paid.fallback_contract_sha256,
            "incomplete": incomplete.fallback_contract_sha256,
            "no_sdk": no_sdk.contract_sha256,
            "reserved_bytes": reserved,
        }
    )
    return reserved, sha256(contract.encode("utf-8")).hexdigest()


def _terminal_shape(
    admitted: StoredContinuationSnapshot, identity: PaidOverflowIdentity, creator_sequence: int, receipt_digest: str
) -> tuple[str, tuple[ContinuationEvent, ContinuationEvent], str, str]:
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = root.frames[-1]
    matches = tuple(item for item in root.effect_frontier if item.effect_id == identity.effect_key)
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or not identity.effect_key.startswith("provider:1:")
        or root.logical.session_id != identity.session_id
        or root.logical.operation_id != identity.operation_id
        or root.cursor != admitted.cursor
        or admitted.cursor != identity.source_cursor + 1
        or root.policy_hash != identity.policy_sha256
        or type(frame) is not AdvisorFrame
        or frame.phase != "awaiting_response"
        or frame.provider_effect_key != identity.effect_key
        or frame.response is not None
        or root.pending_settlement_ids
        or root.pending_owner_results
        or len(matches) != 1
        or matches[0].disposition != "dispatched"
    ):
        raise AuditIntegrityError("Semantic paid-incomplete tail lost its dispatched original source")
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
                    "owner": "composer_semantic_paid_incomplete",
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


def prepare_semantic_paid_incomplete_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> SemanticPaidIncompleteTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("Semantic paid-incomplete reserve requires original identity and head")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("Semantic paid-incomplete reserve requires positive creator sequence")
    receipt_max = maximum_paid_incomplete_receipt_bytes(identity, "semantic_advisor")
    snapshot_json, events, _, _ = _terminal_shape(admitted, identity, creator_sequence, "0" * 64)
    event_bytes = tuple(len(item.payload_json.encode("utf-8")) for item in events)
    terminal_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(snapshot_json).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = receipt_max + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.semantic-paid-incomplete-tail.v1",
            "identity": identity.model_dump(mode="json"),
            "admitted_snapshot_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "creator_sequence": creator_sequence,
            "receipt_max": receipt_max,
            "event_bytes": event_bytes,
            "terminal_snapshot_bytes": terminal_bytes,
            "reserved_bytes": reserved,
        }
    )
    return SemanticPaidIncompleteTailPlan(
        identity,
        admitted,
        creator_sequence,
        receipt_max,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
    )


def build_semantic_paid_incomplete_tail_batch(
    plan: SemanticPaidIncompleteTailPlan, *, receipt: PaidIncompleteReceipt, published_at: datetime
) -> SemanticPaidIncompleteTailBatch:
    if type(plan) is not SemanticPaidIncompleteTailPlan or type(receipt) is not PaidIncompleteReceipt:
        raise TypeError("Semantic paid-incomplete batch requires original plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("Semantic paid-incomplete publication requires aware database time")
    if receipt.producer != "semantic_advisor" or receipt.identity != plan.identity:
        raise AuditIntegrityError("Semantic paid-incomplete receipt changed its original producer or identity")
    receipt_json = encode_paid_incomplete_receipt(receipt, expected=plan.identity)
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes:
        raise AuditIntegrityError("Semantic paid-incomplete receipt exceeded its original closed bound")
    receipt_key, receipt_chunk_key = paid_incomplete_receipt_keys(receipt_json, expected=plan.identity)
    digest = receipt_key.removeprefix("paid-incomplete:")
    snapshot_json, events, shape_key, shape_chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest)
    if (shape_key, shape_chunk_key) != (receipt_key, receipt_chunk_key):
        raise AuditIntegrityError("Semantic paid-incomplete tail changed its canonical receipt keys")
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    receipt_chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    if private_chunk_key(receipt_chunk) != receipt_chunk_key:
        raise AuditIntegrityError("Semantic paid-incomplete receipt changed its private chunk")
    prospective = (
        ProspectiveContinuationChunk(receipt_chunk_key, receipt_json.encode("utf-8"), _CODEC),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", item.payload_json.encode("utf-8"), "event.v2")
            for index, item in enumerate(events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        tuple(len(item.payload_json.encode("utf-8")) for item in events) != plan.terminal_event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("Semantic paid-incomplete batch exceeded its predispatch terminal bound")
    return SemanticPaidIncompleteTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
