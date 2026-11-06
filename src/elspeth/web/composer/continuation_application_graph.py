"""Actual effects/recorder/batch graph roots for portable P4 application DATA.

The existing coupled encoder owns node identities. This module supplies only
explicit additional nominal members and constructors; no reflective discovery.
Existing admitted state/validation and frozen audit atoms keep their owned value
contracts. Private authoring identity is explicit across recorder and batch.
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateFailureFacts
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ToolContractDialect
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.prompt_guard import PromptGuardContentPart
from elspeth.web.composer.audit import PrivatePlannerCandidate
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import (
    RECORDER_CODEC,
    RecorderDrainCursors,
    RecorderSnapshot,
    RetainedVisibleLink,
    _encode_and_decode_recorder_document,
    decode_recorder_snapshot,
    encode_recorder_snapshot,
    recorder_snapshot_json,
    validate_recorder_projection,
)
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import PrivateChunk, owned_json
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.prompt_guard_rendering import JsonPath, json_content_parts
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import PersistedBatchEffects, PersistedTurnEffects, authored_state_hash
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.credential_guard import require_no_credential_material


@dataclass(frozen=True, slots=True)
class ApplicationGraphCut:
    data: RestoredBatchData
    effects_before: PersistedTurnEffects
    effects_after: PersistedTurnEffects
    recorder_before: RecorderSnapshot
    recorder_after: RecorderSnapshot


@dataclass(frozen=True, slots=True)
class ApplicationGraph:
    selected: RestoredBatchData
    effects: PersistedTurnEffects
    recorder: RecorderSnapshot
    cuts: tuple[ApplicationGraphCut, ...]
    selected_audit: PortableDispatchAudit | None = None
    selected_dialect: ToolContractDialect | None = None


def _render_json_value(encoded: str) -> JsonValue:
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("Application rendering lost its original canonical JSON")
    return cast(JsonValue, value)


def _render_json_parts(
    encoded: str, partitions: dict[JsonPath, tuple[PromptGuardContentPart, ...]] | None = None
) -> tuple[PromptGuardContentPart, ...]:
    return json_content_parts(
        encoded, ensure_ascii=False, separators=(",", ":"), encoder=canonical_json, decoder=_render_json_value, string_parts=partitions
    )


def _p4_input_parts(encoded: str) -> tuple[PromptGuardContentPart, ...]:
    value = _render_json_value(encoded)
    if type(value) is not dict or value["schema_version"] != "composer.prepared-p4.v1":
        raise AuditIntegrityError("Application rendering changed its declared prepared P4 codec")
    partitions: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    # These are serialized members emitted by PreparedP4Value's owner, not
    # arbitrary strings selected because their contents resemble JSON.
    for name in (
        "redacted_calls_json",
        "redacted_rows_json",
        "revoked_rows_json",
        "mutation_ceiling_json",
        "authority_revocation_json",
        "private_tool_evidence_json",
        "private_tool_failures_json",
        "rejection_records_json",
        "withheld_replies_json",
        "source_audit_json",
    ):
        text = value[name]
        if text is None:
            continue
        if type(text) is not str:
            raise AuditIntegrityError("Application rendering changed a prepared P4 JSON member")
        nested: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
        if name in ("redacted_rows_json", "revoked_rows_json"):
            rows = _render_json_value(text)
            if type(rows) is not list:
                raise AuditIntegrityError("Application rendering lost its declared P4 row sequence")
            for index, row in enumerate(rows):
                if type(row) is not dict:
                    raise AuditIntegrityError("Application rendering lost its declared P4 row")
                for member in ("state_payload_json", "visible_event_json"):
                    child = row[member]
                    if child is not None:
                        if type(child) is not str:
                            raise AuditIntegrityError("Application rendering changed a P4 row JSON member")
                        nested[(index, member)] = _render_json_parts(child)
        partitions[(name,)] = _render_json_parts(text, nested)
    return _render_json_parts(encoded, partitions)


def _p4_receipt_parts(encoded: str) -> tuple[PromptGuardContentPart, ...]:
    value = _render_json_value(encoded)
    if type(value) is not dict or value["schema_version"] != "composer.persisted-turn.v2":
        raise AuditIntegrityError("Application rendering changed its declared P4 receipt codec")
    partitions: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    prepared, audit = value["prepared_p4_json"], value["audit_outcome_json"]
    if prepared is not None:
        if type(prepared) is not str:
            raise AuditIntegrityError("Application rendering changed its receipt's prepared P4 input")
        partitions[("prepared_p4_json",)] = _p4_input_parts(prepared)
    if audit is not None:
        if type(audit) is not str:
            raise AuditIntegrityError("Application rendering changed its receipt's audit outcome")
        partitions[("audit_outcome_json",)] = _render_json_parts(audit)
    return _render_json_parts(encoded, partitions)


def application_graph_content_parts(encoded: str) -> tuple[PromptGuardContentPart, ...]:
    """Render already admitted graph DATA without losing owned JSON boundaries.

    This grants no graph, source or reference authority. Only codec-declared
    string wrappers are expanded; original user/provider scalars stay atomic.
    Every unit stays untrusted and recombines into the exact original wire.
    """
    value = _render_json_value(encoded)
    if type(value) is not dict or value["schema_version"] not in (
        "composer.evidence-batch-graph.v2",
        "composer.prepared-evidence-batch-graph.v2",
    ):
        raise AuditIntegrityError("Application rendering requires its declared graph codec")
    nodes = value["nodes"]
    if type(nodes) is not list:
        raise AuditIntegrityError("Application rendering lost its declared graph nodes")
    partitions: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    for index, node in enumerate(nodes):
        if type(node) is not dict:
            raise AuditIntegrityError("Application rendering lost its declared graph node")
        kind, body = node["kind"], node["value"]
        if kind in ("state", "validation", "portable_dispatch_audit"):
            if type(body) is not str:
                raise AuditIntegrityError("Application rendering changed a declared JSON node")
            partitions[("nodes", index, "value")] = _render_json_parts(body)
        elif kind == "ContinuationBatchState":
            if type(body) is not list:
                raise AuditIntegrityError("Application rendering lost its declared batch members")
            for member_index, member in enumerate(body):
                if type(member) is not list or len(member) != 2:
                    raise AuditIntegrityError("Application rendering changed a declared batch member")
                name, scalar = member
                if name not in ("p4_receipt_json", "p4_input_json"):
                    continue
                if type(scalar) is not dict or set(scalar) != {"scalar"}:
                    raise AuditIntegrityError("Application rendering changed a declared batch JSON scalar")
                text = scalar["scalar"]
                if text is not None:
                    if type(text) is not str:
                        raise AuditIntegrityError("Application rendering lost a declared batch JSON text")
                    parts = _p4_receipt_parts(text) if name == "p4_receipt_json" else _p4_input_parts(text)
                    partitions[("nodes", index, "value", member_index, 1, "scalar")] = parts
    return _render_json_parts(encoded, partitions)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ApplicationRecorderSource:
    """Complete source-admitted recorder DATA; never a SQL/publication owner."""

    chunk: PrivateChunk
    snapshot: RecorderSnapshot
    env_ref_names: frozenset[str]

    def __init__(self, source: RecorderSnapshot | PrivateChunk, *, env_ref_names: frozenset[str]) -> None:
        _require_source_environment(env_ref_names)
        if type(source) is RecorderSnapshot:
            chunk, snapshot = _encode_and_decode_recorder_document(recorder_snapshot_json(source))
        elif type(source) is PrivateChunk:
            chunk = source
            _require_source_chunk(chunk)
            # Preserve early source bounds and the public decoder's current
            # verification/schema/projected-admission refusal order.
            snapshot = decode_recorder_snapshot(chunk)
        else:
            raise TypeError("application recorder requires its actual complete source")
        object.__setattr__(self, "chunk", chunk)
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "env_ref_names", env_ref_names)

    @staticmethod
    def _from_canonical(canonical: str, *, env_ref_names: frozenset[str]) -> ApplicationRecorderSource:
        _require_source_environment(env_ref_names)
        chunk, snapshot = _encode_and_decode_recorder_document(canonical)
        source = object.__new__(ApplicationRecorderSource)
        object.__setattr__(source, "chunk", chunk)
        object.__setattr__(source, "snapshot", snapshot)
        object.__setattr__(source, "env_ref_names", env_ref_names)
        return source


@dataclass(frozen=True, slots=True, eq=False)
class ProspectiveApplicationRecorderSource(ApplicationRecorderSource):
    """Complete fresh DATA awaiting capacity; never an admitted stored source.

    The normal stored constructor remains bounded. This separate nominal value
    allows validation of a prospective oversized graph before its final refusal.
    Its document must actually exceed the existing byte bound. Every consumer
    revalidates its original binding; it can never enter admitted evidence.
    """


@dataclass(frozen=True, slots=True)
class _ValidatedRecorderSource:
    """One synchronous graph call's checked source, rechecked at its exit."""

    source: ApplicationRecorderSource
    env_ref_names: frozenset[str]
    snapshot: RecorderSnapshot
    original_chunk: tuple[str, str, int, str]

    @classmethod
    def capture(cls, source: ApplicationRecorderSource, env_ref_names: frozenset[str]) -> _ValidatedRecorderSource:
        require_application_recorder_source(source, env_ref_names)
        reference = source.chunk.reference
        return cls(
            source, env_ref_names, source.snapshot, (reference.codec, reference.digest, reference.byte_length, source.chunk.canonical)
        )

    def require_bound(self, source: ApplicationRecorderSource, env_ref_names: frozenset[str]) -> None:
        reference = source.chunk.reference
        if (
            type(self) is not _ValidatedRecorderSource
            or self.source is not source
            or self.env_ref_names != env_ref_names
            or source.env_ref_names != env_ref_names
            or self.snapshot is not source.snapshot
            or self.original_chunk != (reference.codec, reference.digest, reference.byte_length, source.chunk.canonical)
        ):
            raise AuditIntegrityError("application graph lost its call-local checked recorder source")


@dataclass(frozen=True, slots=True)
class _ValidatedSelectedRecorder:
    """A selected graph root checked once within one synchronous projection."""

    graph: ApplicationGraph
    source: ApplicationRecorderSource | None
    chunk: PrivateChunk

    @classmethod
    def capture(cls, graph: ApplicationGraph, source: ApplicationRecorderSource | None) -> _ValidatedSelectedRecorder:
        if type(graph) is not ApplicationGraph or type(graph.cuts) is not tuple:
            raise TypeError("application graph requires its actual owned roots")
        return cls(graph, source, application_recorder_chunk(graph.recorder, recorder_source=source))

    def require_bound(self, graph: ApplicationGraph, source: ApplicationRecorderSource | None) -> None:
        if (
            type(self) is not _ValidatedSelectedRecorder
            or self.graph is not graph
            or self.source is not source
            or (source is not None and self.chunk.reference != source.chunk.reference)
        ):
            raise AuditIntegrityError("application graph lost its call-local selected recorder check")


def _prospective_recorder_values(snapshot: RecorderSnapshot, env_ref_names: frozenset[str]) -> tuple[PrivateChunk, RecorderSnapshot]:
    canonical, restored = validate_recorder_projection(snapshot)
    encoded = canonical.encode("utf-8")
    # This is a complete-text security traversal, not a retained-byte allowance.
    # Fresh excess must not stop before a credential at its original tail.
    require_no_credential_material(
        canonical,
        surface="composer_continuation_evidence_recorder",
        env_ref_names=env_ref_names,
        max_string_bytes=max(JSON_MAX_TOTAL_UTF8_BYTES, len(encoded)),
    )
    chunk = PrivateChunk(PrivateChunkRef(codec=RECORDER_CODEC, digest=sha256(encoded).hexdigest(), byte_length=len(encoded)), canonical)
    return chunk, restored


def capture_fresh_application_recorder_source(snapshot: RecorderSnapshot, *, env_ref_names: frozenset[str]) -> ApplicationRecorderSource:
    """Preserve ordinary admission when fitting; defer only actual fresh excess."""
    _require_source_environment(env_ref_names)
    canonical = recorder_snapshot_json(snapshot)
    if utf8_size(canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        return ApplicationRecorderSource._from_canonical(canonical, env_ref_names=env_ref_names)
    chunk, restored = _prospective_recorder_values(snapshot, env_ref_names)
    return ProspectiveApplicationRecorderSource(chunk, restored, env_ref_names)


def require_application_recorder_capacity(source: ApplicationRecorderSource) -> None:
    """Classify valid fresh excess only after its enclosing validation finishes."""
    from elspeth.web.composer.protocol import ComposerContextUnsupported

    if type(source) not in (ApplicationRecorderSource, ProspectiveApplicationRecorderSource):
        raise TypeError("application capacity requires its actual complete recorder source")
    require_application_recorder_source(source, source.env_ref_names)
    if source.chunk.reference.byte_length > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise ComposerContextUnsupported("retained_chunk_bytes")


def _require_source_environment(env_ref_names: frozenset[str]) -> None:
    if type(env_ref_names) is not frozenset or any(type(name) is not str for name in env_ref_names):
        raise TypeError("application recorder requires its closed environment-name set")


def _require_source_chunk_shape(chunk: PrivateChunk) -> None:
    if type(chunk) is not PrivateChunk or type(chunk.reference) is not PrivateChunkRef:
        raise AuditIntegrityError("application recorder lost its complete original source role")
    if (type(chunk.canonical), type(chunk.reference.codec), type(chunk.reference.digest), type(chunk.reference.byte_length)) != (
        str,
        str,
        str,
        int,
    ) or chunk.reference.codec != "composer.audit-buffer.v2":
        raise AuditIntegrityError("application recorder lost its complete original source role")


def _require_source_chunk(chunk: PrivateChunk) -> None:
    _require_source_chunk_shape(chunk)
    if not 0 <= chunk.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or (
        utf8_size(chunk.canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) != chunk.reference.byte_length
    ):
        raise AuditIntegrityError("application recorder exceeds its original complete document bound")


def capture_application_recorder_source(snapshot: RecorderSnapshot, *, env_ref_names: frozenset[str]) -> ApplicationRecorderSource:
    if type(snapshot) is not RecorderSnapshot:
        raise TypeError("application recorder capture requires its actual snapshot")
    return ApplicationRecorderSource(snapshot, env_ref_names=env_ref_names)


def admit_application_recorder_source(chunk: PrivateChunk, *, env_ref_names: frozenset[str]) -> ApplicationRecorderSource:
    if type(chunk) is not PrivateChunk:
        raise TypeError("application recorder admission requires its actual source chunk")
    return ApplicationRecorderSource(chunk, env_ref_names=env_ref_names)


def require_application_recorder_source(source: ApplicationRecorderSource, env_ref_names: frozenset[str]) -> ApplicationRecorderSource:
    _require_source_environment(env_ref_names)
    if type(source) not in (ApplicationRecorderSource, ProspectiveApplicationRecorderSource):
        raise TypeError("application graph requires its actual source-admitted recorder")
    _require_source_environment(source.env_ref_names)
    if source.env_ref_names != env_ref_names:
        raise AuditIntegrityError("application recorder changed its admitted environment")
    if type(source) is ProspectiveApplicationRecorderSource:
        _require_source_chunk_shape(source.chunk)
        chunk, _snapshot = _prospective_recorder_values(source.snapshot, env_ref_names)
        if chunk != source.chunk:
            raise AuditIntegrityError("prospective application recorder changed its complete original source")
        if chunk.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("prospective application recorder lost its actual complete document excess")
        return source
    # The normal constructor strictly decoded the original verified chunk and
    # owns a deeply immutable snapshot. Reusing that same source and credential
    # environment does not introduce new bytes or mutable input to admit.
    return source


def application_recorder_chunk(snapshot: RecorderSnapshot, *, recorder_source: ApplicationRecorderSource | None = None) -> PrivateChunk:
    """Validate the selected prospective recorder without admitting its bytes.

    Historical or unrelated snapshots retain the normal bounded encoder. Only
    the exact complete prospective source document can defer its capacity cut.
    """
    if type(recorder_source) is ApplicationRecorderSource:
        if snapshot is recorder_source.snapshot:
            return recorder_source.chunk
        # Observe the current graph's complete projection. Exact equality with
        # the already admitted bytes needs no second parse or credential scan;
        # ordinary value equality would conflate distinct JSON field types.
        if recorder_snapshot_json(snapshot) == recorder_source.chunk.canonical:
            return recorder_source.chunk
    if type(recorder_source) is ProspectiveApplicationRecorderSource:
        require_application_recorder_source(recorder_source, recorder_source.env_ref_names)
        if recorder_snapshot_json(snapshot) == recorder_source.chunk.canonical:
            return recorder_source.chunk
    return encode_recorder_snapshot(snapshot)


def application_recorder_slots(
    graph: ApplicationGraph, source: ApplicationRecorderSource, *, selected_recorder: _ValidatedSelectedRecorder | None = None
) -> dict[int, int]:
    chunk = application_recorder_chunk(graph.recorder, recorder_source=source) if selected_recorder is None else selected_recorder.chunk
    if selected_recorder is not None:
        selected_recorder.require_bound(graph, source)
    if chunk != source.chunk:
        raise AuditIntegrityError("application graph differs from its complete original recorder source")
    slots: dict[int, int] = {}
    for index, call in enumerate(graph.recorder.llm_calls):
        if type(call) is not ComposerLLMCall:
            raise TypeError("application recorder lost its actual LLM owner")
        slots.setdefault(id(call), index)
    return slots


def construct_application_llm(source: ApplicationRecorderSource, slot: int) -> ComposerLLMCall:
    """Fresh node identity with the admitted cell's deeply immutable values."""
    if type(slot) is not int or not 0 <= slot < len(source.snapshot.llm_calls):
        raise AuditIntegrityError("application LLM lost its exact recorder slot")
    actual = source.snapshot.llm_calls[slot]
    if type(actual) is not ComposerLLMCall:
        raise TypeError("application LLM requires its actual admitted recorder cell")
    # Recorder admission has already decoded every field and frozen the JSON
    # descendants. Copy the instance to retain per-node identity without
    # encoding and parsing the same unmodified values again.
    return copy(actual)


_ATOMS: dict[str, type[Any]] = {
    "application_planner": ComposerPlannerAttempt,
    "application_candidate": PrivatePlannerCandidate,
    "application_withheld": WithheldReply,
    "application_visible": VisibleToolEvent,
    "application_failure": PrivateFailureFacts,
    "application_cursors": RecorderDrainCursors,
}

# Compile only the closed nominal schemas. Every value dump, strict parse and
# canonical round trip below remains fresh for its current source and graph.
_LLM_ADAPTER = TypeAdapter(ComposerLLMCall)
_ATOM_ADAPTERS: dict[type[Any], TypeAdapter[Any]] = {
    ComposerLLMCall: _LLM_ADAPTER,
    ComposerToolInvocation: TypeAdapter(ComposerToolInvocation),
    **{owner: TypeAdapter(owner) for owner in _ATOMS.values()},
}


def _atom_json(value: Any, owner: type[Any]) -> str:
    if type(value) is VisibleToolEvent and VisibleToolEvent.from_envelope(value.to_envelope()) != value:
        raise AuditIntegrityError("application visible event changed its actual historical envelope")
    adapter = _ATOM_ADAPTERS[owner]
    encoded = canonical_json(adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json))
    checked = adapter.validate_json(encoded, strict=True, extra="forbid")
    if canonical_json(adapter.dump_python(checked, mode="json", warnings="error", fallback=owned_json)) != encoded:
        raise AuditIntegrityError("application audit atom changed its complete owned fields")
    return encoded


def application_members(
    value: Any,
    *,
    recorder_slots: dict[int, int] | None = None,
    recorder_source: ApplicationRecorderSource | None = None,
    selected_recorder: _ValidatedSelectedRecorder | None = None,
) -> tuple[str, dict[str, Any]] | None:
    if type(value) is ComposerLLMCall:
        if recorder_slots is None or id(value) not in recorder_slots:
            raise AuditIntegrityError("application LLM has no actual recorder identity slot")
        return "application_llm", {"recorder_slot": recorder_slots[id(value)]}
    if type(value) is ApplicationGraphCut:
        return "application_cut", {
            "data": value.data,
            "effects_before": value.effects_before,
            "effects_after": value.effects_after,
            "recorder_before": value.recorder_before,
            "recorder_after": value.recorder_after,
        }
    if type(value) is RestoredBatchData:
        return "application_batch", {"batch": value.batch, "accumulator": value.accumulator, "calls": value.calls}
    if type(value) is PersistedTurnEffects:
        return "application_effects", {
            "initial_state": value.initial_state,
            "initial_state_id": value.initial_state_id,
            "latest_verified_state": value.latest_verified_state,
            "latest_verified_state_id": value.latest_verified_state_id,
            "latest_committed_state_id": value.latest_committed_state_id,
            "verification": value.verification,
            "initial_authored_state_hash": value.initial_authored_state_hash,
            "latest_authored_state_hash": value.latest_authored_state_hash,
            "net_changed_fields": value.net_changed_fields,
            "batches": value.batches,
            "committed_revision_ids": value.committed_revision_ids,
        }
    if type(value) is PersistedBatchEffects:
        return "application_batch_effects", {
            "assistant_message_id": value.assistant_message_id,
            "state_id": value.state_id,
            "version": value.version,
            "tool_call_ids": value.tool_call_ids,
            "inserted_state_ids": value.inserted_state_ids,
            "authored_state_hash": value.authored_state_hash,
        }
    if type(value) is RecorderSnapshot:
        if selected_recorder is None or value is not selected_recorder.graph.recorder:
            application_recorder_chunk(value, recorder_source=recorder_source)
        else:
            selected_recorder.require_bound(selected_recorder.graph, recorder_source)
        return "application_recorder", {
            "invocations": value.invocations,
            "llm_calls": value.llm_calls,
            "planner_attempts": value.planner_attempts,
            "withheld_replies": value.withheld_replies,
            "visible_tool_events": value.visible_tool_events,
            "private_candidates": value.private_candidates,
            "private_authorings": value.private_authorings,
            "drained": value.drained,
        }
    if type(value) is RetainedVisibleLink:
        return "application_visible_link", {"invocation_index": value.invocation_index, "event": value.event}
    if type(value) is ComposerToolInvocation:
        # The scalar/timestamp/status part retains its exact existing nominal
        # codec; authoring/failure links are separate nodes, never equal-value
        # interned. Constructor admission and the full inner round trip occur.
        return "application_invocation", {
            "canonical": _atom_json(value, ComposerToolInvocation),
            "private_authoring": value.private_authoring,
            "private_failure": value.private_failure,
        }
    for kind, owner in _ATOMS.items():
        if type(value) is owner:
            return kind, {"canonical": _atom_json(value, owner)}
    return None


_FIELDS = {
    "application_cut": ("data", "effects_before", "effects_after", "recorder_before", "recorder_after"),
    "application_batch": ("batch", "accumulator", "calls"),
    "application_effects": (
        "initial_state",
        "initial_state_id",
        "latest_verified_state",
        "latest_verified_state_id",
        "latest_committed_state_id",
        "verification",
        "initial_authored_state_hash",
        "latest_authored_state_hash",
        "net_changed_fields",
        "batches",
        "committed_revision_ids",
    ),
    "application_batch_effects": (
        "assistant_message_id",
        "state_id",
        "version",
        "tool_call_ids",
        "inserted_state_ids",
        "authored_state_hash",
    ),
    "application_recorder": (
        "invocations",
        "llm_calls",
        "planner_attempts",
        "withheld_replies",
        "visible_tool_events",
        "private_candidates",
        "private_authorings",
        "drained",
    ),
    "application_visible_link": ("invocation_index", "event"),
    "application_invocation": ("canonical", "private_authoring", "private_failure"),
}


def construct_application_member(kind: str, members: dict[str, Any]) -> Any:
    expected = ("canonical",) if kind in _ATOMS else _FIELDS.get(kind)
    if expected is None or tuple(members) != expected:
        raise AuditIntegrityError("application graph changed its exact nominal field set/order")
    if kind in _ATOMS:
        owner = _ATOMS[kind]
        restored = _ATOM_ADAPTERS[owner].validate_json(members["canonical"], strict=True, extra="forbid")
        if _atom_json(restored, owner) != members["canonical"]:
            raise AuditIntegrityError("application atom changed its retained inner bytes")
        return restored
    if kind == "application_invocation":
        invocation = _ATOM_ADAPTERS[ComposerToolInvocation].validate_json(members["canonical"], strict=True, extra="forbid")
        if invocation.private_authoring != members["private_authoring"] or invocation.private_failure != members["private_failure"]:
            raise AuditIntegrityError("application invocation changed its private source links")
        linked = replace(invocation, private_authoring=members["private_authoring"], private_failure=members["private_failure"])
        if _atom_json(linked, ComposerToolInvocation) != members["canonical"]:
            raise AuditIntegrityError("application invocation changed its complete original bytes")
        return linked
    if kind == "application_cut":
        return ApplicationGraphCut(**members)
    if kind == "application_batch":
        return RestoredBatchData(**members)
    if kind == "application_effects":
        return PersistedTurnEffects(**members)
    if kind == "application_batch_effects":
        return PersistedBatchEffects(**members)
    if kind == "application_recorder":
        snapshot = RecorderSnapshot(**members)
        snapshot.validate_links_and_cursors()
        return snapshot
    if kind == "application_visible_link":
        return RetainedVisibleLink(**members)
    raise AuditIntegrityError("application graph has no actual constructor")


def application_result_tools(
    graph: ApplicationGraph,
    env_ref_names: frozenset[str],
    *,
    recorder_source: ApplicationRecorderSource | None = None,
    selected_recorder: _ValidatedSelectedRecorder | None = None,
) -> dict[int, str]:
    from elspeth.web.composer.continuation_batch_data import _joint
    from elspeth.web.composer.continuation_p4_batch import _joined

    if type(graph) is not ApplicationGraph or type(graph.cuts) is not tuple:
        raise TypeError("application graph requires its actual owned roots")
    if (graph.selected_audit is None) != (graph.selected_dialect is None):
        raise AuditIntegrityError("active evidence graph lost its complete audit/dialect pair")
    if graph.selected_audit is not None and (
        type(graph.selected_audit) is not PortableDispatchAudit or type(graph.selected_dialect) is not ToolContractDialect
    ):
        raise TypeError("active evidence graph requires its actual portable audit/dialect")
    result_tools: dict[int, str] = {}
    for index, data in enumerate((graph.selected, *(cut.data for cut in graph.cuts))):
        if index == 0 and type(data) is RestoredBatchData and data.batch.phase in ("call_active", "call_ready", "dispatch_ready"):
            prepared = None
            if graph.selected_audit is not None and graph.selected_dialect is not None:
                prepared = (graph.selected_audit, graph.selected_dialect)
            selected = _joint(data, env_ref_names, prepared)
            result_tools.update(selected)
            continue
        if index == 0 and graph.selected_audit is not None:
            raise AuditIntegrityError("P4 evidence graph cannot acquire an active call audit")
        if type(data) is not RestoredBatchData or data.batch.phase not in ("p4_pending", "complete"):
            raise AuditIntegrityError("application graph lost its actual pending/completed batch")
        batch = data.batch
        if batch.p4 is not None or batch.current_call is not None:
            raise AuditIntegrityError("application graph cannot retain a committed witness/current call")
        if batch.phase == "p4_pending" and (batch.p4_receipt_json is not None or batch.p4_verification_failed):
            raise AuditIntegrityError("pending application invented a receipt/verification")
        if batch.phase == "complete" and type(batch.p4_receipt_json) is not str:
            raise AuditIntegrityError("completed application lost its exact historical receipt")
        pending = replace(batch, phase="p4_pending", p4_receipt_json=None, p4_verification_failed=False)
        _joined(RestoredBatchData(pending, data.accumulator, data.calls), env_ref_names=env_ref_names)
        plain = replace(pending, phase="dispatch_ready", p4_input_json=None)
        selected = _joint(RestoredBatchData(plain, data.accumulator, data.calls), env_ref_names)
        for identity, name in selected.items():
            if identity in result_tools and result_tools[identity] != name:
                raise AuditIntegrityError("shared application result has conflicting selected contracts")
            result_tools[identity] = name
    if type(graph.effects) is not PersistedTurnEffects or type(graph.recorder) is not RecorderSnapshot:
        raise TypeError("application graph lost actual evidence/recorder owners")
    for effects in (graph.effects, *(item for cut in graph.cuts for item in (cut.effects_before, cut.effects_after))):
        _admit_effects(effects, env_ref_names)
    if selected_recorder is None:
        application_recorder_chunk(graph.recorder, recorder_source=recorder_source)
    else:
        selected_recorder.require_bound(graph, recorder_source)
    from elspeth.web.composer.continuation_tool_failure import require_retained_failure_links

    for data in (graph.selected, *(cut.data for cut in graph.cuts)):
        require_retained_failure_links(data, graph.recorder.invocations)
        failure = data.batch.retained_failure
        source = None if failure is None else failure.refusal_source
        if (
            source is not None
            and source.provider_call_id is not None
            and sum(call.call_id == source.provider_call_id for call in graph.recorder.llm_calls) != 1
        ):
            raise AuditIntegrityError("retained policy refusal lost its actual recorder cost correlation")
    for cut in graph.cuts:
        if type(cut) is not ApplicationGraphCut:
            raise TypeError("application graph contains another cut owner")
        if any(type(effects) is not PersistedTurnEffects for effects in (cut.effects_before, cut.effects_after)):
            raise TypeError("application cut lost its actual effects")
        for snapshot in (cut.recorder_before, cut.recorder_after):
            if type(snapshot) is not RecorderSnapshot:
                raise TypeError("application cut lost its actual recorder")
            application_recorder_chunk(snapshot, recorder_source=recorder_source)
    return result_tools


def _application_graph_projection(
    graph: ApplicationGraph, *, env_ref_names: frozenset[str], recorder_source: ApplicationRecorderSource
) -> str:
    """Project current mutable values and aliases for an original-source comparison.

    No new wire is published here. The original source already passed the
    decoder; rebuilding it again would test our constructors rather than the
    current graph's values or alias partition.
    """
    from elspeth.web.composer.continuation_batch_data import _encode

    checked_source = _ValidatedRecorderSource.capture(recorder_source, env_ref_names)
    encoded = _encode(graph.selected, env_ref_names, application=graph, recorder_source=recorder_source, checked_source=checked_source)
    if application_recorder_chunk(graph.recorder, recorder_source=recorder_source) != recorder_source.chunk:
        raise AuditIntegrityError("application graph changed its complete selected recorder")
    require_application_recorder_source(recorder_source, env_ref_names)
    return encoded


def encode_application_graph(graph: ApplicationGraph, *, env_ref_names: frozenset[str], recorder_source: ApplicationRecorderSource) -> str:
    from elspeth.web.composer.continuation_batch_data import _decode_graph_values, _encode, _encode_values

    checked_source = _ValidatedRecorderSource.capture(recorder_source, env_ref_names)
    decoded_selected: list[_ValidatedSelectedRecorder] = []
    encoded = _encode(graph.selected, env_ref_names, application=graph, recorder_source=recorder_source, checked_source=checked_source)
    restored, result_tools = _decode_graph_values(
        encoded,
        env_ref_names,
        application=True,
        recorder_source=recorder_source,
        checked_source=checked_source,
        selected_recorder_out=decoded_selected,
    )
    if (
        type(restored) is not ApplicationGraph
        or len(decoded_selected) != 1
        or _encode_values(
            restored.selected,
            env_ref_names,
            result_tools,
            application=restored,
            recorder_source=recorder_source,
            checked_source=checked_source,
            selected_recorder=decoded_selected[0],
        )
        != encoded
    ):
        raise AuditIntegrityError("application constructors changed the actual alias partition")
    # Both originals can contain nested mutable values. Reprove them after
    # every constructor and alias check, before returning this wire source.
    if application_recorder_chunk(graph.recorder, recorder_source=recorder_source) != recorder_source.chunk:
        raise AuditIntegrityError("application graph changed its complete selected recorder")
    if application_recorder_chunk(restored.recorder, recorder_source=recorder_source) != recorder_source.chunk:
        raise AuditIntegrityError("application constructors changed their complete selected recorder")
    require_application_recorder_source(recorder_source, env_ref_names)
    return encoded


def decode_application_graph(
    encoded: str, *, env_ref_names: frozenset[str], recorder_source: ApplicationRecorderSource
) -> ApplicationGraph:
    from elspeth.web.composer.continuation_batch_data import _decode_graph_values, _encode_values

    checked_source = _ValidatedRecorderSource.capture(recorder_source, env_ref_names)
    decoded_selected: list[_ValidatedSelectedRecorder] = []
    restored, result_tools = _decode_graph_values(
        encoded,
        env_ref_names,
        application=True,
        recorder_source=recorder_source,
        checked_source=checked_source,
        selected_recorder_out=decoded_selected,
    )
    if (
        type(restored) is not ApplicationGraph
        or len(decoded_selected) != 1
        or _encode_values(
            restored.selected,
            env_ref_names,
            result_tools,
            application=restored,
            recorder_source=recorder_source,
            checked_source=checked_source,
            selected_recorder=decoded_selected[0],
        )
        != encoded
    ):
        raise AuditIntegrityError("application graph lost complete values or aliases")
    if application_recorder_chunk(restored.recorder, recorder_source=recorder_source) != recorder_source.chunk:
        raise AuditIntegrityError("application graph changed its complete selected recorder")
    require_application_recorder_source(recorder_source, env_ref_names)
    return restored


def _admit_effects(value: PersistedTurnEffects, env_ref_names: frozenset[str]) -> None:
    if type(value) is not PersistedTurnEffects or any(
        type(state) is not CompositionState for state in (value.initial_state, value.latest_verified_state)
    ):
        raise TypeError("application effects require complete actual state owners")
    if (
        value.verification not in ("verified", "unavailable", "not_persisted")
        or value.initial_authored_state_hash != authored_state_hash(value.initial_state)
        or value.latest_authored_state_hash != authored_state_hash(value.latest_verified_state)
        or any(
            identity is not None and type(identity) is not str
            for identity in (value.initial_state_id, value.latest_verified_state_id, value.latest_committed_state_id)
        )
        or type(value.batches) is not tuple
        or type(value.committed_revision_ids) is not tuple
        or len(set(value.committed_revision_ids)) != len(value.committed_revision_ids)
    ):
        raise AuditIntegrityError("application effects changed their complete state/frontier facts")
    metadata: list[Any] = [
        value.initial_state_id,
        value.latest_verified_state_id,
        value.latest_committed_state_id,
        value.net_changed_fields,
        value.committed_revision_ids,
    ]
    for batch in value.batches:
        if (
            type(batch) is not PersistedBatchEffects
            or type(batch.version) is not int
            or batch.version < 1
            or not batch.inserted_state_ids
            or batch.inserted_state_ids[-1] != batch.state_id
            or len(set(batch.inserted_state_ids)) != len(batch.inserted_state_ids)
        ):
            raise AuditIntegrityError("application batch effects lost their actual inserted cohort")
        metadata.extend(
            (batch.assistant_message_id, batch.state_id, batch.tool_call_ids, batch.inserted_state_ids, batch.authored_state_hash)
        )
    require_no_credential_material(owned_json(metadata), surface="portable application effects", env_ref_names=env_ref_names)
