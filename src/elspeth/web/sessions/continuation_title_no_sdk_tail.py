"""Closed zero-use TITLE refusal alternative reserved before SDK entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import EffectFrontierEntry, InitializeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkRefusalReason
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    prepare_snapshot_append,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_paid_overflow_quota import preflight_v4_requested_model
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk

_OWNER = "composer_title_no_sdk"
_CODEC = "composer.title-no-sdk.v1"
_PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"
_PLACEHOLDER_DIGEST = "0" * 64


@dataclass(frozen=True, slots=True)
class TitleNoSdkTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    requested_model: str
    maximum_receipt_bytes: int
    event_bytes: tuple[int, ...]
    terminal_snapshot_bytes: int
    reserved_bytes: int
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class TitleNoSdkTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _failure_class(reason: TitleNoSdkRefusalReason) -> str:
    if reason is TitleNoSdkRefusalReason.GUARD_REFUSED:
        return "PromptGuardRefused"
    if reason is TitleNoSdkRefusalReason.COST_REFUSED:
        return "CommonProviderCostRefused"
    if reason is TitleNoSdkRefusalReason.CHARGEABLE_REFUSED:
        return "ChargeableAdmissionRefused"
    if reason is TitleNoSdkRefusalReason.PRE_SDK_DEADLINE:
        return "TimeoutError"
    if reason is TitleNoSdkRefusalReason.CANCELLED_BEFORE_SDK:
        return "CancelledError"
    raise AuditIntegrityError("TITLE no-SDK refusal reason is not closed")


def _receipt_json(plan: TitleNoSdkTailPlan, *, reason: TitleNoSdkRefusalReason, receipt_key: str) -> str:
    return canonical_json(
        {
            "schema_version": _CODEC,
            "session_id": plan.identity.session_id,
            "operation_id": plan.identity.operation_id,
            "effect_key": plan.identity.effect_key,
            "attempt_id": plan.identity.attempt_id,
            "session_operation_id": plan.identity.session_operation_id,
            "session_operation_epoch": plan.identity.session_operation_epoch,
            "ownership_generation": plan.identity.ownership_generation,
            "lease_sha256": plan.identity.lease_sha256,
            "policy_sha256": plan.identity.policy_sha256,
            "source_checkpoint_id": plan.identity.source_checkpoint_id,
            "source_cursor": plan.identity.source_cursor,
            "source_snapshot_sha256": plan.identity.source_snapshot_sha256,
            "receipt_key": receipt_key,
            "reason": reason.value,
            "original_failure_class": _failure_class(reason),
            "requested_model": plan.requested_model,
            "usage": "zero",
        }
    )


def _terminal_shape(plan: TitleNoSdkTailPlan, *, receipt_key: str, receipt_digest: str) -> tuple[str, tuple[ContinuationEvent, ...], str]:
    root = decode_continuation_snapshot(plan.admitted.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    child = tuple(item for item in root.effect_frontier if item.effect_id == plan.identity.effect_key)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or root.logical.session_id != plan.identity.session_id
        or root.logical.operation_id != plan.identity.operation_id
        or root.cursor != plan.admitted.cursor
        or root.policy_hash != plan.identity.policy_sha256
        or len(child) != 1
        or child[0].disposition != "dispatched"
        or child[0].receipt_id is not None
        or root.pending_settlement_ids != (plan.identity.attempt_id,)
        or any(item.disposition == "prepared" for item in root.effect_frontier)
        or plan.identity.effect_key != "provider:3:0"
        or plan.admitted.cursor != plan.identity.source_cursor + 1
        or len(root.frames) != 1
        or type(root.frames[0]) is not InitializeFrame
        or root.frames[0].ingress_phase != "title_pending"
        or root.frames[0].title_attempt_id != plan.identity.attempt_id
        or root.frames[0].title_response is not None
        or len(root.effect_frontier) != 1
        or root.pending_owner_results
        or root.semantic_frontier is not None
        or root.active_action_json is not None
        or root.active_action_source is not None
        or type(plan.creator_sequence) is not int
        or plan.creator_sequence <= 0
    ):
        raise AuditIntegrityError("TITLE no-SDK tail lost its original dispatched child")
    try:
        parsed = UUID(receipt_key)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("TITLE no-SDK receipt key is not an issued UUID") from error
    if parsed.version != 4 or str(parsed) != receipt_key:
        raise AuditIntegrityError("TITLE no-SDK receipt key is not an issued UUID")
    receipt_chunk_key = f"value:{_CODEC}:{receipt_digest}"
    terminal = root.model_copy(
        update={
            "effect_frontier": (EffectFrontierEntry(effect_id=plan.identity.effect_key, disposition="refused", receipt_id=receipt_key),),
            "pending_settlement_ids": (),
        }
    )
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": _OWNER,
                    "version": 1,
                    "effect_key": plan.identity.effect_key,
                    "attempt_id": plan.identity.attempt_id,
                    "receipt_key": receipt_key,
                    "result_chunk_key": receipt_chunk_key,
                    "source_checkpoint_id": plan.identity.source_checkpoint_id,
                    "source_cursor": plan.identity.source_cursor,
                }
            ),
        ),
        ContinuationEvent(
            "settled",
            canonical_json(
                {
                    "version": 1,
                    "sequence": plan.creator_sequence,
                    "generation": plan.identity.ownership_generation,
                    "status": "failed",
                    "failure_code": "http_error",
                }
            ),
        ),
    )
    return encode_continuation_snapshot(terminal), events, receipt_chunk_key


def prepare_title_no_sdk_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int, requested_model: str
) -> TitleNoSdkTailPlan:
    """Reserve every closed zero-use reason with one exact original model."""
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("TITLE no-SDK H requires its original owned identity and admitted root")
    preflight_v4_requested_model(requested_model)
    provisional = TitleNoSdkTailPlan(identity, admitted, creator_sequence, requested_model, 0, (), 0, 0, "0" * 64)
    root_json, events, _key = _terminal_shape(provisional, receipt_key=_PLACEHOLDER_UUID, receipt_digest=_PLACEHOLDER_DIGEST)
    maximum = max(
        len(_receipt_json(provisional, reason=reason, receipt_key=_PLACEHOLDER_UUID).encode("utf-8")) for reason in TitleNoSdkRefusalReason
    )
    event_bytes = tuple(len(item.payload_json.encode("utf-8")) for item in events)
    root_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(root_json).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = maximum + sum(event_bytes) + root_bytes
    contract = sha256(
        canonical_json(
            {
                "owner": _OWNER,
                "version": 1,
                "identity": identity.model_dump(mode="json"),
                "admitted_checkpoint_id": admitted.checkpoint_id,
                "admitted_cursor": admitted.cursor,
                "admitted_snapshot_sha256": sha256(admitted.payload_json.encode("utf-8")).hexdigest(),
                "creator_sequence": creator_sequence,
                "requested_model": requested_model,
                "maximum_receipt_bytes": maximum,
                "event_bytes": event_bytes,
                "terminal_snapshot_bytes": root_bytes,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    return TitleNoSdkTailPlan(identity, admitted, creator_sequence, requested_model, maximum, event_bytes, root_bytes, reserved, contract)


def build_title_no_sdk_tail_batch(
    plan: TitleNoSdkTailPlan, *, reason: TitleNoSdkRefusalReason, receipt_key: str, published_at: datetime
) -> TitleNoSdkTailBatch:
    if type(plan) is not TitleNoSdkTailPlan or type(reason) is not TitleNoSdkRefusalReason:
        raise AuditIntegrityError("TITLE no-SDK terminal requires nominal plan/reason")
    receipt_json = _receipt_json(plan, reason=reason, receipt_key=receipt_key)
    digest = sha256(receipt_json.encode("utf-8")).hexdigest()
    root_json, events, receipt_chunk_key = _terminal_shape(plan, receipt_key=receipt_key, receipt_digest=digest)
    append = prepare_snapshot_append(plan.admitted, snapshot_json=root_json, events=events, published_at=published_at)
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
        or tuple(len(item.payload_json.encode("utf-8")) for item in events) != plan.event_bytes
        or len(append.snapshot_json.encode("utf-8")) != plan.terminal_snapshot_bytes
        or sum(len(item.payload) for item in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("TITLE no-SDK terminal exceeded its original pre-SDK bound")
    return TitleNoSdkTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
