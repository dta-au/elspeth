"""Conservative closed TITLE paid-overflow tail bound, without SQL authority.

The envelope is source-specific: it includes the full known terminal snapshot,
both attribution events, and a structural upper envelope for the closed V4
codec. The caller must still reserve this plan with the original SQL owner and
write the exact built batch in the same transaction as terminal settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import get_args

from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
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

_EFFECT = "provider:3:0"
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
_COST_FIELDS = frozenset({"status", "amount", "source"})
_USAGE_FIELDS = frozenset(
    {"status", "model", "prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens", "finished_at"}
)


@dataclass(frozen=True, slots=True)
class TitlePaidOverflowTailPlan:
    identity: PaidOverflowIdentity
    admitted: StoredContinuationSnapshot
    creator_sequence: int
    maximum_receipt_bytes: int  # Conservative envelope; no claim of attainable equality.
    reserved_bytes: int
    fallback_contract_sha256: str
    terminal_event_bytes: tuple[int, int]
    terminal_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class TitlePaidOverflowTailBatch:
    receipt_json: str
    receipt_key: str
    receipt_chunk_key: str
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]


def _require_closed_receipt_shape() -> None:
    schema = PaidOverflowReceipt.model_json_schema()
    definitions = schema.get("$defs", {})
    properties = schema.get("properties", {})
    cost = PaidOverflowCost.model_json_schema()
    usage = PaidOverflowUsage.model_json_schema()
    cost_properties = cost.get("properties", {})
    usage_properties = usage.get("properties", {})
    amount_alternatives = cost_properties.get("amount", {}).get("anyOf", ())
    reason_values = get_args(PaidOverflowReason.__value__)
    representation_values = get_args(PaidOverflowRepresentation.__value__)
    cost_status_values = get_args(PaidOverflowCostStatus.__value__)
    usage_status_values = get_args(PaidOverflowUsageStatus.__value__)
    if (
        frozenset(PaidOverflowReceipt.model_fields) != _RECEIPT_FIELDS
        or frozenset(PaidOverflowIdentity.model_fields) != _IDENTITY_FIELDS
        or frozenset(PaidOverflowCost.model_fields) != _COST_FIELDS
        or frozenset(PaidOverflowUsage.model_fields) != _USAGE_FIELDS
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
        or tuple(definitions.get("PaidOverflowReason", {}).get("enum", ())) != reason_values
        or tuple(definitions.get("PaidOverflowRepresentation", {}).get("enum", ())) != representation_values
        or tuple(definitions.get("PaidOverflowCostStatus", {}).get("enum", ())) != cost_status_values
        or tuple(definitions.get("PaidOverflowUsageStatus", {}).get("enum", ())) != usage_status_values
        or tuple(cost_properties.get("source", {}).get("enum", ())) != get_args(ComposerLLMProviderCostSource)
        or properties.get("producer", {}).get("enum") != ["primary", "title", "semantic_advisor", "manual_advisor", "checkpoint_advisor"]
        or properties.get("outcome", {}).get("const") != "terminal_paid_overflow"
        or properties.get("evidence_status", {}).get("const") != "incomplete"
        or properties.get("retryable", {}).get("const") is not False
        or properties.get("measurement", {}).get("enum") != ["exact", "at_least"]
    ):
        raise AuditIntegrityError("TITLE fallback bound no longer covers the complete paid-overflow receipt")


def _maximum_receipt_bytes(identity: PaidOverflowIdentity) -> int:
    """Upper-bound each unknown field by its enforced maximum JSON encoding.

    A control character takes six JSON bytes per model character; all other
    Unicode characters take at most four UTF-8 bytes. The timestamp grammar is
    ASCII and its field has a 64-character cap. Known identity is measured as
    its actual canonical bytes, not a guessed UUID or digest allowance.
    """
    _require_closed_receipt_shape()
    sources = get_args(ComposerLLMProviderCostSource)
    if not sources or any(type(source) is not str for source in sources):
        raise AuditIntegrityError("TITLE fallback cost source domain is not closed")
    longest_source = max(sources, key=lambda source: len(canonical_json(source).encode("utf-8")))
    longest_reason = max(get_args(PaidOverflowReason.__value__), key=lambda value: len(value.encode("utf-8")))
    longest_representation = max(get_args(PaidOverflowRepresentation.__value__), key=lambda value: len(value.encode("utf-8")))
    longest_cost_status = max(get_args(PaidOverflowCostStatus.__value__), key=lambda value: len(value.encode("utf-8")))
    longest_usage_status = max(get_args(PaidOverflowUsageStatus.__value__), key=lambda value: len(value.encode("utf-8")))
    envelope = {
        "schema_version": "composer.paid-overflow.v1",
        "identity": identity.model_dump(mode="json"),
        "producer": "title",
        "outcome": "terminal_paid_overflow",
        "evidence_status": "incomplete",
        "retryable": False,
        "reason": longest_reason,
        "representation": longest_representation,
        "measurement": "at_least",
        "observed_bytes": CANONICAL_INTEGER_MAX,
        "limit_bytes": CANONICAL_INTEGER_MAX,
        "cost": {"status": longest_cost_status, "amount": "9" * COST_AMOUNT_MAX_LENGTH, "source": longest_source},
        "usage": {
            "status": longest_usage_status,
            "model": "\x00" * MODEL_MAX_LENGTH,
            "prompt_tokens": LEDGER_TOKENS_MAX,
            "completion_tokens": LEDGER_TOKENS_MAX,
            "cached_prompt_tokens": LEDGER_TOKENS_MAX,
            "reasoning_tokens": LEDGER_TOKENS_MAX,
            "finished_at": "0" * FINISHED_AT_MAX_LENGTH,
        },
    }
    # The envelope deliberately combines incompatible field values. It is an
    # upper bound, never an attainable 'largest receipt' or exact minimum hold.
    return len(canonical_json(envelope).encode("utf-8"))


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
        raise AuditIntegrityError("TITLE fallback lost its actual sole dispatched provider source")
    receipt_key = f"paid-overflow:{receipt_digest}"
    receipt_chunk_key = f"value:{_RECEIPT_CODEC}:{receipt_digest}"
    frontier = (EffectFrontierEntry(effect_id=_EFFECT, disposition="paid_overflow", receipt_id=receipt_key),)
    terminal = root.model_copy(update={"effect_frontier": frontier, "pending_settlement_ids": ()})
    terminal_json = encode_continuation_snapshot(terminal)
    events = (
        ContinuationEvent(
            "checkpoint",
            canonical_json(
                {
                    "owner": "composer_title_paid_overflow",
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


def prepare_title_paid_overflow_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> TitlePaidOverflowTailPlan:
    if type(identity) is not PaidOverflowIdentity or type(admitted) is not StoredContinuationSnapshot:
        raise TypeError("TITLE fallback requires owned original identity and admitted root")
    if type(creator_sequence) is not int or creator_sequence <= 0:
        raise ValueError("TITLE fallback requires its original positive creator sequence")
    maximum_receipt_bytes = _maximum_receipt_bytes(identity)
    snapshot_json, events, _, _ = _terminal_shape(admitted, identity, creator_sequence, "0" * 64)
    event_bytes = (
        len(events[0].payload_json.encode("utf-8")),
        len(events[1].payload_json.encode("utf-8")),
    )
    terminal_bytes = len(
        encode_continuation_snapshot(
            decode_continuation_snapshot(snapshot_json).model_copy(update={"cursor": admitted.cursor + len(events)})
        ).encode("utf-8")
    )
    reserved = maximum_receipt_bytes + sum(event_bytes) + terminal_bytes
    manifest = canonical_json(
        {
            "version": "composer.title-paid-overflow-tail.v1",
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
    return TitlePaidOverflowTailPlan(
        identity,
        admitted,
        creator_sequence,
        maximum_receipt_bytes,
        reserved,
        sha256(manifest.encode("utf-8")).hexdigest(),
        event_bytes,
        terminal_bytes,
    )


def build_title_paid_overflow_tail_batch(
    plan: TitlePaidOverflowTailPlan, *, receipt: PaidOverflowReceipt, published_at: datetime
) -> TitlePaidOverflowTailBatch:
    """Build the only writable fallback batch; a changed shape fails closed."""
    if type(plan) is not TitlePaidOverflowTailPlan or type(receipt) is not PaidOverflowReceipt:
        raise TypeError("TITLE fallback requires its original plan and nominal receipt")
    if type(published_at) is not datetime or published_at.tzinfo is None:
        raise ValueError("TITLE fallback requires an aware database publication time")
    if (
        receipt.producer != "title"
        or receipt.identity.effect_key != _EFFECT
        or (receipt.reason == "private_document_bytes" and receipt.representation not in ("physical_audit", "response"))
        or (receipt.reason == "retained_payload_bytes" and receipt.representation != "retained_tail")
        or receipt.reason == "evidence_pair_bytes"
    ):
        raise AuditIntegrityError("TITLE fallback receipt changed its closed producer/effect/size category")
    receipt_json = encode_paid_overflow_receipt(receipt, expected=plan.identity)
    if len(receipt_json.encode("utf-8")) > plan.maximum_receipt_bytes:
        raise AuditIntegrityError("TITLE fallback receipt exceeded its original closed bound")
    digest = sha256(receipt_json.encode("utf-8")).hexdigest()
    snapshot_json, events, receipt_key, receipt_chunk_key = _terminal_shape(plan.admitted, plan.identity, plan.creator_sequence, digest)
    append = prepare_snapshot_append(plan.admitted, snapshot_json=snapshot_json, events=events, published_at=published_at)
    receipt_chunk = encode_private_chunk(codec=_RECEIPT_CODEC, canonical=receipt_json)
    if private_chunk_key(receipt_chunk) != receipt_chunk_key:
        raise AuditIntegrityError("TITLE fallback receipt key differs from its complete canonical bytes")
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
        or sum(len(chunk.payload) for chunk in prospective) > plan.reserved_bytes
    ):
        raise AuditIntegrityError("TITLE fallback writer exceeded its original complete terminal-tail shape")
    return TitlePaidOverflowTailBatch(receipt_json, receipt_key, receipt_chunk_key, append, prospective)
