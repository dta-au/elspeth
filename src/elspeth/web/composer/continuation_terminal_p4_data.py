"""Closed, non-resumable terminal P4 DATA; never execution or commit authority.

These documents bind the last valid issued source to safe publication input and
an actual writer receipt. They cannot encode a failed batch or restore an
exception. SQL ownership, source selection and committed readback remain with
the existing P4 owner. Normal continuation codecs do not accept these tags.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import Field, ValidationError

from elspeth.contracts.bounded_serialization import bounded_canonical_json_size, utf8_size
from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ContinuationValue, EffectFrontierEntry, LogicalRequestBinding, PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, RecorderSnapshot, decode_recorder_snapshot
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response, verify_private_chunk
from elspeth.web.composer.continuation_evidence_application_values import P4EffectProducerData
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_phase_transitions import ToolIssueSource
from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value, encode_prepared_p4, p4_semantic_input, prepared_p4_members
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt


class TerminalP4Reference(ContinuationValue):
    codec: Literal["composer.terminal-p4-input.v1", "composer.terminal-p4-result.v1"]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0, le=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)


@dataclass(frozen=True, slots=True)
class TerminalP4Document:
    reference: TerminalP4Reference
    canonical: str


class TerminalP4Input(ContinuationValue):
    schema_version: Literal["composer.terminal-p4-input.v1"] = "composer.terminal-p4-input.v1"
    logical: LogicalRequestBinding
    actor_user_id: str = Field(min_length=1)
    claim_attempt: int = Field(ge=1)
    producer: P4EffectProducerData
    checkpoint_id: str = Field(min_length=1)
    cursor: int = Field(ge=1)
    issue: ToolIssueSource
    issued_effect_key: str
    prior_prefix: PrivateChunkRef
    source_evidence: PrivateChunkRef
    response: PrivateChunk
    recorder: PrivateChunk
    effect_frontier: tuple[EffectFrontierEntry, ...]
    prepared: PreparedP4Value
    semantic_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TerminalP4Result(ContinuationValue):
    schema_version: Literal["composer.terminal-p4-result.v1"] = "composer.terminal-p4-result.v1"
    input: TerminalP4Reference
    receipt: PersistedComposerTurnReceipt
    drained: RecorderDrainCursors


def terminal_p4_input_key(effect_key: str) -> str:
    """One immutable operation-local input slot; not a commit or source proof."""
    if type(effect_key) is not str or len(effect_key) != 73 or not effect_key.startswith("batch:") or not effect_key.endswith(":p4"):
        raise AuditIntegrityError("terminal P4 input key requires its exact batch effect")
    digest = effect_key[6:-3]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise AuditIntegrityError("terminal P4 input key lost its response digest")
    return "terminal-p4-input:" + sha256(effect_key.encode("utf-8")).hexdigest()


def _bounded_text(canonical: str) -> None:
    if type(canonical) is not str or utf8_size(canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) > (
        CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    ):
        raise AuditIntegrityError("terminal P4 document exceeds its complete byte bound")


def _require_nested_model(
    value: LogicalRequestBinding | ToolIssueSource | PrivateChunkRef | EffectFrontierEntry | TerminalP4Reference,
    expected: type[LogicalRequestBinding | ToolIssueSource | PrivateChunkRef | EffectFrontierEntry | TerminalP4Reference],
) -> None:
    """Refuse undeclared state before a known nested model is projected.

    Calls below enumerate the terminal envelopes' actual owned graph. This is
    not recursive inspection of arbitrary objects or serialized string DATA.
    """
    if (
        type(value) is not expected
        or set(value.__dict__) != set(expected.model_fields)
        or value.__pydantic_extra__
        or value.__pydantic_private__
    ):
        raise AuditIntegrityError("terminal P4 nested owner changed its nominal type or declared fields")


def _require_nested_input_owners(value: TerminalP4Input) -> None:
    _require_nested_model(value.logical, LogicalRequestBinding)
    _require_nested_model(value.issue, ToolIssueSource)
    _require_nested_model(value.issue.response, PrivateChunkRef)
    _require_nested_model(value.issue.prepared, PrivateChunkRef)
    _require_nested_model(value.prior_prefix, PrivateChunkRef)
    _require_nested_model(value.source_evidence, PrivateChunkRef)
    # Exact slotted dataclasses cannot carry an unprojected subclass field.
    if (
        type(value.producer) is not P4EffectProducerData
        or type(value.response) is not PrivateChunk
        or type(value.recorder) is not PrivateChunk
    ):
        raise AuditIntegrityError("terminal P4 nested owner changed its nominal dataclass")
    _require_nested_model(value.response.reference, PrivateChunkRef)
    _require_nested_model(value.recorder.reference, PrivateChunkRef)
    if type(value.effect_frontier) is not tuple:
        raise AuditIntegrityError("terminal P4 nested owner changed its immutable frontier cohort")
    for effect in value.effect_frontier:
        _require_nested_model(effect, EffectFrontierEntry)


def _verify_document(document: TerminalP4Document, *, codec: str) -> str:
    if type(document) is not TerminalP4Document or type(document.reference) is not TerminalP4Reference:
        raise AuditIntegrityError("terminal P4 requires its closed document owner")
    _require_nested_model(document.reference, TerminalP4Reference)
    _bounded_text(document.canonical)
    try:
        reference = TerminalP4Reference.model_validate_json(canonical_json(document.reference.model_dump(mode="json", warnings="error")))
    except ValidationError as error:
        raise AuditIntegrityError("terminal P4 reference violates its closed contract") from error
    encoded = document.canonical.encode("utf-8")
    if reference.codec != codec or reference.byte_length != len(encoded) or reference.digest != sha256(encoded).hexdigest():
        raise AuditIntegrityError("terminal P4 document changed its mode or exact bytes")
    value, parse_error = parse_json_strict(document.canonical)
    if parse_error is not None or canonical_json(value) != document.canonical:
        raise AuditIntegrityError("terminal P4 document must be canonical strict JSON")
    return document.canonical


def _document(value: TerminalP4Input | TerminalP4Result, *, env_ref_names: frozenset[str]) -> TerminalP4Document:
    if type(value) not in (TerminalP4Input, TerminalP4Result):
        raise AuditIntegrityError("terminal P4 encoding requires its closed DATA owner")
    expected_fields = TerminalP4Input.model_fields if type(value) is TerminalP4Input else TerminalP4Result.model_fields
    if set(value.__dict__) != set(expected_fields) or value.__pydantic_extra__:
        raise AuditIntegrityError("terminal P4 DATA owner has extra or missing fields")
    if type(value) is TerminalP4Input and (
        type(value.prepared) is not PreparedP4Value
        or set(value.prepared.__dict__) != set(PreparedP4Value.model_fields)
        or value.prepared.__pydantic_extra__
    ):
        raise AuditIntegrityError("terminal P4 prepared owner has extra or missing fields")
    if type(value) is TerminalP4Result and (
        type(value.receipt) is not PersistedComposerTurnReceipt
        or set(value.receipt.__dict__) != set(PersistedComposerTurnReceipt.model_fields)
        or value.receipt.__pydantic_extra__
    ):
        raise AuditIntegrityError("terminal P4 receipt owner has extra or missing fields")
    if type(value) is TerminalP4Input:
        _require_nested_input_owners(value)
    elif type(value) is TerminalP4Result:
        _require_nested_model(value.input, TerminalP4Reference)
        if type(value.drained) is not RecorderDrainCursors:
            raise AuditIntegrityError("terminal P4 nested owner changed its nominal drain cursors")
    else:
        raise AuditIntegrityError("terminal P4 encoding requires its closed DATA owner")
    payload = value.model_dump(mode="json", warnings="error")
    measured = bounded_canonical_json_size(payload, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    if measured.admitted_bytes is None:
        raise AuditIntegrityError("terminal P4 document exceeds its complete byte bound")
    canonical = canonical_json(payload)
    _bounded_text(canonical)
    require_no_credential_material(
        canonical,
        surface="terminal P4 private DATA",
        env_ref_names=env_ref_names,
        max_string_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    encoded = canonical.encode("utf-8")
    if len(encoded) != measured.admitted_bytes:
        raise AuditIntegrityError("terminal P4 encoding changed its measured complete bytes")
    return TerminalP4Document(
        TerminalP4Reference(codec=value.schema_version, digest=sha256(encoded).hexdigest(), byte_length=len(encoded)), canonical
    )


def _recorder(value: TerminalP4Input, *, env_ref_names: frozenset[str]) -> RecorderSnapshot:
    """Check the actual pending slices, retaining every other recorder channel."""
    _bounded_text(value.recorder.canonical)
    recorder = decode_recorder_snapshot(value.recorder)
    members = prepared_p4_members(value.prepared, env_ref_names=env_ref_names)
    cursor = recorder.drained
    if (
        recorder.invocations[cursor.invocations : cursor.invocations + len(members.invocations)] != members.invocations
        or recorder.private_authorings[cursor.private_authorings : cursor.private_authorings + len(members.authorings)]
        != members.authorings
        or recorder.withheld_replies[cursor.withheld_replies : cursor.withheld_replies + len(members.withheld_replies)]
        != members.withheld_replies
    ):
        raise AuditIntegrityError("terminal P4 changed its exact undrained recorder slices")
    expected = tuple(
        (cursor.invocations + index, row.visible_event) for index, row in enumerate(members.redacted_rows) if row.visible_event is not None
    )
    links = recorder.visible_tool_events[cursor.visible_tool_events : cursor.visible_tool_events + len(expected)]
    if tuple((link.invocation_index, link.event) for link in links) != expected:
        raise AuditIntegrityError("terminal P4 changed its exact visible-event aliases")
    return recorder


def _require_input(value: TerminalP4Input, *, env_ref_names: frozenset[str]) -> None:
    issue, producer, prepared = value.issue, value.producer, value.prepared
    if (
        value.logical.session_id != prepared.session_id
        or value.cursor != issue.cursor
        or value.checkpoint_id == issue.previous_checkpoint_id
        or (producer.creator_sequence, producer.creator_generation) != (issue.sequence, issue.generation)
        or (producer.creator_operation_id, producer.creator_operation_epoch) != (issue.creator_operation_id, issue.creator_operation_epoch)
        or (prepared.creator_operation_id, prepared.creator_operation_epoch) != (issue.creator_operation_id, issue.creator_operation_epoch)
        or value.issued_effect_key != f"tool:{issue.response.digest}:{issue.tool_index}"
        or issue.response != value.response.reference
        or issue.response.codec != "composer.response.v1"
        or issue.prepared.codec != "composer.tool-batch.v1"
        or value.prior_prefix.codec != "composer.coupled-batch-data.v1"
        or value.source_evidence.codec != "composer.evidence.v2"
        or not prepared.crash_pending
    ):
        raise AuditIntegrityError("terminal P4 changed its original source/creator roles")
    for reference in (issue.prepared, value.prior_prefix, value.source_evidence):
        if reference.byte_length > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("terminal P4 source exceeds its complete byte bound")
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    if value.semantic_input_sha256 != sha256(p4_semantic_input(prepared, env_ref_names=env_ref_names).encode("utf-8")).hexdigest():
        raise AuditIntegrityError("terminal P4 changed its complete semantic input hash")
    if members.mutation_ceiling is None or (
        members.mutation_ceiling.session_id != value.logical.session_id
        or members.mutation_ceiling.request_id != value.logical.original_user_message_id
    ):
        raise AuditIntegrityError("terminal P4 lost its original logical mutation ceiling")
    _bounded_text(value.response.canonical)
    verify_private_chunk(value.response.reference, value.response.canonical)
    response = decode_continuation_response(value.response)
    calls = response.tool_batch.calls
    if not 0 <= issue.tool_index < len(calls) or len(members.invocations) != issue.tool_index + 1:
        raise AuditIntegrityError("terminal P4 changed its actual executed prefix length")
    if calls[issue.tool_index].id != issue.tool_call_id:
        raise AuditIntegrityError("terminal P4 changed its actual issued call")
    for call, invocation, authoring in zip(calls[: issue.tool_index + 1], members.invocations, members.authorings, strict=True):
        if (
            call.id != invocation.tool_call_id
            or call.function.name != invocation.tool_name
            or call.function.arguments != authoring.raw_arguments
        ):
            raise AuditIntegrityError("terminal P4 changed its paid response/call prefix")
    last = members.invocations[-1]
    if issue.input_sha256 != stable_hash(
        {"tool_call_id": last.tool_call_id, "name": last.tool_name, "arguments": members.authorings[-1].arguments_canonical}
    ):
        raise AuditIntegrityError("terminal P4 changed its actual issued semantic input")
    controlled = canonical_json({"success": False, "status": "SERVICE_FAILURE", "reason": "service_failure"})
    if (
        last.status is not ComposerToolStatus.SUCCESS
        or last.result_canonical != controlled
        or members.redacted_rows[-1].content != controlled
        or members.revoked_rows[-1].content != controlled
        or last.error_class is not None
        or last.error_message is not None
        or last.error_category is not None
        or last.private_failure is not None
        or members.private_tool_failures
    ):
        raise AuditIntegrityError("terminal P4 requires its closed value-free service completion")
    if any(
        item.error_class is not None or item.error_message is not None or item.private_failure is not None for item in members.invocations
    ):
        raise AuditIntegrityError("terminal P4 input cannot retain a native failure projection")
    keys = tuple(item.effect_id for item in value.effect_frontier)
    if len(set(keys)) != len(keys) or f"batch:{issue.response.digest}:p4" in keys:
        raise AuditIntegrityError("terminal P4 changed its original distinct effect frontier")
    issued = tuple(item for item in value.effect_frontier if item.effect_id == value.issued_effect_key)
    if len(issued) != 1 or issued[0].disposition != "dispatched" or issued[0].receipt_id is not None:
        raise AuditIntegrityError("terminal P4 lost its actual issued-effect frontier")
    _recorder(value, env_ref_names=env_ref_names)


def encode_terminal_p4_input(value: TerminalP4Input, *, env_ref_names: frozenset[str] = frozenset()) -> TerminalP4Document:
    document = _document(value, env_ref_names=env_ref_names)
    decode_terminal_p4_input(document, env_ref_names=env_ref_names)
    return document


def decode_terminal_p4_input(document: TerminalP4Document, *, env_ref_names: frozenset[str] = frozenset()) -> TerminalP4Input:
    canonical = _verify_document(document, codec="composer.terminal-p4-input.v1")
    try:
        value = TerminalP4Input.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("terminal P4 input violates its closed contract") from error
    if _document(value, env_ref_names=env_ref_names) != document:
        raise AuditIntegrityError("terminal P4 input omitted or normalized a field")
    _require_input(value, env_ref_names=env_ref_names)
    return value


def encode_terminal_p4_result(
    value: TerminalP4Result, *, input_document: TerminalP4Document, env_ref_names: frozenset[str] = frozenset()
) -> TerminalP4Document:
    document = _document(value, env_ref_names=env_ref_names)
    decode_terminal_p4_result(document, input_document=input_document, env_ref_names=env_ref_names)
    return document


def decode_terminal_p4_result(
    document: TerminalP4Document, *, input_document: TerminalP4Document, env_ref_names: frozenset[str] = frozenset()
) -> TerminalP4Result:
    # These existing functions validate receipt DATA and exact recorder
    # projection. Neither their use here nor decoding a result proves commit.
    from elspeth.web.composer.continuation_p4_application import _project_recorder
    from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, decode_p4_receipt

    canonical = _verify_document(document, codec="composer.terminal-p4-result.v1")
    original = decode_terminal_p4_input(input_document, env_ref_names=env_ref_names)
    try:
        value = TerminalP4Result.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("terminal P4 result violates its closed contract") from error
    if _document(value, env_ref_names=env_ref_names) != document:
        raise AuditIntegrityError("terminal P4 result omitted or normalized a field")
    if value.input != input_document.reference:
        raise AuditIntegrityError("terminal P4 result changed its complete input manifest")
    receipt, outcome = decode_p4_receipt(
        canonical_json(value.receipt.model_dump(mode="json", warnings="error")), env_ref_names=env_ref_names
    )
    if (
        receipt.effect_key != f"batch:{original.issue.response.digest}:p4"
        or receipt.input_sha256 != original.semantic_input_sha256
        or receipt.prepared_p4_json != encode_prepared_p4(original.prepared, env_ref_names=env_ref_names)
    ):
        raise AuditIntegrityError("terminal P4 result changed its actual prepared input/effect")
    recorder = _recorder(original, env_ref_names=env_ref_names)
    projected = _project_recorder(recorder, original.prepared, ReadmittedP4Result(receipt, outcome, None), env_ref_names=env_ref_names)
    if value.drained != projected.drained:
        raise AuditIntegrityError("terminal P4 result changed its exact published recorder slices")
    return value
