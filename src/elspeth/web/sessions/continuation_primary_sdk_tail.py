"""Predispatch reserved PRIMARY SDK-terminal receipt, root and event batch."""

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
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.continuation_sdk_terminal import (
    SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES,
    SdkTerminalReceipt,
    encode_sdk_terminal_receipt,
)


@dataclass(frozen=True, slots=True)
class PrimarySdkTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    reserved_bytes: int
    contract_sha256: str
    event_bytes: tuple[int, int]
    snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class PrimarySdkTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _terminal_shape(
    admitted: StoredContinuationSnapshot, identity: PaidOverflowIdentity, creator_sequence: int, digest: str
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
        raise AuditIntegrityError("PRIMARY SDK tail lost its original dispatched source")
    receipt_key = f"sdk-terminal:{digest}"
    chunk_key = f"value:composer.sdk-terminal.v1:{digest}"
    frontier = tuple(
        EffectFrontierEntry(effect_id=item.effect_id, disposition="sdk_terminal", receipt_id=receipt_key)
        if item.effect_id == identity.effect_key
        else item
        for item in root.effect_frontier
    )
    snapshot = encode_continuation_snapshot(root.model_copy(update={"effect_frontier": frontier}))
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": "composer_primary_sdk_terminal",
                    "version": 1,
                    "effect_key": identity.effect_key,
                    "attempt_id": identity.attempt_id,
                    "receipt_key": receipt_key,
                    "result_chunk_key": chunk_key,
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
    return snapshot, events, receipt_key, chunk_key


def prepare_primary_sdk_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> PrimarySdkTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("PRIMARY SDK tail requires original nominal identity and head")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("PRIMARY SDK tail requires positive creator sequence")
    snapshot, events, _, _ = _terminal_shape(admitted, identity, creator_sequence, "0" * 64)
    event_bytes = (len(events[0].payload_json.encode("utf-8")), len(events[1].payload_json.encode("utf-8")))
    snapshot_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(snapshot).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES + sum(event_bytes) + snapshot_bytes
    manifest = canonical_json(
        {
            "version": "composer.primary-sdk-terminal-tail.v1",
            "identity": identity.model_dump(mode="json"),
            "admitted_snapshot_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
            "admitted_checkpoint_id": admitted.checkpoint_id,
            "admitted_cursor": admitted.cursor,
            "creator_sequence": creator_sequence,
            "receipt_max": SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES,
            "event_bytes": event_bytes,
            "snapshot_bytes": snapshot_bytes,
            "reserved_bytes": reserved,
        }
    )
    return PrimarySdkTailPlan(
        identity, admitted, creator_sequence, reserved, sha256(manifest.encode("utf-8")).hexdigest(), event_bytes, snapshot_bytes
    )


def build_primary_sdk_tail_batch(plan: PrimarySdkTailPlan, *, receipt: SdkTerminalReceipt, published_at: datetime) -> PrimarySdkTailBatch:
    if type(plan) is not PrimarySdkTailPlan or type(receipt) is not SdkTerminalReceipt:
        raise TypeError("PRIMARY SDK batch requires its nominal plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("PRIMARY SDK publication requires aware database time")
    rebuilt = prepare_primary_sdk_tail_plan(identity=plan.identity, admitted=plan.admitted, creator_sequence=plan.creator_sequence)
    if rebuilt != plan:
        raise AuditIntegrityError("PRIMARY SDK plan differs from its original closed recipe")
    encoded = encode_sdk_terminal_receipt(receipt, expected=plan.identity)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    snapshot, events, receipt_key, chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest)
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot, events=events, published_at=published_at)
    chunk = encode_private_chunk(codec="composer.sdk-terminal.v1", canonical=encoded)
    if private_chunk_key(chunk) != chunk_key:
        raise AuditIntegrityError("PRIMARY SDK receipt changed its canonical chunk key")
    prospective = (
        ProspectiveContinuationChunk(chunk_key, encoded.encode("utf-8"), "composer.sdk-terminal.v1"),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if (
        tuple(len(event.payload_json.encode("utf-8")) for event in events) != plan.event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("PRIMARY SDK batch exceeds its original reserved tail")
    return PrimarySdkTailBatch(encoded, receipt_key, chunk_key, append, prospective)
