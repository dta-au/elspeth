"""Closed returned-overflow tail for one physical EARLY/END checkpoint call.

This module prepares bytes. The SQL owner must reserve its bound before SDK
entry and prove the original physical attempt again before consuming it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    prepare_snapshot_append,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity, PaidOverflowReceipt, encode_paid_overflow_receipt
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.continuation_title_paid_overflow_tail import _maximum_receipt_bytes

_RECEIPT_CODEC = "composer.paid-overflow.v1"
_OWNER = "composer_checkpoint_paid_overflow"


@dataclass(frozen=True, slots=True)
class CheckpointPaidOverflowTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    phase: Literal["early", "end"]
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    fallback_contract_sha256: str
    terminal_event_bytes: tuple[int, ...]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class CheckpointPaidOverflowTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _terminal_shape(
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    phase: Literal["early", "end"],
    creator_sequence: int,
    receipt_digest: str,
) -> tuple[str, tuple[ContinuationEvent, ...], tuple[str, ...], str, str]:
    root = decode_continuation_snapshot(admitted.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    child = tuple(item for item in root.effect_frontier if item.effect_id == identity.effect_key)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or root.logical.session_id != identity.session_id
        or root.logical.operation_id != identity.operation_id
        or root.cursor != admitted.cursor
        or root.policy_hash != identity.policy_sha256
        or len(child) != 1
        or child[0].disposition != "dispatched"
        or child[0].receipt_id is not None
        or root.pending_settlement_ids != (identity.attempt_id,)
        or any(item.disposition == "prepared" for item in root.effect_frontier)
        or type(creator_sequence) is not int
        or creator_sequence < 1
        or phase not in ("early", "end")
        or len(receipt_digest) != 64
        or any(character not in "0123456789abcdef" for character in receipt_digest)
    ):
        raise AuditIntegrityError("checkpoint overflow tail lost its original dispatched child")
    receipt_key = f"paid-overflow:{receipt_digest}"
    receipt_chunk_key = f"value:{_RECEIPT_CODEC}:{receipt_digest}"
    unresolved = tuple(
        item.effect_id for item in root.effect_frontier if item.effect_id != identity.effect_key and item.disposition == "dispatched"
    )
    frontier = tuple(
        EffectFrontierEntry(effect_id=item.effect_id, disposition="paid_overflow", receipt_id=receipt_key)
        if item.effect_id == identity.effect_key
        else item.model_copy(update={"disposition": "unknown"})
        if item.effect_id in unresolved
        else item
        for item in root.effect_frontier
    )
    terminal = root.model_copy(
        update={
            "effect_frontier": frontier,
            "pending_settlement_ids": tuple(value for value in root.pending_settlement_ids if value != identity.attempt_id),
        }
    )
    terminal_json = encode_continuation_snapshot(terminal)
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": _OWNER,
                    "version": 1,
                    "phase": phase,
                    "effect_key": identity.effect_key,
                    "attempt_id": identity.attempt_id,
                    "receipt_key": receipt_key,
                    "result_chunk_key": receipt_chunk_key,
                    "source_result_complete": True,
                    "source_checkpoint_id": identity.source_checkpoint_id,
                    "source_cursor": identity.source_cursor,
                }
            ),
        ),
        *(ContinuationEvent("effect_unknown", canonical_json({"version": 1, "effect_key": key})) for key in unresolved),
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
    return terminal_json, events, unresolved, receipt_key, receipt_chunk_key


def prepare_checkpoint_paid_overflow_tail_plan(
    *,
    identity: PaidOverflowIdentity,
    admitted: StoredContinuationSnapshot,
    phase: Literal["early", "end"],
    creator_sequence: int,
) -> CheckpointPaidOverflowTailPlan:
    """Reserve the closed receipt envelope and exact event/root byte shapes."""
    terminal_json, events, _unresolved, _key, _chunk_key = _terminal_shape(admitted, identity, phase, creator_sequence, "0" * 64)
    # The shared closed-schema envelope currently writes the TITLE producer.
    # This tail's exact producer is longer; account for that JSON field's byte
    # delta before any physical call can reserve H.
    producer_delta = len(canonical_json("checkpoint_advisor").encode("utf-8")) - len(canonical_json("title").encode("utf-8"))
    maximum = _maximum_receipt_bytes(identity) + producer_delta
    event_bytes = tuple(len(event.payload_json.encode("utf-8")) for event in events)
    root_bytes = len(terminal_json.encode("utf-8"))
    reserved = maximum + sum(event_bytes) + root_bytes
    contract = sha256(
        canonical_json(
            {
                "owner": _OWNER,
                "version": 1,
                "identity": identity.model_dump(mode="json"),
                "phase": phase,
                "admitted_checkpoint_id": admitted.checkpoint_id,
                "admitted_cursor": admitted.cursor,
                "admitted_snapshot_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
                "creator_sequence": creator_sequence,
                "maximum_receipt_bytes": maximum,
                "terminal_event_bytes": event_bytes,
                "terminal_snapshot_bytes": root_bytes,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    return CheckpointPaidOverflowTailPlan(identity, admitted, phase, creator_sequence, maximum, reserved, contract, event_bytes, root_bytes)


def build_checkpoint_paid_overflow_tail_batch(
    plan: CheckpointPaidOverflowTailPlan, *, receipt: PaidOverflowReceipt, published_at: datetime
) -> CheckpointPaidOverflowTailBatch:
    """Build one exact replacement of H after a returned incomplete result."""
    if type(plan) is not CheckpointPaidOverflowTailPlan or type(receipt) is not PaidOverflowReceipt:
        raise AuditIntegrityError("checkpoint terminal requires its nominal plan and receipt")
    if (
        receipt.identity != plan.identity
        or receipt.producer != "checkpoint_advisor"
        or receipt.outcome != "terminal_paid_overflow"
        or receipt.evidence_status != "incomplete"
        or receipt.retryable is not False
    ):
        raise AuditIntegrityError("checkpoint terminal receipt changed its original paid source")
    receipt_json = encode_paid_overflow_receipt(receipt, expected=plan.identity)
    receipt_digest = sha256(receipt_json.encode("utf-8")).hexdigest()
    terminal_json, events, _unresolved, receipt_key, receipt_chunk_key = _terminal_shape(
        plan.admitted, plan.identity, plan.phase, plan.creator_sequence, receipt_digest
    )
    append = prepare_snapshot_append(plan.admitted, snapshot_json=terminal_json, events=events, published_at=published_at)
    prospective = (
        ProspectiveContinuationChunk(receipt_chunk_key, receipt_json.encode("utf-8"), _RECEIPT_CODEC),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(events, 1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes
        or tuple(len(event.payload_json.encode("utf-8")) for event in events) != plan.terminal_event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(chunk.payload) for chunk in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("checkpoint terminal batch exceeded its original pre-SDK bound")
    return CheckpointPaidOverflowTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
