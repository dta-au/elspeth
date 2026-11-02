from __future__ import annotations

import hashlib
from typing import Final, Literal

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.audit import PrivatePlannerCandidate
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    encode_private_chunk,
    owned_json,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.continuation_retention_memo import RecorderRequestTextKey, RecorderRequestTextProof, current_retention_stage_memo


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


_BUFFER_CODEC = TypeAdapter(RecorderSnapshot)


RECORDER_CODEC: Final[Literal["composer.audit-buffer.v2"]] = "composer.audit-buffer.v2"
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
        if type(key) is not str or type(canonical) is not str or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != key:
            raise AuditIntegrityError("recorder request block text differs from its content address")
        value, error = parse_json_strict(canonical)
        if error is not None:
            raise AuditIntegrityError("recorder request block is not strict JSON")
        blocks.store_proven_text(key, value)
    return stored


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
    return {"disposition": "retained", "canonical": canonical_json(context), "reasons": []}


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
    return canonical_json({"blocks": blocks.document(), "snapshot": projected})


def encode_recorder_snapshot(snapshot: RecorderSnapshot) -> PrivateChunk:
    return encode_private_chunk(codec=RECORDER_CODEC, canonical=recorder_snapshot_json(snapshot))


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
    snapshot = _decode_recorder_document(chunk.canonical)
    if encode_recorder_snapshot(snapshot).canonical != chunk.canonical:
        raise AuditIntegrityError("audit buffer omitted or normalized a retained field")
    return snapshot


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
