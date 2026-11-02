"""Closed V4 TITLE tail for a returned paid result with incomplete evidence.

Preparation measures the full terminal root and both events before dispatch.
The original SQL owner must reserve the maximum of all mutually exclusive
terminal alternatives and write the exact batch in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import EffectFrontierEntry, InitializeFrame
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
from elspeth.web.sessions.continuation_title_no_sdk_tail import TitleNoSdkTailPlan, prepare_title_no_sdk_tail_plan
from elspeth.web.sessions.continuation_title_paid_overflow_tail import TitlePaidOverflowTailPlan, prepare_title_paid_overflow_tail_plan

_EFFECT = "provider:3:0"
_CODEC = "composer.paid-incomplete.v1"


@dataclass(frozen=True, slots=True)
class TitlePaidIncompleteTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    fallback_contract_sha256: str
    terminal_event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class TitlePaidIncompleteTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def title_combined_terminal_hold_contract(
    paid: TitlePaidOverflowTailPlan, incomplete: TitlePaidIncompleteTailPlan, no_sdk: TitleNoSdkTailPlan
) -> tuple[int, str]:
    """Reserve the maximum of the three mutually exclusive original tails."""
    if (
        type(paid) is not TitlePaidOverflowTailPlan
        or type(incomplete) is not TitlePaidIncompleteTailPlan
        or type(no_sdk) is not TitleNoSdkTailPlan
        or paid.identity != incomplete.identity
        or paid.identity != no_sdk.identity
        or paid.admitted != incomplete.admitted
        or paid.admitted != no_sdk.admitted
        or paid.creator_sequence != incomplete.creator_sequence
        or paid.creator_sequence != no_sdk.creator_sequence
        or prepare_title_paid_overflow_tail_plan(identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence)
        != paid
        or prepare_title_paid_incomplete_tail_plan(
            identity=incomplete.identity, admitted=incomplete.admitted, creator_sequence=incomplete.creator_sequence
        )
        != incomplete
        or prepare_title_no_sdk_tail_plan(
            identity=no_sdk.identity,
            admitted=no_sdk.admitted,
            creator_sequence=no_sdk.creator_sequence,
            requested_model=no_sdk.requested_model,
        )
        != no_sdk
    ):
        raise AuditIntegrityError("TITLE H changed one of its original terminal alternatives")
    reserved = max(paid.reserved_bytes, incomplete.reserved_bytes, no_sdk.reserved_bytes)
    contract = canonical_json(
        {
            "version": "composer.title-v4-combined-tail.v1",
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
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or identity.effect_key != _EFFECT
        or root.logical.session_id != identity.session_id
        or root.logical.operation_id != identity.operation_id
        or root.cursor != admitted.cursor
        or admitted.cursor != identity.source_cursor + 1
        or root.policy_hash != identity.policy_sha256
        or len(root.frames) != 1
        or type(root.frames[0]) is not InitializeFrame
        or root.frames[0].ingress_phase != "title_pending"
        or root.frames[0].title_attempt_id != identity.attempt_id
        or root.frames[0].title_response is not None
        or root.pending_settlement_ids != (identity.attempt_id,)
        or root.pending_owner_results
        or root.semantic_frontier is not None
        or root.active_action_json is not None
        or root.active_action_source is not None
        or len(root.effect_frontier) != 1
        or root.effect_frontier[0].effect_id != _EFFECT
        or root.effect_frontier[0].disposition != "dispatched"
        or root.effect_frontier[0].receipt_id is not None
    ):
        raise AuditIntegrityError("TITLE paid-incomplete tail lost its original dispatched source")
    receipt_key = f"paid-incomplete:{receipt_digest}"
    receipt_chunk_key = f"value:{_CODEC}:{receipt_digest}"
    frontier = (EffectFrontierEntry(effect_id=_EFFECT, disposition="paid_incomplete", receipt_id=receipt_key),)
    terminal_json = encode_continuation_snapshot(root.model_copy(update={"effect_frontier": frontier, "pending_settlement_ids": ()}))
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": "composer_title_paid_incomplete",
                    "version": 1,
                    "effect_key": _EFFECT,
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


def prepare_title_paid_incomplete_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> TitlePaidIncompleteTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("TITLE paid-incomplete reserve requires original identity and root")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("TITLE paid-incomplete reserve requires positive creator sequence")
    receipt_max = maximum_paid_incomplete_receipt_bytes(identity, "title")
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
            "version": "composer.title-paid-incomplete-tail.v1",
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
    return TitlePaidIncompleteTailPlan(
        identity,
        admitted,
        creator_sequence,
        receipt_max,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
    )


def build_title_paid_incomplete_tail_batch(
    plan: TitlePaidIncompleteTailPlan, *, receipt: PaidIncompleteReceipt, published_at: datetime
) -> TitlePaidIncompleteTailBatch:
    if type(plan) is not TitlePaidIncompleteTailPlan or type(receipt) is not PaidIncompleteReceipt:
        raise TypeError("TITLE paid-incomplete batch requires original plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("TITLE paid-incomplete publication requires aware database time")
    if receipt.producer != "title" or receipt.identity != plan.identity:
        raise AuditIntegrityError("TITLE paid-incomplete receipt changed its original producer or identity")
    receipt_json = encode_paid_incomplete_receipt(receipt, expected=plan.identity)
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes:
        raise AuditIntegrityError("TITLE paid-incomplete receipt exceeded its original closed bound")
    receipt_key, receipt_chunk_key = paid_incomplete_receipt_keys(receipt_json, expected=plan.identity)
    digest = receipt_key.removeprefix("paid-incomplete:")
    snapshot_json, events, shape_key, shape_chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest)
    if (shape_key, shape_chunk_key) != (receipt_key, receipt_chunk_key):
        raise AuditIntegrityError("TITLE paid-incomplete tail changed its canonical receipt keys")
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    receipt_chunk = encode_private_chunk(codec=_CODEC, canonical=receipt_json)
    if private_chunk_key(receipt_chunk) != receipt_chunk_key:
        raise AuditIntegrityError("TITLE paid-incomplete receipt changed its private chunk")
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
        raise AuditIntegrityError("TITLE paid-incomplete batch exceeded its predispatch terminal bound")
    return TitlePaidIncompleteTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
