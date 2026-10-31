from __future__ import annotations

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
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply


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


def encode_recorder_snapshot(snapshot: RecorderSnapshot) -> PrivateChunk:
    if type(snapshot) is not RecorderSnapshot:
        raise TypeError("audit buffer requires its owned snapshot")
    snapshot.validate_links_and_cursors()
    projected = _BUFFER_CODEC.dump_python(
        snapshot,
        mode="json",
        warnings="error",
        fallback=owned_json,
    )
    return encode_private_chunk(
        codec="composer.audit-buffer.v1",
        canonical=canonical_json(projected),
    )


def decode_recorder_snapshot(chunk: PrivateChunk) -> RecorderSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.audit-buffer.v1":
        raise AuditIntegrityError("audit buffer has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    value, error = parse_json_strict(chunk.canonical)
    if error is not None:
        raise AuditIntegrityError("audit buffer is not strict JSON")
    try:
        snapshot = _BUFFER_CODEC.validate_json(
            canonical_json(value),
            strict=True,
            extra="forbid",
        )
    except ValidationError as exc:
        raise AuditIntegrityError("audit buffer violates its owned codec") from exc
    snapshot.validate_links_and_cursors()
    if encode_recorder_snapshot(snapshot).canonical != chunk.canonical:
        raise AuditIntegrityError("audit buffer omitted or normalized a retained field")
    return snapshot
