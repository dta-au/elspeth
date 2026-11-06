"""Closed returned-incomplete terminal batch for one V4 checkpoint attempt."""

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
from elspeth.web.sessions.continuation_paid_incomplete import (
    PaidIncompleteReceipt,
    encode_paid_incomplete_receipt,
    maximum_paid_incomplete_receipt_bytes,
    paid_incomplete_receipt_keys,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk

_CODEC = "composer.paid-incomplete.v1"
_OWNER = "composer_checkpoint_paid_incomplete"


@dataclass(frozen=True, slots=True)
class CheckpointPaidIncompleteTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    phase: Literal["early", "end"]
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    contract_sha256: str
    terminal_event_bytes: tuple[int, ...]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class CheckpointPaidIncompleteTailBatch:
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
    private_audit_sha256: str,
) -> tuple[str, tuple[ContinuationEvent, ...], str, str]:
    root = decode_continuation_snapshot(admitted.payload_json)
    child = tuple(item for item in root.effect_frontier if item.effect_id == identity.effect_key)
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
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
        or len(private_audit_sha256) != 64
        or any(character not in "0123456789abcdef" for character in private_audit_sha256)
    ):
        raise AuditIntegrityError("checkpoint incomplete tail lost its original dispatched child")
    receipt_key = f"paid-incomplete:{receipt_digest}"
    receipt_chunk_key = f"value:{_CODEC}:{receipt_digest}"
    unresolved = tuple(
        item.effect_id for item in root.effect_frontier if item.effect_id != identity.effect_key and item.disposition == "dispatched"
    )
    frontier = tuple(
        EffectFrontierEntry(effect_id=item.effect_id, disposition="paid_incomplete", receipt_id=receipt_key)
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
                    "source_result_incomplete": True,
                    "private_audit_sha256": private_audit_sha256,
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
    return encode_continuation_snapshot(terminal), events, receipt_key, receipt_chunk_key


def prepare_checkpoint_paid_incomplete_tail_plan(
    *,
    identity: PaidOverflowIdentity,
    admitted: StoredContinuationSnapshot,
    phase: Literal["early", "end"],
    creator_sequence: int,
) -> CheckpointPaidIncompleteTailPlan:
    terminal_json, events, _key, _chunk = _terminal_shape(admitted, identity, phase, creator_sequence, "0" * 64, "0" * 64)
    maximum = maximum_paid_incomplete_receipt_bytes(identity, "checkpoint_advisor")
    event_bytes = tuple(len(event.payload_json.encode("utf-8")) for event in events)
    root_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(terminal_json).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
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
    return CheckpointPaidIncompleteTailPlan(
        identity, admitted, phase, creator_sequence, maximum, reserved, contract, event_bytes, root_bytes
    )


def build_checkpoint_paid_incomplete_tail_batch(
    plan: CheckpointPaidIncompleteTailPlan,
    *,
    receipt: PaidIncompleteReceipt,
    published_at: datetime,
    private_audit_sha256: str = "0" * 64,
) -> CheckpointPaidIncompleteTailBatch:
    if type(plan) is not CheckpointPaidIncompleteTailPlan or type(receipt) is not PaidIncompleteReceipt:
        raise AuditIntegrityError("checkpoint incomplete terminal requires its nominal plan and receipt")
    if (
        receipt.identity != plan.identity
        or receipt.producer != "checkpoint_advisor"
        or receipt.outcome != "terminal_paid_incomplete"
        or receipt.provider_returned is not True
        or receipt.evidence_status != "incomplete"
        or receipt.retryable is not False
    ):
        raise AuditIntegrityError("checkpoint incomplete terminal changed its original paid source")
    receipt_json = encode_paid_incomplete_receipt(receipt, expected=plan.identity)
    receipt_key, receipt_chunk_key = paid_incomplete_receipt_keys(receipt_json, expected=plan.identity)
    digest = sha256(receipt_json.encode("utf-8")).hexdigest()
    terminal_json, events, expected_key, expected_chunk = _terminal_shape(
        plan.admitted, plan.identity, plan.phase, plan.creator_sequence, digest, private_audit_sha256
    )
    if (receipt_key, receipt_chunk_key) != (expected_key, expected_chunk):
        raise AuditIntegrityError("checkpoint incomplete receipt keys changed")
    append = prepare_snapshot_append(plan.admitted, snapshot_json=terminal_json, events=events, published_at=published_at)
    prospective = (
        ProspectiveContinuationChunk(receipt_chunk_key, receipt_json.encode("utf-8"), _CODEC),
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
        raise AuditIntegrityError("checkpoint incomplete terminal exceeded its original pre-SDK bound")
    return CheckpointPaidIncompleteTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
