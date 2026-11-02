"""Original preview observation DATA, without dispatch or settlement authority.

No function here invokes a producer, reads SQL/resources or enables nonempty
preview. Live owners still need actual current SQL/source admission. Cold DATA
does not restore an issued observation, cache grant or execution permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from weakref import WeakKeyDictionary

from pydantic import BaseModel, ConfigDict, Field

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_snapshot, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, ContinuationDocumentTooLarge
from elspeth.web.composer.continuation_tool_batch import ToolResultValue, decode_tool_result_value, encode_tool_result_value
from elspeth.web.composer.continuation_value_batch_computation import _require_original_call
from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatchCall
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.execution.schemas import ValidationResult

_CODEC = "composer.preview-observation.v1"


class PreviewSourceData(BaseModel):
    """A complete prepared source locator; nominal DATA grants no authority."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.preview-source.v1"] = "composer.preview-source.v1"
    session_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    cursor: int = Field(ge=0)
    sequence: int = Field(ge=1)
    generation: int = Field(ge=1)
    claim_attempt: int = Field(ge=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(ge=1)
    lease_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_batch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: PrivateChunkRef
    tool_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)
    raw_arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings_hash: str = Field(min_length=1)
    session_scope: str = Field(min_length=1)
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreviewObservationData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.preview-observation.v1"] = "composer.preview-observation.v1"
    source: PreviewSourceData
    # This already carries the complete strict runtime result exactly once.
    result: ToolResultValue
    tolerant_json: str | None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class OriginalPreviewObservation:
    """An identity observation, never an SDK/current SQL/dispatch receipt."""

    source: PreviewSourceData
    result: ToolResult
    strict: ValidationResult | None
    tolerant: ValidationResult | None
    data: PreviewObservationData


_ORIGINALS: WeakKeyDictionary[OriginalPreviewObservation, str] = WeakKeyDictionary()


def preview_source_data(prepared: PreparedValueBatchCall) -> PreviewSourceData:
    """Bind the actual prepared in-memory cohort; perform no SQL reproof."""
    data = _require_original_call(prepared)
    runtime, call, context = prepared.runtime, prepared.call, prepared.context
    selected = runtime.source.selected
    root, inventory = selected.stack, runtime.materialization.inventory
    running = inventory.running
    top = root.frames[-1]
    if (
        type(top) is not ToolBatchFrame
        or top.phase != "tool_ready"
        or top.next_tool_index != call.index
        or call.phase != "prepared"
        or call.tool_name != "preview_pipeline"
        or call.tool_call.function.name != "preview_pipeline"
        or type(call.arguments) is not dict
        or call.arguments
        or data.batch.state is not call.pre_state
        or context.session_operation_context is not running.session_operation_context
        or runtime.bindings.session_operation_context is not running.session_operation_context
        or runtime.bindings.required_work is None
        or runtime.bindings.required_work.running is not running
        or context.policy_catalog.snapshot is not context.plugin_snapshot
    ):
        raise AuditIntegrityError("preview source changed its original prepared cohort")
    key = context.preflight.key(call.pre_state, session_scope=context.session_scope, plugin_snapshot=context.plugin_snapshot)
    return PreviewSourceData(
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        checkpoint_id=selected.checkpoint_id,
        cursor=root.cursor,
        sequence=running.claim.segment_sequence,
        generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        creator_operation_id=running.session_operation_context.fence.operation_id,
        creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
        lease_sha256=sha256(running.session_operation_context.fence.lease_token.encode("utf-8")).hexdigest(),
        policy_sha256=root.policy_hash,
        snapshot_sha256=sha256(encode_continuation_snapshot(root).encode("utf-8")).hexdigest(),
        prepared_batch_sha256=sha256(prepared.prepared_batch_json.encode("utf-8")).hexdigest(),
        response=top.response,
        tool_index=call.index,
        tool_call_id=call.tool_call.id,
        raw_arguments_sha256=sha256(call.tool_call.function.arguments.encode("utf-8")).hexdigest(),
        state_sha256=sha256(canonical_json(call.pre_state.to_dict()).encode("utf-8")).hexdigest(),
        settings_hash=key.settings_hash,
        session_scope=key.session_scope,
        environment_sha256=stable_hash(sorted(runtime.env_ref_names)),
    )


def require_preview_source_matches(source: PreviewSourceData, prepared: PreparedValueBatchCall) -> None:
    if type(source) is not PreviewSourceData or source != preview_source_data(prepared):
        raise AuditIntegrityError("preview source differs from its original prepared cohort")


def _data(
    source: PreviewSourceData,
    result: ToolResult,
    strict: ValidationResult | None,
    tolerant: ValidationResult | None,
    *,
    names: frozenset[str],
) -> PreviewObservationData:
    if (
        type(result) is not ToolResult
        or (strict is not None and type(strict) is not ValidationResult)
        or (strict is None and result.success)
        or result.runtime_preflight is not strict
    ):
        raise AuditIntegrityError("preview observation lost its original result/verdict identity")
    if (tolerant is not None and type(tolerant) is not ValidationResult) or (
        strict is not None and is_pending_interpretation_handoff(strict)
    ) != (tolerant is not None):
        raise AuditIntegrityError("preview observation changed its original strict/tolerant branch")
    value = PreviewObservationData(
        source=source,
        result=encode_tool_result_value("preview_pipeline", result, env_ref_names=names),
        tolerant_json=None if tolerant is None else canonical_json(tolerant.model_dump(mode="json")),
    )
    _validate_data(value, names=names)
    return value


def observe_original_preview_result(
    prepared: PreparedValueBatchCall,
    *,
    source: PreviewSourceData,
    result: ToolResult,
    strict: ValidationResult | None,
    tolerant: ValidationResult | None = None,
) -> OriginalPreviewObservation:
    """Observe an already-returned original; never invoke/authorize its producer."""
    require_preview_source_matches(source, prepared)
    if result.updated_state is not prepared.call.pre_state:
        raise AuditIntegrityError("preview observation replaced its original read-only state")
    value = _data(source, result, strict, tolerant, names=prepared.runtime.env_ref_names)
    observed = OriginalPreviewObservation(source, result, strict, tolerant, value)
    _ORIGINALS[observed] = canonical_json(value.model_dump(mode="json"))
    return observed


def require_original_preview_observation(observed: OriginalPreviewObservation, prepared: PreparedValueBatchCall) -> None:
    if type(observed) is not OriginalPreviewObservation or observed not in _ORIGINALS:
        raise AuditIntegrityError("preview observation lacks its original issued identity")
    require_preview_source_matches(observed.source, prepared)
    original = _ORIGINALS[observed]
    value = _data(observed.source, observed.result, observed.strict, observed.tolerant, names=prepared.runtime.env_ref_names)
    if canonical_json(observed.data.model_dump(mode="json")) != original or canonical_json(value.model_dump(mode="json")) != original:
        raise AuditIntegrityError("preview observation changed its original complete returned DATA")


def _validate_data(value: PreviewObservationData, *, names: frozenset[str]) -> None:
    if type(value) is not PreviewObservationData or value.result.tool_name != "preview_pipeline":
        raise AuditIntegrityError("preview DATA lost its closed source/result schema")
    result = decode_tool_result_value(value.result, env_ref_names=names)
    strict = result.runtime_preflight
    if (
        (strict is not None and type(strict) is not ValidationResult)
        or (strict is None and result.success)
        or sha256(value.result.state_json.encode("utf-8")).hexdigest() != value.source.state_sha256
        or (strict is not None and is_pending_interpretation_handoff(strict)) != (value.tolerant_json is not None)
    ):
        raise AuditIntegrityError("preview DATA changed its complete state/verdict branch")
    if value.tolerant_json is not None:
        tolerant = ValidationResult.model_validate_json(value.tolerant_json, strict=True)
        if canonical_json(tolerant.model_dump(mode="json")) != value.tolerant_json:
            raise AuditIntegrityError("preview DATA changed its complete tolerant verdict")


def measure_preview_data(
    value: PreviewObservationData, *, limit: int = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
) -> BoundedCanonicalSize:
    """Pure encoded-byte observation; no semantic/credential/P4/custody grant.

    Excess has admitted_bytes=None, never an invented exact excess size.
    This measures one complete DATA document, not aggregate or whole-P4 fit.
    """
    if type(value) is not PreviewObservationData:
        raise TypeError("preview measurement requires its nominal complete DATA")
    return bounded_canonical_json_size(value.model_dump(mode="json"), limit=limit)


def encode_preview_data(value: PreviewObservationData, *, env_ref_names: frozenset[str] = frozenset()) -> PrivateChunk:
    """Encode DATA only; no live observation, dispatch or terminal permission."""
    _validate_data(value, names=env_ref_names)
    if measure_preview_data(value).exceeded:
        raise ContinuationDocumentTooLarge("complete preview observation exceeds its unchanged private document bound")
    return encode_private_chunk(codec=_CODEC, canonical=canonical_json(value.model_dump(mode="json")), env_ref_names=env_ref_names)


def decode_preview_data(
    chunk: PrivateChunk, *, expected_source: PreviewSourceData, env_ref_names: frozenset[str] = frozenset()
) -> PreviewObservationData:
    """Strict DATA replay; do not recreate a live issued observation or cache."""
    if type(chunk) is not PrivateChunk or chunk.reference.codec != _CODEC or type(expected_source) is not PreviewSourceData:
        raise AuditIntegrityError("preview DATA lost its original codec/source")
    verify_private_chunk(chunk.reference, chunk.canonical)
    value = PreviewObservationData.model_validate_json(chunk.canonical, strict=True)
    if value.source != expected_source or encode_preview_data(value, env_ref_names=env_ref_names) != chunk:
        raise AuditIntegrityError("preview DATA changed its original complete source or bytes")
    return value
