from __future__ import annotations

import hashlib
from dataclasses import dataclass as stdlib_dataclass
from typing import Final, Literal

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from elspeth.contracts._canonical_json_native import canonical_json_native_strings
from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateFailureFacts, PrivateReceivedDiagnostic, PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.composer_provider_context import PrivateProviderContext
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer import continuation_codec
from elspeth.web.composer.audit import PrivatePlannerCandidate
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    encode_private_chunk,
    owned_json,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.continuation_retention_memo import (
    PrivateChunkVerificationKey,
    RecorderBlockHashKey,
    RecorderRequestTextDependencies,
    RecorderRequestTextKey,
    RecorderRequestTextProof,
    current_retention_stage_memo,
    current_v4_preparation_memo,
)


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class RecorderDrainCursors:
    invocations: int = 0
    llm_calls: int = 0
    planner_attempts: int = 0
    withheld_replies: int = 0
    visible_tool_events: int = 0
    private_candidates: int = 0
    private_authorings: int = 0


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class RetainedVisibleLink:
    invocation_index: int
    event: VisibleToolEvent


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class RecorderSnapshot:
    invocations: tuple[ComposerToolInvocation, ...]
    llm_calls: tuple[ComposerLLMCall, ...]
    planner_attempts: tuple[ComposerPlannerAttempt, ...]
    withheld_replies: tuple[WithheldReply, ...]
    visible_tool_events: tuple[RetainedVisibleLink, ...]
    private_candidates: tuple[PrivatePlannerCandidate, ...]
    private_authorings: tuple[PrivateToolAuthoring, ...]
    drained: RecorderDrainCursors

    def validate_links_and_cursors(self) -> None:
        cursor_pairs = (
            (self.drained.invocations, len(self.invocations)),
            (self.drained.llm_calls, len(self.llm_calls)),
            (self.drained.planner_attempts, len(self.planner_attempts)),
            (self.drained.withheld_replies, len(self.withheld_replies)),
            (self.drained.visible_tool_events, len(self.visible_tool_events)),
            (self.drained.private_candidates, len(self.private_candidates)),
            (self.drained.private_authorings, len(self.private_authorings)),
        )
        if any(type(cursor) is not int or not 0 <= cursor <= length for cursor, length in cursor_pairs):
            raise AuditIntegrityError("buffer drain cursor exceeds its retained channel")
        indexes = tuple(link.invocation_index for link in self.visible_tool_events)
        if len(set(indexes)) != len(indexes) or any(type(index) is not int or not 0 <= index < len(self.invocations) for index in indexes):
            raise AuditIntegrityError("visible buffer event lost its unique invocation alias")
        authoring_ids = tuple(item.tool_call_id for item in self.private_authorings)
        if len(set(authoring_ids)) != len(authoring_ids):
            raise AuditIntegrityError("buffer repeats a private authoring identity")


@stdlib_dataclass(frozen=True, slots=True)
class RecorderDocumentData:
    """Actual admitted source and its detached immutable recorder DATA."""

    chunk: PrivateChunk
    snapshot: RecorderSnapshot


def _recorder_data_children(value: object) -> tuple[object, ...] | None:
    """Closed owned record fields for retained residence, never serialization.

    Only the JSON decoder's detached frozen values enter the production memo.
    Unknown future record types decline retention instead of changing admission.
    The field-completeness control checks these actual direct attribute reads.
    """
    if type(value) is RecorderDocumentData:
        return (value.chunk, value.snapshot)
    if type(value) is PrivateChunk:
        return (value.reference, value.canonical)
    if type(value) is RecorderSnapshot:
        return (
            value.invocations,
            value.llm_calls,
            value.planner_attempts,
            value.withheld_replies,
            value.visible_tool_events,
            value.private_candidates,
            value.private_authorings,
            value.drained,
        )
    if type(value) is RecorderDrainCursors:
        return (
            value.invocations,
            value.llm_calls,
            value.planner_attempts,
            value.withheld_replies,
            value.visible_tool_events,
            value.private_candidates,
            value.private_authorings,
        )
    if type(value) is RetainedVisibleLink:
        return (value.invocation_index, value.event)
    if type(value) is ComposerToolInvocation:
        return (
            value.tool_call_id,
            value.tool_name,
            value.arguments_canonical,
            value.arguments_hash,
            value.result_canonical,
            value.result_hash,
            value.status,
            value.error_class,
            value.error_message,
            value.version_before,
            value.version_after,
            value.started_at,
            value.finished_at,
            value.latency_ms,
            value.actor,
            value.cache_hit,
            value.authority_arguments_canonical,
            value.authority_arguments_hash,
            value.error_category,
            value.strict_sent,
            value.wire_conformant,
            value.private_authoring,
            value.private_failure,
        )
    if type(value) is ComposerLLMCall:
        return (
            value.model_requested,
            value.model_returned,
            value.status,
            value.prompt_tokens,
            value.completion_tokens,
            value.total_tokens,
            value.latency_ms,
            value.provider_request_id,
            value.messages_hash,
            value.tools_spec_hash,
            value.declared_tool_names,
            value.started_at,
            value.finished_at,
            value.error_class,
            value.error_message,
            value.temperature,
            value.seed,
            value.finish_reason,
            value.cached_prompt_tokens,
            value.cache_creation_input_tokens,
            value.cache_read_input_tokens,
            value.reasoning_tokens,
            value.reasoning_content,
            value.reasoning_details,
            value.thinking_blocks,
            value.provider_cost,
            value.pricing_model,
            value.provider_cost_source,
            value.cost_response_accountable,
            value.max_completion_tokens_requested,
            value.planner_policy_hash,
            value.planner_call_ordinal,
            value.call_id,
            value.provider_served,
            value.tool_contract_dialect,
            value.strict_tool_count,
            value.private_failure,
            value.private_diagnostics,
            value.private_request,
        )
    if type(value) is ComposerPlannerAttempt:
        return (
            value.ordinal,
            value.planner_call_ordinal,
            value.phase,
            value.outcome,
            value.planner_code,
            value.selected_tools,
            value.requested_information,
            value.new_information,
            value.rejection_codes,
            value.candidate_shape_hash,
            value.repeated_fingerprint,
            value.led_to,
        )
    if type(value) is WithheldReply:
        return (value.origin, value.content)
    if type(value) is VisibleToolEvent:
        return (
            value.session_id,
            value.operation_id,
            value.operation_epoch,
            value.producer_declared_request_id,
            value.tool_call_id,
            value.tool_name,
            value.display_arguments_hash,
            value.display_result_hash,
            value.payload_json,
            value.payload_hash,
            value.parent_assistant_id,
        )
    if type(value) is PrivatePlannerCandidate:
        return (
            value.evidence_id,
            value.tool_call_id,
            value.tool_name,
            value.arguments_canonical,
            value.authority_arguments_canonical,
            value.raw_arguments,
            value.status,
            value.result_canonical,
        )
    if type(value) is PrivateToolAuthoring:
        return (
            value.tool_call_id,
            value.tool_name,
            value.arguments_canonical,
            value.authority_arguments_canonical,
            value.raw_arguments,
        )
    if type(value) is PrivateFailureFacts:
        return (value.canonical,)
    if type(value) is PrivateReceivedDiagnostic:
        return (
            value.evidence_id,
            value.tool_call_id,
            value.tool_name,
            value.arguments_canonical,
            value.result_canonical,
            value.raw_arguments,
        )
    if type(value) is PrivateProviderContext:
        return (value.disposition, value.canonical, value.reasons)
    return None


_BUFFER_CODEC = TypeAdapter(RecorderSnapshot)


RECORDER_CODEC: Final[Literal["composer.audit-buffer.v2"]] = "composer.audit-buffer.v2"
_TRUSTED_RECORDER_BLOCK_SHA256: Final = hashlib.sha256
_DOCUMENT_KEYS = frozenset({"blocks", "snapshot"})
_CONTEXT_KEYS = frozenset({"capture_boundary", "messages", "schema_version", "tools"})
_STORED_REQUEST_KEYS = frozenset({"disposition", "capture_boundary", "messages", "schema_version", "tools"})


def _block_key(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _RecorderBlocks:
    """One recorder document's content-addressed blocks.

    Every retained call repeats the system prompt, skill text, tool specs and
    the history prefix. Each distinct message and tools list is stored once
    per recorder document, so its size grows with distinct content and the
    number of calls, not with their product.
    """

    __slots__ = ("_used", "_values")

    def __init__(self) -> None:
        self._values = dict[str, object]()
        self._used = set[str]()

    @classmethod
    def admit(cls, stored: object) -> _RecorderBlocks:
        if type(stored) is not dict or any(type(key) is not str or _block_key(block) != key for key, block in stored.items()):
            raise AuditIntegrityError("audit buffer block does not match its content address")
        blocks = cls()
        blocks._values.update(stored)
        return blocks

    def store(self, value: object) -> str:
        key = _block_key(value)
        self._values.setdefault(key, value)
        return key

    def store_proven_text(self, key: str, value: object) -> None:
        """First-wins insertion after the private immutable text transfer binds the key."""
        self._values.setdefault(key, value)

    def resolve(self, key: object) -> object:
        if type(key) is not str or key not in self._values:
            raise AuditIntegrityError("audit buffer retained request names an absent block")
        self._used.add(key)
        return self._values[key]

    def require_all_used(self) -> None:
        if self._used != set(self._values):
            raise AuditIntegrityError("audit buffer carries an unreferenced block")

    def document(self) -> object:
        return dict(self._values)


def _deduplicate_request(request: object, blocks: _RecorderBlocks) -> object:
    """Store one retained provider context as refs to content-addressed blocks."""
    if type(request) is not dict or request.get("disposition") != "retained":
        return request
    if set(request) != {"disposition", "canonical", "reasons"} or request["reasons"] or type(request["canonical"]) is not str:
        raise AuditIntegrityError("retained provider context lost its closed receipt shape")
    key = RecorderRequestTextKey(request["canonical"], CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    memo = current_retention_stage_memo()
    if memo is not None and current_v4_preparation_memo() is not None:
        prepared = memo.v4_recorder_request_text(
            key,
            RecorderRequestTextDependencies(parse_json_strict, canonical_json, hashlib.sha256, _CONTEXT_KEYS),
            _prepared_request_text,
        )
    else:
        prepared = memo.recorder_request_text(key, _prepared_request_text) if memo is not None else _prepared_request_text(key)
    return request if prepared is None else _transfer_prepared_request_text(prepared, blocks)


def _prepared_request_text(key: RecorderRequestTextKey) -> RecorderRequestTextProof | None:
    """Only immutable complete-text/ref/block encodings cross a stage memo."""
    context, error = parse_json_strict(key.canonical)
    if (
        error is not None
        or type(context) is not dict
        or set(context) != _CONTEXT_KEYS
        or type(context["messages"]) is not list
        or canonical_json(context) != key.canonical
    ):
        # Not the prepared capture document: retain its exact bytes unchanged.
        return None
    proven_blocks: dict[str, str] = {}

    def block_key(value: object) -> str:
        canonical = canonical_json(value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        proven_blocks.setdefault(digest, canonical)
        return digest

    ref_document = {
        "disposition": "retained",
        "schema_version": context["schema_version"],
        "capture_boundary": context["capture_boundary"],
        "messages": [block_key(message) for message in context["messages"]],
        "tools": None if context["tools"] is None else block_key(context["tools"]),
    }
    return RecorderRequestTextProof(canonical_json(ref_document), tuple(proven_blocks.items()))


def _transfer_prepared_request_text(proof: RecorderRequestTextProof, blocks: _RecorderBlocks) -> object:
    """Rebuild fresh mutable values from a private producer's immutable proof."""
    if type(proof) is not RecorderRequestTextProof:
        raise AuditIntegrityError("recorder request lost its exact text proof")
    stored, error = parse_json_strict(proof.ref_document_json)
    if error is not None or type(stored) is not dict or set(stored) != _STORED_REQUEST_KEYS or type(stored["messages"]) is not list:
        raise AuditIntegrityError("recorder request lost its prepared ref document")
    expected = (*stored["messages"], *((stored["tools"],) if stored["tools"] is not None else ()))
    if any(type(item) is not str for item in expected) or set(expected) != {key for key, _ in proof.blocks}:
        raise AuditIntegrityError("recorder request lost its complete block references")
    for key, canonical in proof.blocks:
        if type(key) is not str or type(canonical) is not str:
            raise AuditIntegrityError("recorder request block text differs from its content address")
        hash_key = RecorderBlockHashKey(key, canonical)
        memo = current_retention_stage_memo()
        if memo is None or hashlib.sha256 is not _TRUSTED_RECORDER_BLOCK_SHA256:
            _require_recorder_block_hash(hash_key)
        else:
            memo.recorder_block_hash(hash_key, _require_recorder_block_hash)
        value, error = parse_json_strict(canonical)
        if error is not None:
            raise AuditIntegrityError("recorder request block is not strict JSON")
        blocks.store_proven_text(key, value)
    return stored


def _require_recorder_block_hash(key: RecorderBlockHashKey) -> None:
    """Prove only immutable text binding; each transfer still parses fresh values."""
    if hashlib.sha256(key.canonical.encode("utf-8")).hexdigest() != key.expected_digest:
        raise AuditIntegrityError("recorder request block text differs from its content address")


def _restore_request(stored: object, blocks: _RecorderBlocks) -> object:
    if type(stored) is not dict or stored.get("disposition") != "retained" or "canonical" in stored:
        return stored
    if set(stored) != _STORED_REQUEST_KEYS or type(stored["messages"]) is not list:
        raise AuditIntegrityError("audit buffer retained request lost its closed block-reference shape")
    context = {
        "schema_version": stored["schema_version"],
        "capture_boundary": stored["capture_boundary"],
        "messages": [blocks.resolve(key) for key in stored["messages"]],
        "tools": None if stored["tools"] is None else blocks.resolve(stored["tools"]),
    }
    memo = current_retention_stage_memo()
    canonical = None
    if memo is not None and current_v4_preparation_memo() is not None:
        canonical = memo.v4_restored_recorder_request_text(
            canonical_json(stored),
            CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
            RecorderRequestTextDependencies(parse_json_strict, canonical_json, hashlib.sha256, _CONTEXT_KEYS),
            _prepared_request_text,
        )
    if canonical is None:
        canonical = canonical_json(context)
    return {"disposition": "retained", "canonical": canonical, "reasons": []}


def recorder_snapshot_json(snapshot: RecorderSnapshot) -> str:
    """Project the exact v2 document without claiming storage admission."""
    if type(snapshot) is not RecorderSnapshot:
        raise TypeError("audit buffer requires its owned snapshot")
    snapshot.validate_links_and_cursors()
    projected = _BUFFER_CODEC.dump_python(
        snapshot,
        mode="json",
        warnings="error",
        fallback=owned_json,
    )
    blocks = _RecorderBlocks()
    for call in projected["llm_calls"]:
        if call["cost_response_accountable"] is None:
            del call["cost_response_accountable"]
        call["private_request"] = _deduplicate_request(call["private_request"], blocks)
    return canonical_json_native_strings({"blocks": blocks.document(), "snapshot": projected})


def encode_recorder_snapshot(snapshot: RecorderSnapshot) -> PrivateChunk:
    return encode_private_chunk(codec=RECORDER_CODEC, canonical=recorder_snapshot_json(snapshot))


def _encode_and_decode_recorder_document(canonical: str) -> tuple[PrivateChunk, RecorderSnapshot]:
    """Admit fresh text and return its original chunk with detached DATA."""
    _require_recorder_document_capacity(canonical)
    encoded = canonical.encode("utf-8")
    reference = PrivateChunkRef(codec=RECORDER_CODEC, digest=hashlib.sha256(encoded).hexdigest(), byte_length=len(encoded))
    key = PrivateChunkVerificationKey(reference, canonical)

    def construct() -> RecorderDocumentData:
        return _construct_recorder_document(canonical)

    memo = current_retention_stage_memo()
    data = memo.recorder_data(key, construct) if memo is not None else construct()
    verify_private_chunk(reference, canonical)
    if data.chunk.reference != reference or data.chunk.canonical != canonical:
        raise AuditIntegrityError("recorder document differs from its actual source")
    return data.chunk, data.snapshot


def _require_recorder_document_capacity(canonical: str) -> None:
    if utf8_size(canonical, limit=continuation_codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) > (
        continuation_codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    ):
        # Preserve the encoder's original exact overflow witness and refusal.
        encode_private_chunk(codec=RECORDER_CODEC, canonical=canonical)


def _construct_recorder_document(source: str | PrivateChunk) -> RecorderDocumentData:
    if isinstance(source, str):
        chunk = encode_private_chunk(codec=RECORDER_CODEC, canonical=source)
        snapshot = _decode_recorder_document(source)
        projected = recorder_snapshot_json(snapshot)
        if projected != source:
            encode_private_chunk(codec=RECORDER_CODEC, canonical=projected)
            raise AuditIntegrityError("audit buffer omitted or normalized a retained field")
    else:
        snapshot = _decode_recorder_document(source.canonical)
        chunk = encode_private_chunk(codec=RECORDER_CODEC, canonical=recorder_snapshot_json(snapshot))
        if chunk != source:
            raise AuditIntegrityError("audit buffer omitted or normalized a retained field")
    return RecorderDocumentData(chunk, snapshot)


def validate_recorder_projection(snapshot: RecorderSnapshot) -> tuple[str, RecorderSnapshot]:
    """Validate complete prospective DATA before deciding its capacity.

    This returns no admitted chunk. The same decoder and exact v2 projection
    retain all original constructors, block checks and field identity.
    """
    canonical = recorder_snapshot_json(snapshot)
    restored = _decode_recorder_document(canonical)
    if recorder_snapshot_json(restored) != canonical:
        raise AuditIntegrityError("audit buffer omitted or normalized a retained field")
    return canonical, restored


def decode_recorder_snapshot(chunk: PrivateChunk) -> RecorderSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != RECORDER_CODEC:
        raise AuditIntegrityError("audit buffer has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)

    def construct() -> RecorderDocumentData:
        return _construct_recorder_document(chunk)

    memo = current_retention_stage_memo()
    data = memo.recorder_data(key, construct) if memo is not None else construct()
    _require_recorder_document_capacity(chunk.canonical)
    if data.chunk != chunk:
        raise AuditIntegrityError("recorder document differs from its actual source")
    return data.snapshot


def _decode_recorder_document(canonical: str) -> RecorderSnapshot:
    value, error = parse_json_strict(canonical)
    if error is not None:
        raise AuditIntegrityError("audit buffer is not strict JSON")
    if type(value) is not dict or set(value) != _DOCUMENT_KEYS or type(value["blocks"]) is not dict or type(value["snapshot"]) is not dict:
        raise AuditIntegrityError("audit buffer lost its closed block document shape")
    blocks, projected = _RecorderBlocks.admit(value["blocks"]), value["snapshot"]
    calls = projected.get("llm_calls")
    if type(calls) is not list or any(type(call) is not dict or "private_request" not in call for call in calls):
        raise AuditIntegrityError("audit buffer lost its retained provider call shape")
    for call in calls:
        call["private_request"] = _restore_request(call["private_request"], blocks)
    blocks.require_all_used()
    try:
        snapshot = _BUFFER_CODEC.validate_json(
            canonical_json(projected),
            strict=True,
            extra="forbid",
        )
    except ValidationError as exc:
        raise AuditIntegrityError("audit buffer violates its owned codec") from exc
    snapshot.validate_links_and_cursors()
    return snapshot
