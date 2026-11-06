"""Original preview return projection and source-bound unpaid terminal DATA.

Projection alone grants no dispatch, SQL or terminal authority. The issuing
owner must reserve and prove the actual tool source before resource work.
Only a validated original return plus measured byte excess selects incomplete
DATA. Other schema/credential/codec failures retain their original meaning.
"""

from __future__ import annotations

from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Literal
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import Connection, insert, select, update
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size, utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ContinuationFrameStack, EffectFrontierEntry, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_preview_observation import (
    OriginalPreviewObservation,
    PreviewObservationData,
    PreviewSourceData,
    decode_preview_data,
    encode_preview_data,
    measure_preview_data,
    observe_original_preview_result,
    require_original_preview_observation,
    require_preview_source_matches,
)
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_value, prepare_tool_result_value
from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatchCall
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_FAILURE_CODES,
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    ComposerOperationError,
    ComposerOperationRunning,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
    settle_continuation_on_connection,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composer_validation_terminal_holds_table as validation_holds

type PreviewByteSurface = Literal["strict_verdict", "tolerant_verdict", "tool_feedback", "private_result"]


class ValidationIncompleteReceipt(BaseModel):
    """Fixed source facts only; missing diagnostic/output text is never used."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.preview-incomplete.v1"] = "composer.preview-incomplete.v1"
    source: PreviewSourceData
    outcome: Literal["validation_incomplete"] = "validation_incomplete"
    reason: Literal["oversized_original_preview_result"] = "oversized_original_preview_result"
    surface: PreviewByteSurface
    limit: int = Field(
        default=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
        ge=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
        le=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    admitted_bytes: None = None
    additional_provider_calls: Literal[0] = 0
    additional_provider_cost_usd: Literal["0"] = "0"
    missing_result_used: Literal[False] = False
    automatic_retry_allowed: Literal[False] = False
    returned_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    returned_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$")


class PreviewTailIncompleteReceipt(BaseModel):
    """A complete raw return whose mandatory consequence cannot be retained."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.preview-incomplete.v2"] = "composer.preview-incomplete.v2"
    source: PreviewSourceData
    outcome: Literal["validation_incomplete"] = "validation_incomplete"
    reason: Literal["complete_preview_tail_exceeds_limit"] = "complete_preview_tail_exceeds_limit"
    surface: Literal["private_chunk", "evidence_pair", "retained_history"]
    limit: int = Field(gt=0)
    # A saturated lower bound is sufficient to prove excess and has a bound
    # derived from the admitted limit before the original producer starts.
    observed_at_least: int = Field(gt=0)
    measurement: Literal["lower_bound"] = "lower_bound"
    original_result: PrivateChunkRef | None = None
    additional_provider_calls: Literal[0] = 0
    additional_provider_cost_usd: Literal["0"] = "0"
    missing_result_used: Literal[False] = False
    automatic_retry_allowed: Literal[False] = False
    returned_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    returned_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$")

    @model_validator(mode="after")
    def require_tail_measurement(self) -> PreviewTailIncompleteReceipt:
        if self.observed_at_least != self.limit + 1:
            raise ValueError("preview tail receipt requires its explicit saturated lower bound")
        if self.surface != "retained_history" and self.limit != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise ValueError("preview mandatory tail receipt changed its original document limit")
        if self.original_result is not None and (
            self.original_result.codec != "composer.preview-observation.v1"
            or self.original_result.byte_length > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        ):
            raise ValueError("preview tail receipt cannot retain another or oversized original result")
        return self


type PreviewIncompleteReceipt = ValidationIncompleteReceipt | PreviewTailIncompleteReceipt
_INCOMPLETE_RECEIPT = TypeAdapter[PreviewIncompleteReceipt](PreviewIncompleteReceipt)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CompletePreviewReturn:
    original: OriginalPreviewObservation
    chunk: PrivateChunk
    feedback: str


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class OversizedPreviewReturn:
    source: PreviewSourceData
    result: ToolResult
    tolerant: ValidationResult | None
    receipt: ValidationIncompleteReceipt
    measurement: BoundedCanonicalSize


type PreviewReturned = CompletePreviewReturn | OversizedPreviewReturn
_RETURNS: WeakKeyDictionary[PreviewReturned, tuple[str, str]] = WeakKeyDictionary()


class PreviewTailExcess(Exception):
    """The original pre-consume planning error, bound to its original return."""

    def __init__(self, returned: CompletePreviewReturn, original: BaseException, receipt: PreviewTailIncompleteReceipt) -> None:
        super().__init__("complete original preview tail exceeds its retention allowance")
        self.returned = returned
        self.original = original
        self.receipt = receipt


_TAIL_EXCESSES: WeakKeyDictionary[PreviewTailExcess, tuple[CompletePreviewReturn, BaseException, str]] = WeakKeyDictionary()


def observe_preview_tail_excess(returned: CompletePreviewReturn, original: BaseException) -> PreviewTailExcess | None:
    """Observe only native size refusal at an actual unconsumed planner call.

    This is not a writer capability: current issued source, hold, actor and
    inventory must all be re-proved on the terminal writer's connection.
    """
    from elspeth.web.composer.continuation_capture_overflow import ContinuationMandatoryCaptureOverflow
    from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded

    if type(returned) is not CompletePreviewReturn:
        raise AuditIntegrityError("preview tail excess requires its complete original return")
    require_issued_preview_return(returned)
    surface: Literal["private_chunk", "evidence_pair", "retained_history"]
    if type(original) is ContinuationRetainedCapacityExceeded:
        surface, limit = "retained_history", original.limit_bytes
        if original.observed_bytes <= limit:
            raise AuditIntegrityError("preview aggregate excess changed its native measurement")
    else:
        cause = original.__cause__
        if type(cause) is not ContinuationMandatoryCaptureOverflow:
            return None
        witness = cause.witness
        if witness.unit not in ("private_chunk", "evidence_pair") or witness.observed_bytes <= witness.limit_bytes:
            raise AuditIntegrityError("preview mandatory excess changed its native measurement")
        surface, limit = witness.unit, witness.limit_bytes
        if limit != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("preview mandatory excess changed the unchanged document bound")
    receipt = PreviewTailIncompleteReceipt(
        source=returned.original.source,
        surface=surface,
        limit=limit,
        observed_at_least=limit + 1,
        returned_sha256=_RETURNS[returned][0],
        returned_at=datetime.now(UTC).isoformat(timespec="microseconds"),
    )
    observed = PreviewTailExcess(returned, original, receipt)
    _TAIL_EXCESSES[observed] = (returned, original, canonical_json(receipt.model_dump(mode="json")))
    return observed


def require_original_preview_tail_excess(observed: PreviewTailExcess, returned: CompletePreviewReturn) -> None:
    from elspeth.web.composer.continuation_capture_overflow import ContinuationMandatoryCaptureOverflow
    from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded

    if type(observed) is not PreviewTailExcess or type(returned) is not CompletePreviewReturn:
        raise AuditIntegrityError("preview tail excess lacks its actual original identity")
    issued = _TAIL_EXCESSES.get(observed)
    if (
        issued is None
        or issued[0] is not returned
        or observed.returned is not returned
        or observed.original is not issued[1]
        or canonical_json(observed.receipt.model_dump(mode="json")) != issued[2]
        or observed.receipt.source != returned.original.source
        or observed.receipt.observed_at_least != observed.receipt.limit + 1
    ):
        raise AuditIntegrityError("preview tail excess changed its original source or measurement")
    if type(observed.original) is ContinuationRetainedCapacityExceeded:
        if (
            observed.receipt.surface != "retained_history"
            or observed.original.limit_bytes != observed.receipt.limit
            or observed.original.observed_bytes <= observed.original.limit_bytes
        ):
            raise AuditIntegrityError("preview tail excess changed its actual aggregate measurement")
    else:
        cause = observed.original.__cause__
        if (
            type(cause) is not ContinuationMandatoryCaptureOverflow
            or cause.witness.unit != observed.receipt.surface
            or cause.witness.limit_bytes != observed.receipt.limit
            or cause.witness.observed_bytes <= cause.witness.limit_bytes
        ):
            raise AuditIntegrityError("preview tail excess changed its actual mandatory measurement")
    require_issued_preview_return(returned)


class _HashSink(BytesIO):
    """Stream canonical bytes into a digest without retaining oversized text."""

    def __init__(self) -> None:
        super().__init__()
        self.digest = sha256()

    def write(self, data: Buffer) -> int:
        self.digest.update(data)
        return memoryview(data).nbytes


def _returned_hash(source: PreviewSourceData, result: ToolResult, tolerant: ValidationResult | None) -> str:
    admitted = admit_discovery_result("preview_pipeline", result)
    with _HashSink() as sink:
        rfc8785.dump(
            {
                "source": source.model_dump(mode="json"),
                "result": admitted.to_dict(),
                "tolerant": None if tolerant is None else tolerant.model_dump(mode="json", warnings="error"),
            },
            sink,
        )
        return sink.digest.hexdigest()


def _require_returned(
    prepared: PreparedValueBatchCall, source: PreviewSourceData, result: ToolResult, tolerant: ValidationResult | None
) -> None:
    require_preview_source_matches(source, prepared)
    if type(result) is not ToolResult:
        raise AuditIntegrityError("preview return requires its actual nominal ToolResult")
    strict = result.runtime_preflight
    if (
        type(result) is not ToolResult
        or result.updated_state is not prepared.call.pre_state
        or (strict is None and result.success)
        or (strict is not None and type(strict) is not ValidationResult)
        or (tolerant is not None and type(tolerant) is not ValidationResult)
        or (strict is not None and is_pending_interpretation_handoff(strict)) != (tolerant is not None)
    ):
        raise AuditIntegrityError("preview return lost its original complete result/verdict branch")
    # Check original nominal model contents even if model_construct/copy or
    # nested mutable fields bypassed their constructor. No lossy coercion.
    for verdict in (strict, tolerant):
        if verdict is not None:
            encoded = canonical_json(verdict.model_dump(mode="json", warnings="error"))
            checked = ValidationResult.model_validate_json(encoded, strict=True)
            if canonical_json(checked.model_dump(mode="json", warnings="error")) != encoded:
                raise AuditIntegrityError("preview return normalized its original verdict")
    admit_discovery_result("preview_pipeline", result)


def _excess(
    source: PreviewSourceData,
    result: ToolResult,
    tolerant: ValidationResult | None,
    surface: PreviewByteSurface,
    size: BoundedCanonicalSize,
) -> OversizedPreviewReturn:
    if type(size) is not BoundedCanonicalSize or not size.exceeded or size.limit != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("preview incomplete lacks its actual original byte-excess observation")
    fingerprint = _returned_hash(source, result, tolerant)
    value = OversizedPreviewReturn(
        source,
        result,
        tolerant,
        ValidationIncompleteReceipt(
            source=source,
            surface=surface,
            returned_sha256=fingerprint,
            returned_at=datetime.now(UTC).isoformat(timespec="microseconds"),
        ),
        size,
    )
    _RETURNS[value] = (fingerprint, canonical_json(value.receipt.model_dump(mode="json")))
    return value


def project_original_preview_return(
    prepared: PreparedValueBatchCall,
    *,
    source: PreviewSourceData,
    result: ToolResult,
    tolerant: ValidationResult | None = None,
) -> PreviewReturned:
    """Project actual returned DATA; no catching/classifying arbitrary errors."""
    _require_returned(prepared, source, result, tolerant)
    limit = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    verdicts: tuple[tuple[PreviewByteSurface, ValidationResult | None], ...] = (
        ("strict_verdict", result.runtime_preflight),
        ("tolerant_verdict", tolerant),
    )
    for surface, verdict in verdicts:
        if verdict is not None:
            size = bounded_canonical_json_size(verdict.model_dump(mode="json", warnings="error"), limit=limit)
            if size.exceeded:
                return _excess(source, result, tolerant, surface, size)
    # This is the exact unchanged original discovery feedback serializer,
    # including escaping and spacing, not only its canonical outward shape.
    feedback = serialize_admitted_discovery_result(admit_discovery_result("preview_pipeline", result))
    if utf8_size(feedback, limit=limit) > limit:
        return _excess(source, result, tolerant, "tool_feedback", BoundedCanonicalSize(None, limit))
    planned = PreviewObservationData(
        source=source,
        result=prepare_tool_result_value("preview_pipeline", result, env_ref_names=prepared.runtime.env_ref_names),
        tolerant_json=None if tolerant is None else canonical_json(tolerant.model_dump(mode="json", warnings="error")),
    )
    size = measure_preview_data(planned)
    if size.exceeded:
        return _excess(source, result, tolerant, "private_result", size)
    # Semantic/credential/cold-codec failures are not an oversized return.
    original = observe_original_preview_result(prepared, source=source, result=result, strict=result.runtime_preflight, tolerant=tolerant)
    if original.data != planned:
        raise AuditIntegrityError("preview complete projection differs from its exact original planned DATA")
    chunk = encode_preview_data(original.data, env_ref_names=prepared.runtime.env_ref_names)
    value = CompletePreviewReturn(original, chunk, feedback)
    _RETURNS[value] = (_returned_hash(source, result, tolerant), chunk.canonical)
    return value


def require_original_preview_return(value: PreviewReturned, prepared: PreparedValueBatchCall) -> None:
    require_issued_preview_return(value)
    source, result, tolerant = preview_return_fields(value)
    _require_returned(prepared, source, result, tolerant)
    if type(value) is CompletePreviewReturn:
        require_original_preview_observation(value.original, prepared)


def preview_return_fields(value: PreviewReturned) -> tuple[PreviewSourceData, ToolResult, ValidationResult | None]:
    if type(value) is CompletePreviewReturn:
        return value.original.source, value.original.result, value.original.tolerant
    if type(value) is OversizedPreviewReturn:
        return value.source, value.result, value.tolerant
    raise AuditIntegrityError("preview return has no closed original projection")


def require_issued_preview_return(value: PreviewReturned) -> None:
    """Validate the same issued return after pure adoption changed batch DATA.

    The SQL writer must independently replay current actor/source/hold proof.
    This identity/bytes observation never grants it current write authority.
    """
    if type(value) not in (CompletePreviewReturn, OversizedPreviewReturn) or value not in _RETURNS:
        raise AuditIntegrityError("preview return lacks its actual issued original identity")
    if type(value) is CompletePreviewReturn:
        source, result, tolerant = value.original.source, value.original.result, value.original.tolerant
        if (
            value.chunk.canonical != _RETURNS[value][1]
            or canonical_json(value.original.data.model_dump(mode="json")) != value.chunk.canonical
            or value.original.data.source != source
        ):
            raise AuditIntegrityError("preview return changed its original complete chunk")
        if serialize_admitted_discovery_result(admit_discovery_result("preview_pipeline", result)) != value.feedback:
            raise AuditIntegrityError("preview return changed its original complete feedback")
    elif type(value) is OversizedPreviewReturn:
        source, result, tolerant = value.source, value.result, value.tolerant
        if (
            not value.measurement.exceeded
            or value.measurement.limit != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
            or value.receipt.source != source
            or value.receipt.returned_sha256 != _RETURNS[value][0]
            or canonical_json(value.receipt.model_dump(mode="json")) != _RETURNS[value][1]
        ):
            raise AuditIntegrityError("preview return changed its original missing-output facts")
    else:
        raise AuditIntegrityError("preview return has no closed original projection")
    if sha256(canonical_json(result.updated_state.to_dict()).encode("utf-8")).hexdigest() != source.state_sha256:
        raise AuditIntegrityError("preview return changed its original read-only state")
    if _returned_hash(source, result, tolerant) != _RETURNS[value][0]:
        raise AuditIntegrityError("preview return changed its original output after observation")


class PreviewTerminalHoldContract(BaseModel):
    """Source-bound fixed fallback DATA; no provider hold or physical attempt."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.preview-hold.v1", "composer.preview-hold.v2"] = "composer.preview-hold.v2"
    source: PreviewSourceData
    diagnostic_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    document_bytes: tuple[int, ...]
    reserved_bytes: int = Field(gt=0)
    env_ref_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PreviewReturnCredit:
    transaction: RootTransaction
    contract: PreviewTerminalHoldContract
    returned: PreviewReturned


_CREDIT_KEY = "composer_preview_return_credit"


@contextmanager
def preview_returned_credit_scope(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    returned: PreviewReturned,
    env_ref_names: frozenset[str],
) -> Iterator[None]:
    """Spend only this original returned result's hold in this transaction.

    The row stays active until the actual effect is resolved. Thus every other
    writer still sees its reservation, including an exact-fit result whose
    first chunk must be inserted before the effect's receipt FK can be set.
    """
    require_issued_preview_return(returned)
    source = preview_return_fields(returned)[0]
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    if (str(running.claim.session_id), running.claim.operation_id, running.claim.segment_sequence, running.claim.ownership_generation) != (
        source.session_id,
        source.operation_id,
        source.sequence,
        source.generation,
    ):
        raise AuditIntegrityError("preview return credit changed its actual current actor")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active or _CREDIT_KEY in conn.info:
        raise AuditIntegrityError("preview credit requires its sole original active transaction")
    contract = read_preview_terminal_hold_on_connection(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        effect_key=_effect_key(source),
        env_ref_names=env_ref_names,
    )
    if contract.source != source:
        raise AuditIntegrityError("preview credit replaced its actual original returned source")
    credit = _PreviewReturnCredit(transaction, contract, returned)
    conn.info[_CREDIT_KEY] = credit
    try:
        yield
    finally:
        conn.info.pop(_CREDIT_KEY, None)


def preview_returned_credit_on_connection(conn: Connection, *, session_id: str, operation_id: str) -> int:
    credit = conn.info.get(_CREDIT_KEY)
    if credit is None:
        return 0
    if (
        type(credit) is not _PreviewReturnCredit
        or conn.get_transaction() is not credit.transaction
        or not credit.transaction.is_active
        or (credit.contract.source.session_id, credit.contract.source.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("preview credit crossed its original transaction/source")
    row = conn.execute(
        select(validation_holds.c.state, validation_holds.c.reserved_bytes).where(
            validation_holds.c.session_id == session_id,
            validation_holds.c.operation_id == operation_id,
            validation_holds.c.effect_key == _effect_key(credit.contract.source),
        )
    ).one_or_none()
    if row is None or row.reserved_bytes != credit.contract.reserved_bytes or row.state not in ("active", "consumed"):
        raise AuditIntegrityError("preview return credit lost its original hold row")
    return credit.contract.reserved_bytes if row.state == "active" else 0


def _effect_key(source: PreviewSourceData) -> str:
    return f"tool:{source.response.digest}:{source.tool_index}"


def _incomplete_event(receipt: PreviewIncompleteReceipt, chunk: PrivateChunk) -> str:
    return canonical_json(
        {
            "owner": "composer_preview",
            "outcome": "validation_incomplete",
            "effect_key": _effect_key(receipt.source),
            "source_checkpoint_id": receipt.source.checkpoint_id,
            "receipt": chunk.reference.model_dump(mode="json"),
        }
    )


def _incomplete_root(root: ContinuationFrameStack, *, source: PreviewSourceData, receipt_id: str, cursor: int) -> ContinuationFrameStack:
    key = _effect_key(source)
    matches = tuple(entry for entry in root.effect_frontier if entry.effect_id == key)
    if matches != (EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),):
        raise AuditIntegrityError("preview terminal lost its one actual dispatched effect")
    if any(entry.disposition not in ("committed", "refused") for entry in root.effect_frontier if entry.effect_id != key):
        raise AuditIntegrityError("preview fallback cannot dispose another unresolved owner")
    return root.model_copy(
        update={
            "cursor": cursor,
            "effect_frontier": tuple(
                EffectFrontierEntry(effect_id=key, disposition="validation_incomplete", receipt_id=receipt_id)
                if entry.effect_id == key
                else entry
                for entry in root.effect_frontier
            ),
        }
    )


def _fallback_documents_v1(root: ContinuationFrameStack, *, source: PreviewSourceData) -> tuple[str, ...]:
    """Closed maximum of the actual five payload documents in this tail.

    Zero hex is capacity DATA only. No planning value is a returned-result
    witness or written receipt. Every actual digest has the same fixed width.
    The longest closed surface/failure code comes from the actual owned enums.
    """
    from typing import get_args

    surface = max(get_args(PreviewByteSurface.__value__), key=lambda item: len(canonical_json(item).encode("utf-8")))
    candidate = ValidationIncompleteReceipt(
        source=source, surface=surface, returned_sha256="0" * 64, returned_at="9999-12-31T23:59:59.999999+00:00"
    )
    canonical = canonical_json(candidate.model_dump(mode="json"))
    chunk = encode_private_chunk(codec="composer.preview-incomplete.v1", canonical=canonical)
    receipt_id = f"validation-incomplete:{chunk.reference.digest}"
    first = _incomplete_root(root, source=source, receipt_id=receipt_id, cursor=root.cursor + 1)
    final = first.model_copy(update={"cursor": first.cursor + 1})
    failure = max(COMPOSER_OPERATION_FAILURE_CODES, key=lambda item: len(canonical_json(item).encode("utf-8")))
    settled = canonical_json(
        {
            "version": 1,
            "sequence": source.sequence,
            "generation": source.generation,
            "status": "failed",
            "failure_code": failure,
        }
    )
    return (
        canonical,
        _incomplete_event(candidate, chunk),
        encode_continuation_snapshot(first),
        settled,
        encode_continuation_snapshot(final),
    )


def _receipt_documents(root: ContinuationFrameStack, receipt: PreviewIncompleteReceipt, failure: str) -> tuple[str, ...]:
    canonical = canonical_json(receipt.model_dump(mode="json"))
    codec = "composer.preview-incomplete.v2" if type(receipt) is PreviewTailIncompleteReceipt else "composer.preview-incomplete.v1"
    chunk = encode_private_chunk(codec=codec, canonical=canonical)
    source = receipt.source
    first = _incomplete_root(root, source=source, receipt_id=f"validation-incomplete:{chunk.reference.digest}", cursor=root.cursor + 1)
    final = first.model_copy(update={"cursor": first.cursor + 1})
    return (
        canonical,
        _incomplete_event(receipt, chunk),
        encode_continuation_snapshot(first),
        canonical_json(
            {"version": 1, "sequence": source.sequence, "generation": source.generation, "status": "failed", "failure_code": failure}
        ),
        encode_continuation_snapshot(final),
    )


def _fallback_documents(
    root: ContinuationFrameStack,
    *,
    source: PreviewSourceData,
    version: Literal["composer.preview-hold.v1", "composer.preview-hold.v2"] = "composer.preview-hold.v1",
) -> tuple[str, ...]:
    original = _fallback_documents_v1(root, source=source)
    if version == "composer.preview-hold.v1":
        return original
    from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy

    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("preview tail fallback requires its immutable V4 allowance")
    bound = max(CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, policy.max_retained_payload_bytes)
    candidate = PreviewTailIncompleteReceipt(
        source=source,
        surface="retained_history",
        limit=bound,
        observed_at_least=bound + 1,
        original_result=PrivateChunkRef(
            codec="composer.preview-observation.v1", digest="0" * 64, byte_length=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        ),
        returned_sha256="0" * 64,
        returned_at="9999-12-31T23:59:59.999999+00:00",
    )
    failure = max(COMPOSER_OPERATION_FAILURE_CODES, key=lambda item: len(canonical_json(item).encode("utf-8")))
    extended = _receipt_documents(root, candidate, failure)
    return tuple(max(pair, key=lambda text: len(text.encode("utf-8"))) for pair in zip(original, extended, strict=True))


def _require_original_preview_source_on_connection(conn: Connection, *, source: PreviewSourceData, env_ref_names: frozenset[str]) -> None:
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data
    from elspeth.web.composer.continuation_phase_transitions import read_tool_issue_data_on_connection
    from elspeth.web.sessions.continuation_primary import read_original_committed_primary_response_data_on_connection

    if type(source) is not PreviewSourceData or stable_hash(sorted(env_ref_names)) != source.environment_sha256:
        raise AuditIntegrityError("preview SQL source requires its closed original locator")
    sid, operation = source.session_id, source.operation_id
    issue = read_tool_issue_data_on_connection(
        conn, session_id=UUID(sid), operation_id=operation, effect_key=_effect_key(source), env_ref_names=env_ref_names
    )
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == source.checkpoint_id,
            checkpoints.c.cursor == source.cursor,
        )
    ).one_or_none()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == source.sequence,
            attempts.c.generation == source.generation,
        )
    ).one_or_none()
    if (
        checkpoint is None
        or creator is None
        or issue.previous_checkpoint_id != source.checkpoint_id
        or issue.cursor != source.cursor + 1
        or issue.preview_source_json != canonical_json(source.model_dump(mode="json"))
        or (issue.sequence, issue.generation, issue.tool_index, issue.tool_call_id, issue.response)
        != (source.sequence, source.generation, source.tool_index, source.tool_call_id, source.response)
        or (creator.claim_attempt, creator.session_operation_id, creator.session_operation_epoch)
        != (source.claim_attempt, source.creator_operation_id, source.creator_operation_epoch)
        or type(creator.session_operation_lease_token) is not str
        or sha256(creator.session_operation_lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
    ):
        raise AuditIntegrityError("preview hold lost its actual issue/creator/checkpoint")
    snapshot = read_chunk(conn, session_id=sid, operation_id=operation, key=checkpoint.snapshot_chunk_key)
    root = decode_continuation_snapshot(snapshot.decode("utf-8", errors="strict"))
    if sha256(snapshot).hexdigest() != source.snapshot_sha256 or root.policy_hash != source.policy_sha256:
        raise AuditIntegrityError("preview hold changed its exact original snapshot/policy")
    encoded = read_chunk(conn, session_id=sid, operation_id=operation, key=f"value:{issue.prepared.codec}:{issue.prepared.digest}")
    prepared = decode_prepared_batch_data(encoded.decode("utf-8", errors="strict"), env_ref_names=env_ref_names)
    call = prepared.batch.current_call
    if (
        sha256(encoded).hexdigest() != source.prepared_batch_sha256
        or len(prepared.calls) != 1
        or call is None
        or call.tool_name != "preview_pipeline"
        or call.arguments != {}
        or call.index != source.tool_index
        or sha256(call.tool_call.function.arguments.encode("utf-8")).hexdigest() != source.raw_arguments_sha256
        or sha256(canonical_json(call.pre_state.to_dict()).encode("utf-8")).hexdigest() != source.state_sha256
    ):
        raise AuditIntegrityError("preview hold changed its original complete prepared call")
    read_original_committed_primary_response_data_on_connection(
        conn,
        session_id=UUID(sid),
        operation_id=operation,
        source_checkpoint_id=source.checkpoint_id,
        source_cursor=source.cursor,
        response=source.response,
    )


def reserve_preview_terminal_hold_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    source: PreviewSourceData,
    env_ref_names: frozenset[str],
    created_at: datetime,
) -> PreviewTerminalHoldContract:
    """Reserve the actual unpaid fallback in the issuing transaction."""
    from elspeth.web.sessions.continuation_retained_capacity import admit_retained_payload_chunks, load_continuation_read_contract

    _require_current_continuation_running(conn, running)
    if (
        str(running.claim.session_id) != source.session_id
        or running.claim.operation_id != source.operation_id
        or running.session_operation_context.fence.operation_id != source.creator_operation_id
        or running.session_operation_context.fence.operation_epoch != source.creator_operation_epoch
        or sha256(running.session_operation_context.fence.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
    ):
        raise AuditIntegrityError("preview reservation changed its actual current actor")
    _require_original_preview_source_on_connection(conn, source=source, env_ref_names=env_ref_names)
    stored = load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id)
    root = decode_continuation_snapshot(stored.payload_json)
    frame = root.frames[-1]
    if (
        stored.cursor != source.cursor + 1
        or type(frame) is not ToolBatchFrame
        or frame.phase != "effect_reconciling"
        or root.pending_owner_results
        or root.pending_settlement_ids
    ):
        raise AuditIntegrityError("preview reservation lost its exact unexecuted issue cut")
    read_contract = load_continuation_read_contract(conn, session_id=source.session_id, operation_id=source.operation_id)
    if (
        read_contract.version != "composer.continuation.policy.v4"
        or read_contract.policy_hash != source.policy_sha256
        or type(read_contract.allowance) is not int
        or read_contract.allowance <= 0
    ):
        raise AuditIntegrityError("preview reservation requires its actual immutable V4 policy")
    documents = _fallback_documents(root, source=source, version="composer.preview-hold.v2")
    sizes = tuple(len(document.encode("utf-8")) for document in documents)
    if any(size > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES for size in sizes):
        raise AuditIntegrityError("preview has no complete bounded unpaid terminal fallback")
    contract = PreviewTerminalHoldContract(
        source=source,
        diagnostic_id=str(uuid4()),
        document_bytes=sizes,
        reserved_bytes=sum(sizes),
        env_ref_names=tuple(sorted(env_ref_names)),
    )
    encoded = canonical_json(contract.model_dump(mode="json"))
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=(),
        retained_payload_limit_bytes=read_contract.allowance,
        _new_hold_bytes=contract.reserved_bytes,
    )
    conn.execute(
        insert(validation_holds).values(
            session_id=source.session_id,
            operation_id=source.operation_id,
            effect_key=_effect_key(source),
            policy_sha256=source.policy_sha256,
            allowance_bytes=read_contract.allowance,
            source_checkpoint_id=source.checkpoint_id,
            source_cursor=source.cursor,
            source_json=encoded,
            source_sha256=sha256(encoded.encode("utf-8")).hexdigest(),
            reserved_bytes=contract.reserved_bytes,
            fallback_contract_sha256=sha256(canonical_json(list(sizes)).encode("utf-8")).hexdigest(),
            state="active",
            created_at=created_at,
            consumed_at=None,
        )
    )
    return contract


def _issued_root_on_connection(conn: Connection, source: PreviewSourceData) -> ContinuationFrameStack:
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == source.session_id,
            checkpoints.c.operation_id == source.operation_id,
            checkpoints.c.cursor == source.cursor + 1,
        )
    ).one_or_none()
    if checkpoint is None:
        raise AuditIntegrityError("preview hold lost its immutable issued checkpoint")
    encoded = read_chunk(conn, session_id=source.session_id, operation_id=source.operation_id, key=checkpoint.snapshot_chunk_key)
    root = decode_continuation_snapshot(encoded.decode("utf-8", errors="strict"))
    if (
        root.cursor != source.cursor + 1
        or root.logical.session_id != source.session_id
        or root.logical.operation_id != source.operation_id
        or root.policy_hash != source.policy_sha256
    ):
        raise AuditIntegrityError("preview hold changed its immutable issued root")
    return root


def read_preview_terminal_hold_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> PreviewTerminalHoldContract:
    """Reprove original issue/paid source and exact bounded fallback DATA."""
    from elspeth.web.sessions.continuation_retained_capacity import load_continuation_read_contract

    row = conn.execute(
        select(validation_holds).where(
            validation_holds.c.session_id == session_id,
            validation_holds.c.operation_id == operation_id,
            validation_holds.c.effect_key == effect_key,
        )
    ).one_or_none()
    if row is None or type(row.source_json) is not str:
        raise AuditIntegrityError("preview result has no original unpaid terminal hold")
    contract = PreviewTerminalHoldContract.model_validate_json(row.source_json, strict=True)
    source = contract.source
    stored = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if (
        (source.session_id, source.operation_id, _effect_key(source)) != (session_id, operation_id, effect_key)
        or stored.version != "composer.continuation.policy.v4"
        or row.policy_sha256 != stored.policy_hash
        or row.policy_sha256 != source.policy_sha256
        or row.allowance_bytes != stored.allowance
        or row.source_checkpoint_id != source.checkpoint_id
        or row.source_cursor != source.cursor
        or canonical_json(contract.model_dump(mode="json")) != row.source_json
        or sha256(row.source_json.encode("utf-8")).hexdigest() != row.source_sha256
        or row.reserved_bytes != contract.reserved_bytes
        or contract.env_ref_names != tuple(sorted(env_ref_names))
        or stable_hash(list(contract.env_ref_names)) != source.environment_sha256
        or row.state not in ("active", "consumed")
        or (row.state == "active") != (row.consumed_at is None)
    ):
        raise AuditIntegrityError("preview hold differs from original immutable admission")
    _require_original_preview_source_on_connection(conn, source=source, env_ref_names=env_ref_names)
    documents = _fallback_documents(_issued_root_on_connection(conn, source), source=source, version=contract.schema_version)
    sizes = tuple(len(document.encode("utf-8")) for document in documents)
    if (
        sizes != contract.document_bytes
        or sum(sizes) != contract.reserved_bytes
        or any(size > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES for size in sizes)
        or sha256(canonical_json(list(sizes)).encode("utf-8")).hexdigest() != row.fallback_contract_sha256
    ):
        raise AuditIntegrityError("preview hold changed its actual complete fallback bound")
    return contract


def live_preview_terminal_hold_bytes_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    policy_version: str,
    policy_sha256: str | None,
    allowance: int | None,
) -> int:
    """Observe every hold's actual bounded contract before giving capacity."""
    total = 0
    for row in conn.execute(
        select(validation_holds).where(validation_holds.c.session_id == session_id, validation_holds.c.operation_id == operation_id)
    ):
        if (
            policy_version != "composer.continuation.policy.v4"
            or type(policy_sha256) is not str
            or type(allowance) is not int
            or allowance <= 0
        ):
            raise AuditIntegrityError("legacy continuation acquired an unpaid preview hold")
        contract = PreviewTerminalHoldContract.model_validate_json(row.source_json, strict=True)
        source = contract.source
        sizes = tuple(
            len(document.encode("utf-8"))
            for document in _fallback_documents(_issued_root_on_connection(conn, source), source=source, version=contract.schema_version)
        )
        if (
            (source.session_id, source.operation_id, _effect_key(source)) != (session_id, operation_id, row.effect_key)
            or row.policy_sha256 != policy_sha256
            or source.policy_sha256 != policy_sha256
            or row.allowance_bytes != allowance
            or row.source_checkpoint_id != source.checkpoint_id
            or row.source_cursor != source.cursor
            or canonical_json(contract.model_dump(mode="json")) != row.source_json
            or sha256(row.source_json.encode("utf-8")).hexdigest() != row.source_sha256
            or sizes != contract.document_bytes
            or sum(sizes) != contract.reserved_bytes
            or row.reserved_bytes != contract.reserved_bytes
            or contract.env_ref_names != tuple(sorted(set(contract.env_ref_names)))
            or stable_hash(list(contract.env_ref_names)) != source.environment_sha256
            or not 0 < contract.reserved_bytes <= allowance
            or any(size > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES for size in sizes)
            or sha256(canonical_json(list(sizes)).encode("utf-8")).hexdigest() != row.fallback_contract_sha256
            or row.state not in ("active", "consumed")
            or (row.state == "active") != (row.consumed_at is None)
        ):
            raise AuditIntegrityError("unpaid preview hold lost its original bounded capacity facts")
        if row.state == "active":
            total += contract.reserved_bytes
    return total


def consume_preview_terminal_hold_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    effect_key: str,
    env_ref_names: frozenset[str],
    consumed_at: datetime,
    returned: PreviewReturned,
) -> PreviewTerminalHoldContract:
    """Spend only an actual returned result inside its disposition transaction.

    The transaction's actual-return credit covers payload insertion first.
    Consumption then requires the actual resolved effect/receipt. Any failure
    rolls it back with the whole result tail.
    """
    require_issued_preview_return(returned)
    contract = read_preview_terminal_hold_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key, env_ref_names=env_ref_names
    )
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key
        )
    ).one()
    if (
        effect.owner != "tool_dispatch"
        or effect.disposition not in ("committed", "validation_incomplete")
        or effect.receipt_key is None
        or effect.result_chunk_key is None
        or effect.provider_attempt_id is not None
        or contract.source != preview_return_fields(returned)[0]
    ):
        raise AuditIntegrityError("preview hold cannot release an unresolved or foreign result")
    changed = conn.execute(
        update(validation_holds)
        .where(
            validation_holds.c.session_id == session_id,
            validation_holds.c.operation_id == operation_id,
            validation_holds.c.effect_key == effect_key,
            validation_holds.c.state == "active",
        )
        .values(state="consumed", consumed_at=consumed_at)
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("preview result repeated its original reservation consumption")
    return contract


@dataclass(frozen=True, slots=True)
class PreviewValidationIncompleteTerminal:
    snapshot: StoredContinuationSnapshot
    receipt: PreviewIncompleteReceipt
    error: ComposerOperationError
    env_ref_names: frozenset[str]


def retain_preview_validation_incomplete_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    returned: OversizedPreviewReturn,
    env_ref_names: frozenset[str],
    recorded_at: datetime,
) -> PreviewValidationIncompleteTerminal:
    with preview_returned_credit_scope(conn, running, returned=returned, env_ref_names=env_ref_names):
        return _retain_preview_validation_incomplete_on_connection(
            conn, running, returned=returned, env_ref_names=env_ref_names, recorded_at=recorded_at
        )


def retain_preview_tail_incomplete_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    returned: CompletePreviewReturn,
    excess: PreviewTailExcess,
    env_ref_names: frozenset[str],
    recorded_at: datetime,
) -> PreviewValidationIncompleteTerminal:
    require_original_preview_tail_excess(excess, returned)
    with preview_returned_credit_scope(conn, running, returned=returned, env_ref_names=env_ref_names):
        return _retain_preview_validation_incomplete_on_connection(
            conn, running, returned=returned, env_ref_names=env_ref_names, recorded_at=recorded_at, tail=excess
        )


def _retainable_tail_original(
    conn: Connection,
    *,
    root: ContinuationFrameStack,
    returned: CompletePreviewReturn,
    receipt: PreviewTailIncompleteReceipt,
    failure: str,
) -> tuple[PreviewTailIncompleteReceipt, PrivateChunk | None]:
    from elspeth.web.sessions.continuation_retained_capacity import (
        ContinuationRetainedCapacityExceeded,
        ProspectiveContinuationChunk,
        admit_retained_payload_chunks,
        load_continuation_read_contract,
    )

    source = receipt.source
    candidate = receipt.model_copy(update={"original_result": returned.chunk.reference})
    documents = _receipt_documents(root, candidate, failure)
    # Sizing keys are unique prospective DATA, never published identities.
    # They prevent fictional deduplication of actual mandatory terminal bytes.
    prospective = tuple(
        ProspectiveContinuationChunk(f"preview-terminal-plan:{uuid4()}", text.encode("utf-8"), "event.v2") for text in documents
    )
    contract = load_continuation_read_contract(conn, session_id=source.session_id, operation_id=source.operation_id)
    if contract.allowance is None:
        raise AuditIntegrityError("preview optional original lost its immutable retained limit")
    try:
        admit_retained_payload_chunks(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            prospective=(
                *prospective,
                ProspectiveContinuationChunk(
                    private_chunk_key(returned.chunk), returned.chunk.canonical.encode("utf-8"), returned.chunk.reference.codec
                ),
            ),
            retained_payload_limit_bytes=contract.allowance,
        )
    except ContinuationRetainedCapacityExceeded:
        return receipt, None
    return candidate, returned.chunk


def _retain_preview_validation_incomplete_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    returned: OversizedPreviewReturn | CompletePreviewReturn,
    env_ref_names: frozenset[str],
    recorded_at: datetime,
    tail: PreviewTailExcess | None = None,
) -> PreviewValidationIncompleteTerminal:
    """Commit the original measured missing output and terminal atomically.

    Caller owns the real session lock/transaction and returned-result timing
    scope. No error, cancellation or unknown SQL completion is swallowed.
    """
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    receipt: PreviewIncompleteReceipt
    if type(returned) is CompletePreviewReturn:
        if tail is None:
            raise AuditIntegrityError("complete preview requires its original unconsumed tail excess")
        require_original_preview_tail_excess(tail, returned)
        receipt = tail.receipt
    elif type(returned) is OversizedPreviewReturn and tail is None:
        receipt = returned.receipt
    else:
        raise AuditIntegrityError("preview incomplete requires its actual original size refusal")
    require_issued_preview_return(returned)
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    source = preview_return_fields(returned)[0]
    if (source.session_id, source.operation_id, source.sequence, source.generation, source.claim_attempt) != (
        str(running.claim.session_id),
        running.claim.operation_id,
        running.claim.segment_sequence,
        running.claim.ownership_generation,
        running.claim.attempt,
    ):
        raise AuditIntegrityError("preview incomplete changed its actual physical owner")
    stored = load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id)
    root = decode_continuation_snapshot(stored.payload_json)
    if stored.cursor != source.cursor + 1:
        raise AuditIntegrityError("preview incomplete replaced its actual issued head")
    contract = read_preview_terminal_hold_on_connection(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        effect_key=_effect_key(source),
        env_ref_names=env_ref_names,
    )
    if tail is not None and contract.schema_version != "composer.preview-hold.v2":
        raise AuditIntegrityError("preview tail excess cannot spend a legacy raw-result hold")
    if type(receipt) is PreviewTailIncompleteReceipt:
        from elspeth.web.sessions.continuation_retained_capacity import load_continuation_read_contract

        read_contract = load_continuation_read_contract(conn, session_id=source.session_id, operation_id=source.operation_id)
        expected_limit = read_contract.allowance if receipt.surface == "retained_history" else CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        if receipt.limit != expected_limit or receipt.observed_at_least != receipt.limit + 1:
            raise AuditIntegrityError("preview tail excess changed its actual immutable retention limit")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)).one()
    # Durable Stop keeps its public priority; the observed incomplete tool
    # effect/receipt remains truthful and is never replayed.
    error = (
        request_cancelled_error(request_id=parent.request_id)
        if parent.cancel_requested_at is not None
        else ComposerOperationError(
            http_status=422,
            failure_code="operation_failed",
            error_type="validation_incomplete",
            body={
                "error_type": "validation_incomplete",
                "reason": "preview_tail_capacity_exceeded" if tail is not None else "oversized_preview_result",
            },
            diagnostic_id=contract.diagnostic_id,
        )
    )
    retained_original: PrivateChunk | None = None
    if type(returned) is CompletePreviewReturn and type(receipt) is PreviewTailIncompleteReceipt:
        receipt, retained_original = _retainable_tail_original(
            conn, root=root, returned=returned, receipt=receipt, failure=error.failure_code
        )
    canonical = canonical_json(receipt.model_dump(mode="json"))
    codec: Literal["composer.preview-incomplete.v1", "composer.preview-incomplete.v2"] = (
        "composer.preview-incomplete.v2" if type(receipt) is PreviewTailIncompleteReceipt else "composer.preview-incomplete.v1"
    )
    chunk = encode_private_chunk(codec=codec, canonical=canonical, env_ref_names=env_ref_names)
    receipt_id = f"validation-incomplete:{chunk.reference.digest}"
    first = _incomplete_root(root, source=source, receipt_id=receipt_id, cursor=root.cursor + 1)
    final = first.model_copy(update={"cursor": first.cursor + 1})
    actual_documents = _receipt_documents(root, receipt, error.failure_code)
    sizes = tuple(len(document.encode("utf-8")) for document in actual_documents)
    if len(sizes) != len(contract.document_bytes) or any(
        actual > allowed for actual, allowed in zip(sizes, contract.document_bytes, strict=True)
    ):
        raise AuditIntegrityError("preview incomplete exceeded its original complete reserved fallback")
    if retained_original is not None:
        put_chunk(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            key=private_chunk_key(retained_original),
            payload=retained_original.canonical.encode("utf-8"),
            codec=retained_original.reference.codec,
        )
    put_chunk(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        key=private_chunk_key(chunk),
        payload=canonical.encode("utf-8"),
        codec=chunk.reference.codec,
    )
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == _effect_key(source),
            effects.c.owner == "tool_dispatch",
            effects.c.disposition == "dispatched",
            effects.c.creator_sequence == source.sequence,
            effects.c.creator_generation == source.generation,
        )
        .values(
            disposition="validation_incomplete",
            receipt_key=receipt_id,
            result_chunk_key=private_chunk_key(chunk),
            response_finished_at=datetime.fromisoformat(receipt.returned_at),
        )
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("preview incomplete lost its original dispatched effect")
    consume_preview_terminal_hold_on_connection(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        effect_key=_effect_key(source),
        env_ref_names=env_ref_names,
        consumed_at=recorded_at,
        returned=returned,
    )
    append_complete_snapshot(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=encode_continuation_snapshot(first),
        event_batch=(ContinuationEvent("checkpoint", actual_documents[1]),),
        handoff=False,
    )
    settle_continuation_on_connection(conn, running, outcome=error)
    terminal = load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id)
    if terminal.payload_json != encode_continuation_snapshot(final):
        raise AuditIntegrityError("preview incomplete changed its actual reserved terminal snapshot")
    return PreviewValidationIncompleteTerminal(terminal, receipt, error, env_ref_names)


def consume_preview_holds_for_failed_settlement_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, unresolved: tuple[str, ...], consumed_at: datetime
) -> tuple[str, ...]:
    """Spend the failed terminal tail without claiming a returned result.

    The real terminal owner calls this under its locked failed settlement.
    An unresolved dispatch remains UNKNOWN, with no receipt or replay grant.
    Provider holds and physical-cost custody remain independent.
    """
    marked: list[str] = []
    for row in conn.execute(
        select(validation_holds).where(
            validation_holds.c.session_id == session_id,
            validation_holds.c.operation_id == operation_id,
            validation_holds.c.state == "active",
        )
    ):
        descriptor = PreviewTerminalHoldContract.model_validate_json(row.source_json, strict=True)
        read_preview_terminal_hold_on_connection(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            effect_key=row.effect_key,
            env_ref_names=frozenset(descriptor.env_ref_names),
        )
        effect = conn.execute(
            select(effects).where(
                effects.c.session_id == session_id,
                effects.c.operation_id == operation_id,
                effects.c.effect_key == row.effect_key,
            )
        ).one()
        if (
            row.effect_key not in unresolved
            or effect.owner != "tool_dispatch"
            or effect.disposition != "dispatched"
            or effect.receipt_key is not None
            or effect.result_chunk_key is not None
            or effect.provider_attempt_id is not None
        ):
            raise AuditIntegrityError("failed preview settlement cannot consume another known result or owner")
        changed_effect = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == session_id,
                effects.c.operation_id == operation_id,
                effects.c.effect_key == row.effect_key,
                effects.c.owner == "tool_dispatch",
                effects.c.disposition == "dispatched",
            )
            .values(disposition="unknown")
        )
        if changed_effect.rowcount != 1:
            raise AuditIntegrityError("failed preview settlement lost its actual unknown effect")
        changed = conn.execute(
            update(validation_holds)
            .where(
                validation_holds.c.session_id == session_id,
                validation_holds.c.operation_id == operation_id,
                validation_holds.c.effect_key == row.effect_key,
                validation_holds.c.state == "active",
            )
            .values(state="consumed", consumed_at=consumed_at)
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("failed preview settlement repeated its original reservation")
        marked.append(row.effect_key)
    return tuple(marked)


def _decode_preview_terminal_error(
    *,
    status: str,
    failure_code: str | None,
    result_schema: str | None,
    result_json: str | None,
    result_sha256: str | None,
) -> ComposerOperationError:
    """Validate terminal value bytes; this grants no source or SQL authority."""
    if status != "failed" or result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR or type(result_json) is not str:
        raise AuditIntegrityError("preview incomplete has no sealed failed parent result")
    try:
        error = ComposerOperationError.model_validate_json(result_json, strict=True)
        digest = composer_operation_result_hash(result_json)
    except (TypeError, ValueError) as malformed:
        raise AuditIntegrityError("preview incomplete has a malformed owned terminal error") from malformed
    if canonical_json(error.model_dump(mode="json")) != result_json or result_sha256 != digest or failure_code != error.failure_code:
        raise AuditIntegrityError("preview incomplete changed its canonical terminal error or digest")
    return error


def read_preview_validation_incomplete_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> PreviewValidationIncompleteTerminal | None:
    """Cold no-reexecution proof of original issue, hold, receipt and terminal."""
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
    from elspeth.web.sessions.time_normalization import restore_utc

    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key
        )
    ).one_or_none()
    if effect is None or effect.disposition != "validation_incomplete":
        return None
    contract = read_preview_terminal_hold_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key, env_ref_names=env_ref_names
    )
    source = contract.source
    hold = conn.execute(
        select(validation_holds.c.state, validation_holds.c.consumed_at).where(
            validation_holds.c.session_id == session_id,
            validation_holds.c.operation_id == operation_id,
            validation_holds.c.effect_key == effect_key,
        )
    ).one()
    if (
        effect.owner != "tool_dispatch"
        or effect.provider_attempt_id is not None
        or effect.response_finished_at is None
        or type(effect.result_chunk_key) is not str
        or type(effect.receipt_key) is not str
        or hold.state != "consumed"
        or hold.consumed_at is None
        or (effect.creator_sequence, effect.creator_generation) != (source.sequence, source.generation)
    ):
        raise AuditIntegrityError("preview incomplete lost its actual unpaid effect/consumed hold")
    encoded = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=effect.result_chunk_key).decode(
        "utf-8", errors="strict"
    )
    receipt = _INCOMPLETE_RECEIPT.validate_json(encoded, strict=True)
    codec: Literal["composer.preview-incomplete.v1", "composer.preview-incomplete.v2"] = (
        "composer.preview-incomplete.v2" if type(receipt) is PreviewTailIncompleteReceipt else "composer.preview-incomplete.v1"
    )
    chunk = encode_private_chunk(codec=codec, canonical=encoded, env_ref_names=env_ref_names)
    if type(receipt) is PreviewTailIncompleteReceipt:
        from elspeth.web.sessions.continuation_retained_capacity import load_continuation_read_contract

        read_contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
        expected_limit = read_contract.allowance if receipt.surface == "retained_history" else CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        if (
            contract.schema_version != "composer.preview-hold.v2"
            or receipt.limit != expected_limit
            or receipt.observed_at_least != receipt.limit + 1
        ):
            raise AuditIntegrityError("preview tail receipt changed its original limit or complete-return branch")
        if receipt.original_result is not None:
            reference = receipt.original_result
            if reference.codec != "composer.preview-observation.v1":
                raise AuditIntegrityError("preview tail retained another original result codec")
            original = read_chunk(
                conn, session_id=session_id, operation_id=operation_id, key=f"value:{reference.codec}:{reference.digest}"
            ).decode("utf-8", errors="strict")
            data = decode_preview_data(PrivateChunk(reference, original), expected_source=source, env_ref_names=env_ref_names)
            original_result = decode_tool_result_value(data.result, env_ref_names=env_ref_names)
            tolerant = ValidationResult.model_validate_json(data.tolerant_json, strict=True) if data.tolerant_json is not None else None
            if _returned_hash(source, original_result, tolerant) != receipt.returned_sha256:
                raise AuditIntegrityError("preview tail retained original differs from its returned-result fingerprint")
    if (
        receipt.source != source
        or canonical_json(receipt.model_dump(mode="json")) != encoded
        or effect.result_chunk_key != private_chunk_key(chunk)
        or effect.receipt_key != f"validation-incomplete:{chunk.reference.digest}"
        or restore_utc(effect.response_finished_at) != datetime.fromisoformat(receipt.returned_at)
    ):
        raise AuditIntegrityError("preview incomplete changed its original missing-output receipt")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == session_id, jobs.c.operation_id == operation_id)).one()
    if parent.phase != "settled" or parent.settled_at is None or parent.settled_by != "owner_terminal":
        raise AuditIntegrityError("preview incomplete has no sealed failed parent")
    error = _decode_preview_terminal_error(
        status=parent.status,
        failure_code=parent.failure_code,
        result_schema=parent.result_schema,
        result_json=parent.result_json,
        result_sha256=parent.result_sha256,
    )
    expected_error = (
        request_cancelled_error(request_id=parent.request_id)
        if error.failure_code == "request_cancelled" and parent.cancel_requested_at is not None
        else ComposerOperationError(
            http_status=422,
            failure_code="operation_failed",
            error_type="validation_incomplete",
            body={
                "error_type": "validation_incomplete",
                "reason": "preview_tail_capacity_exceeded" if type(receipt) is PreviewTailIncompleteReceipt else "oversized_preview_result",
            },
            diagnostic_id=contract.diagnostic_id,
        )
    )
    terminal = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    issued = _issued_root_on_connection(conn, source)
    first = _incomplete_root(issued, source=source, receipt_id=effect.receipt_key, cursor=issued.cursor + 1)
    final = first.model_copy(update={"cursor": first.cursor + 1})
    if (
        error != expected_error
        or terminal.payload_json != encode_continuation_snapshot(final)
        or parent.checkpoint_id != terminal.checkpoint_id
        or parent.journal_cursor != terminal.cursor
        or (parent.current_segment_sequence, parent.ownership_generation) != (source.sequence, source.generation)
    ):
        raise AuditIntegrityError("preview incomplete changed its actual terminal source/error")
    expected_events = (
        (issued.cursor + 1, "checkpoint", _incomplete_event(receipt, chunk)),
        (
            issued.cursor + 2,
            "settled",
            canonical_json(
                {
                    "version": 1,
                    "sequence": source.sequence,
                    "generation": source.generation,
                    "status": "failed",
                    "failure_code": error.failure_code,
                }
            ),
        ),
    )
    for cursor, tag, canonical in expected_events:
        event = conn.execute(
            select(events).where(events.c.session_id == session_id, events.c.operation_id == operation_id, events.c.sequence == cursor)
        ).one_or_none()
        if (
            event is None
            or event.tag != tag
            or read_chunk(conn, session_id=session_id, operation_id=operation_id, key=event.payload_chunk_key) != canonical.encode("utf-8")
        ):
            raise AuditIntegrityError("preview incomplete lost its immutable result/terminal journal")
    return PreviewValidationIncompleteTerminal(terminal, receipt, error, env_ref_names)
