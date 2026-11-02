"""Closed PRIMARY V4 paid terminal tail and its predispatch byte bound."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import get_args

from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
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
from elspeth.web.sessions.continuation_paid_overflow import (
    CANONICAL_INTEGER_MAX,
    COST_AMOUNT_MAX_LENGTH,
    FINISHED_AT_MAX_LENGTH,
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowCostStatus,
    PaidOverflowIdentity,
    PaidOverflowReason,
    PaidOverflowReceipt,
    PaidOverflowRepresentation,
    PaidOverflowUsage,
    PaidOverflowUsageStatus,
    encode_paid_overflow_receipt,
)
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk

_RECEIPT_CODEC = "composer.paid-overflow.v1"
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "identity",
        "producer",
        "outcome",
        "evidence_status",
        "retryable",
        "reason",
        "representation",
        "measurement",
        "observed_bytes",
        "limit_bytes",
        "cost",
        "usage",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "session_id",
        "operation_id",
        "effect_key",
        "attempt_id",
        "session_operation_id",
        "session_operation_epoch",
        "ownership_generation",
        "lease_sha256",
        "policy_sha256",
        "source_checkpoint_id",
        "source_cursor",
        "source_snapshot_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class PrimaryPaidOverflowTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    maximum_receipt_bytes: int
    reserved_bytes: int
    fallback_contract_sha256: str
    terminal_event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class PrimaryPaidOverflowTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _maximum_receipt_bytes(identity: PaidOverflowIdentity) -> int:
    """Bound every closed optional field at its validated maximum encoding."""
    definitions = PaidOverflowReceipt.model_json_schema().get("$defs", {})
    properties = PaidOverflowReceipt.model_json_schema().get("properties", {})
    cost_properties = PaidOverflowCost.model_json_schema().get("properties", {})
    usage_properties = PaidOverflowUsage.model_json_schema().get("properties", {})
    amount_alternatives = cost_properties.get("amount", {}).get("anyOf", ())
    if (
        frozenset(PaidOverflowReceipt.model_fields) != _RECEIPT_FIELDS
        or frozenset(PaidOverflowIdentity.model_fields) != _IDENTITY_FIELDS
        or frozenset(PaidOverflowCost.model_fields) != frozenset({"status", "amount", "source"})
        or frozenset(PaidOverflowUsage.model_fields)
        != frozenset({"status", "model", "prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens", "finished_at"})
        or definitions.get("_Ordinal", {}).get("maximum") != CANONICAL_INTEGER_MAX
        or definitions.get("_LedgerTokens", {}).get("maximum") != LEDGER_TOKENS_MAX
        or properties.get("limit_bytes", {}).get("maximum") != CANONICAL_INTEGER_MAX
        or usage_properties.get("model", {}).get("anyOf")
        != [{"maxLength": MODEL_MAX_LENGTH, "minLength": 1, "type": "string"}, {"type": "null"}]
        or usage_properties.get("finished_at", {}).get("anyOf")
        != [{"maxLength": FINISHED_AT_MAX_LENGTH, "minLength": 1, "type": "string"}, {"type": "null"}]
        or not amount_alternatives
        or amount_alternatives[0].get("maxLength") != COST_AMOUNT_MAX_LENGTH
        or amount_alternatives[0].get("pattern") != r"^(0|[1-9][0-9]*)(\.[0-9]+)?(e-?[0-9]+)?$"
        or tuple(definitions.get("PaidOverflowReason", {}).get("enum", ())) != get_args(PaidOverflowReason.__value__)
        or tuple(definitions.get("PaidOverflowRepresentation", {}).get("enum", ())) != get_args(PaidOverflowRepresentation.__value__)
        or tuple(definitions.get("PaidOverflowCostStatus", {}).get("enum", ())) != get_args(PaidOverflowCostStatus.__value__)
        or tuple(definitions.get("PaidOverflowUsageStatus", {}).get("enum", ())) != get_args(PaidOverflowUsageStatus.__value__)
        or tuple(cost_properties.get("source", {}).get("enum", ())) != get_args(ComposerLLMProviderCostSource)
        or properties.get("producer", {}).get("enum") != ["primary", "title", "semantic_advisor", "manual_advisor", "checkpoint_advisor"]
        or properties.get("outcome", {}).get("const") != "terminal_paid_overflow"
        or properties.get("evidence_status", {}).get("const") != "incomplete"
        or properties.get("retryable", {}).get("const") is not False
        or properties.get("measurement", {}).get("enum") != ["exact", "at_least"]
    ):
        raise AuditIntegrityError("PRIMARY terminal bound no longer covers the closed paid-overflow receipt")
    sources = get_args(ComposerLLMProviderCostSource)
    if not sources or any(type(source) is not str for source in sources):
        raise AuditIntegrityError("PRIMARY terminal cost provenance is not closed")
    envelope = {
        "schema_version": "composer.paid-overflow.v1",
        "identity": identity.model_dump(mode="json"),
        "producer": "primary",
        "outcome": "terminal_paid_overflow",
        "evidence_status": "incomplete",
        "retryable": False,
        "reason": max(get_args(PaidOverflowReason.__value__), key=lambda value: len(value.encode("utf-8"))),
        "representation": max(get_args(PaidOverflowRepresentation.__value__), key=lambda value: len(value.encode("utf-8"))),
        "measurement": "at_least",
        "observed_bytes": CANONICAL_INTEGER_MAX,
        "limit_bytes": CANONICAL_INTEGER_MAX,
        "cost": {
            "status": max(get_args(PaidOverflowCostStatus.__value__), key=lambda value: len(value.encode("utf-8"))),
            "amount": "9" * COST_AMOUNT_MAX_LENGTH,
            "source": max(sources, key=lambda value: len(canonical_json(value).encode("utf-8"))),
        },
        "usage": {
            "status": max(get_args(PaidOverflowUsageStatus.__value__), key=lambda value: len(value.encode("utf-8"))),
            "model": "\x00" * MODEL_MAX_LENGTH,
            "prompt_tokens": LEDGER_TOKENS_MAX,
            "completion_tokens": LEDGER_TOKENS_MAX,
            "cached_prompt_tokens": LEDGER_TOKENS_MAX,
            "reasoning_tokens": LEDGER_TOKENS_MAX,
            "finished_at": "0" * FINISHED_AT_MAX_LENGTH,
        },
    }
    return len(canonical_json(envelope).encode("utf-8"))


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
    receipt_key = f"paid-overflow:{receipt_digest}"
    receipt_chunk_key = f"value:{_RECEIPT_CODEC}:{receipt_digest}"
    frontier = tuple(
        EffectFrontierEntry(effect_id=item.effect_id, disposition="paid_overflow", receipt_id=receipt_key)
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
                    "owner": "composer_primary_paid_overflow",
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


def prepare_primary_paid_overflow_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> PrimaryPaidOverflowTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("PRIMARY terminal plan requires owned original identity and head")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("PRIMARY terminal plan requires its positive creator sequence")
    maximum_receipt_bytes = _maximum_receipt_bytes(identity)
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
            "version": "composer.primary-paid-overflow-tail.v1",
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
    return PrimaryPaidOverflowTailPlan(
        identity,
        admitted,
        creator_sequence,
        maximum_receipt_bytes,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
        (event_bytes[0], event_bytes[1]),
        terminal_bytes,
    )


def build_primary_paid_overflow_tail_batch(
    plan: PrimaryPaidOverflowTailPlan, *, receipt: PaidOverflowReceipt, published_at: datetime
) -> PrimaryPaidOverflowTailBatch:
    if type(plan) is not PrimaryPaidOverflowTailPlan or type(receipt) is not PaidOverflowReceipt:
        raise TypeError("PRIMARY terminal batch requires original nominal plan and receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("PRIMARY terminal publication requires aware database time")
    if (
        receipt.producer != "primary"
        or receipt.identity.effect_key != plan.identity.effect_key
        or (receipt.reason == "private_document_bytes" and receipt.representation not in ("physical_audit", "response", "recorder"))
        or (receipt.reason == "evidence_pair_bytes" and receipt.representation != "evidence_pair")
        or (receipt.reason == "aggregate_retained_bytes" and receipt.representation != "retained_history")
    ):
        raise AuditIntegrityError("PRIMARY terminal receipt changed its size category")
    receipt_json = encode_paid_overflow_receipt(receipt, expected=plan.identity)
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
    return PrimaryPaidOverflowTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
