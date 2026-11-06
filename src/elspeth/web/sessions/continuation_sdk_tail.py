"""Bounded SDK terminal DATA recipes; source factories supply SQL authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EffectFrontierEntry, InitializeFrame
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
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.continuation_sdk_terminal import (
    SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES,
    SdkTerminalProducer,
    SdkTerminalReceipt,
    encode_sdk_terminal_receipt,
)


class SdkTerminalFamily(StrEnum):
    PRIMARY = "primary"
    CLASSIFIER = "classifier_primary"
    SEMANTIC = "semantic"
    TITLE = "title"
    CHECKPOINT_EARLY = "checkpoint_early"
    CHECKPOINT_END = "checkpoint_end"

    @property
    def producer(self) -> SdkTerminalProducer:
        match self:
            case self.PRIMARY | self.CLASSIFIER:
                return "primary"
            case self.SEMANTIC:
                return "semantic_advisor"
            case self.TITLE:
                return "title"
            case self.CHECKPOINT_EARLY | self.CHECKPOINT_END:
                return "checkpoint_advisor"

    @property
    def ledger_source(self) -> Literal["composer", "auto_title"]:
        return "auto_title" if self is SdkTerminalFamily.TITLE else "composer"


@dataclass(frozen=True, slots=True)
class SdkTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    reserved_bytes: int
    contract_sha256: str
    event_bytes: tuple[int, ...]
    snapshot_bytes: int
    family: SdkTerminalFamily = SdkTerminalFamily.PRIMARY


@dataclass(frozen=True, slots=True)
class SdkTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _terminal_shape(
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    creator_sequence: int,
    digest: str,
    family: SdkTerminalFamily,
) -> tuple[str, tuple[ContinuationEvent, ...], str, str]:
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = root.frames[-1]
    matches = tuple(item for item in root.effect_frontier if item.effect_id == identity.effect_key)
    if (
        type(family) is not SdkTerminalFamily
        or type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or root.logical.session_id != identity.session_id
        or root.logical.operation_id != identity.operation_id
        or root.cursor != admitted.cursor
        or admitted.cursor != identity.source_cursor + 1
        or root.policy_hash != identity.policy_sha256
        or root.pending_owner_results
        or len(matches) != 1
        or matches[0].disposition != "dispatched"
    ):
        raise AuditIntegrityError("SDK tail lost its original dispatched source")
    unresolved: tuple[str, ...] = ()
    match family:
        case SdkTerminalFamily.PRIMARY:
            if (
                not identity.effect_key.startswith("provider:0:")
                or len(root.frames) != 1
                or type(frame) is not ComposeFrame
                or frame.phase != "awaiting_response"
                or frame.provider_effect_key != identity.effect_key
                or frame.pending_response is not None
                or root.pending_settlement_ids
            ):
                raise AuditIntegrityError("PRIMARY SDK tail lost its original dispatched source")
        case SdkTerminalFamily.CLASSIFIER:
            applied_batch_cut(root.frames)
            if not identity.effect_key.startswith("provider:0:") or root.pending_settlement_ids:
                raise AuditIntegrityError("classifier SDK tail lost its original applied batch")
        case SdkTerminalFamily.SEMANTIC:
            if (
                type(frame) is not AdvisorFrame
                or frame.phase != "awaiting_response"
                or frame.provider_effect_key != identity.effect_key
                or frame.response is not None
                or root.pending_settlement_ids
            ):
                raise AuditIntegrityError("semantic SDK tail lost its original advisor frame")
        case SdkTerminalFamily.TITLE:
            if (
                len(root.frames) != 1
                or type(frame) is not InitializeFrame
                or frame.ingress_phase != "title_pending"
                or frame.title_attempt_id != identity.attempt_id
                or frame.title_response is not None
                or identity.effect_key != "provider:3:0"
                or root.pending_settlement_ids != (identity.attempt_id,)
            ):
                raise AuditIntegrityError("TITLE SDK tail lost its original initialize frame")
        case SdkTerminalFamily.CHECKPOINT_EARLY | SdkTerminalFamily.CHECKPOINT_END:
            if root.pending_settlement_ids != (identity.attempt_id,) or any(
                item.disposition == "prepared" for item in root.effect_frontier
            ):
                raise AuditIntegrityError("checkpoint SDK tail lost its original dispatched frontier")
            unresolved = tuple(
                item.effect_id
                for item in root.effect_frontier
                if item.effect_id != identity.effect_key and item.disposition == "dispatched"
            )
    receipt_key = f"sdk-terminal:{digest}"
    chunk_key = f"value:composer.sdk-terminal.v1:{digest}"
    frontier = tuple(
        EffectFrontierEntry(effect_id=item.effect_id, disposition="sdk_terminal", receipt_id=receipt_key)
        if item.effect_id == identity.effect_key
        else EffectFrontierEntry(effect_id=item.effect_id, disposition="unknown", receipt_id=None)
        if item.effect_id in unresolved
        else item
        for item in root.effect_frontier
    )
    snapshot = encode_continuation_snapshot(root.model_copy(update={"effect_frontier": frontier, "pending_settlement_ids": ()}))
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": f"composer_{family.value}_sdk_terminal",
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
    return snapshot, events, receipt_key, chunk_key


def prepare_sdk_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int, family: SdkTerminalFamily
) -> SdkTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("SDK tail requires original nominal identity and head")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("SDK tail requires positive creator sequence")
    snapshot, events, _, _ = _terminal_shape(admitted, identity, creator_sequence, "0" * 64, family)
    event_bytes = tuple(len(event.payload_json.encode("utf-8")) for event in events)
    snapshot_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(snapshot).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES + sum(event_bytes) + snapshot_bytes
    manifest = canonical_json(
        {
            "version": f"composer.{family.value.replace('_', '-')}-sdk-terminal-tail.v1",
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
    return SdkTailPlan(
        identity, admitted, creator_sequence, reserved, sha256(manifest.encode("utf-8")).hexdigest(), event_bytes, snapshot_bytes, family
    )


def build_sdk_tail_batch(plan: SdkTailPlan, *, receipt: SdkTerminalReceipt, published_at: datetime) -> SdkTailBatch:
    if type(plan) is not SdkTailPlan or type(receipt) is not SdkTerminalReceipt:
        raise TypeError("SDK batch requires its nominal plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("SDK publication requires aware database time")
    rebuilt = prepare_sdk_tail_plan(
        identity=plan.identity, admitted=plan.admitted, creator_sequence=plan.creator_sequence, family=plan.family
    )
    if rebuilt != plan or receipt.producer != plan.family.producer:
        raise AuditIntegrityError("SDK plan differs from its original closed recipe or producer")
    encoded = encode_sdk_terminal_receipt(receipt, expected=plan.identity)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    snapshot, events, receipt_key, chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest, plan.family)
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot, events=events, published_at=published_at)
    chunk = encode_private_chunk(codec="composer.sdk-terminal.v1", canonical=encoded)
    if private_chunk_key(chunk) != chunk_key:
        raise AuditIntegrityError("SDK receipt changed its canonical chunk key")
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
        raise AuditIntegrityError("SDK batch exceeds its original reserved tail")
    return SdkTailBatch(encoded, receipt_key, chunk_key, append, prospective)


def require_sdk_tail_batch(plan: SdkTailPlan, batch: SdkTailBatch) -> None:
    """Validate deterministic bytes using the supplied original sealed append."""
    from elspeth.web.sessions.continuation_sdk_terminal import decode_sdk_terminal_receipt

    if type(plan) is not SdkTailPlan or type(batch) is not SdkTailBatch:
        raise TypeError("SDK capacity requires its nominal plan and batch")
    if plan != prepare_sdk_tail_plan(
        identity=plan.identity, admitted=plan.admitted, creator_sequence=plan.creator_sequence, family=plan.family
    ):
        raise AuditIntegrityError("SDK capacity changed its exact original recipe")
    receipt = decode_sdk_terminal_receipt(batch.receipt_json, expected=plan.identity)
    encoded = encode_sdk_terminal_receipt(receipt, expected=plan.identity)
    if encoded != batch.receipt_json or receipt.producer != plan.family.producer:
        raise AuditIntegrityError("SDK capacity changed its canonical receipt or producer")
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    snapshot, events, receipt_key, chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest, plan.family)
    append = batch.append
    append.require_original()
    if type(append.published_at) is not datetime or append.published_at.tzinfo is None:
        raise AuditIntegrityError("SDK capacity requires original aware publication time")
    expected_snapshot = encode_continuation_snapshot(
        decode_continuation_snapshot(snapshot).model_copy(update={"cursor": plan.admitted.cursor + len(events)})
    )
    expected = (
        ProspectiveContinuationChunk(chunk_key, encoded.encode("utf-8"), "composer.sdk-terminal.v1"),
        *(
            ProspectiveContinuationChunk(f"event:{plan.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", expected_snapshot.encode("utf-8"), "snapshot.v2"),
    )
    if (
        batch.receipt_key != receipt_key
        or batch.receipt_chunk_key != chunk_key
        or append.expected_checkpoint_id != plan.admitted.checkpoint_id
        or append.expected_cursor != plan.admitted.cursor
        or append.predecessor_json != plan.admitted.payload_json
        or append.session_id != plan.identity.session_id
        or append.operation_id != plan.identity.operation_id
        or append.handoff is not False
        or append.events != events
        or append.snapshot_json != expected_snapshot
        or batch.prospective != expected
        or tuple(len(event.payload_json.encode("utf-8")) for event in events) != plan.event_bytes
        or len(expected_snapshot.encode("utf-8")) != plan.snapshot_bytes
        or sum(len(item.payload) for item in expected) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("SDK capacity changed its exact sealed append or prospective bytes")
