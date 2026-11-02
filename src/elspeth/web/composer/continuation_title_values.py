"""Complete received TITLE provider result DATA, before financial settlement.

Decoded source coordinates never materialize a context or authorize a writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from pydantic import Field, ValidationError

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.continuation import ContinuationValue, LogicalRequestBinding, PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    RetainedCompletion,
    decode_continuation_response,
    encode_continuation_response,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call, require_successful_response_audit
from elspeth.web.composer.provider_audit_source import (
    ProviderAuditSource,
    audit_drafts_json,
    capture_provider_audit_source,
    decode_audit_drafts_json,
    provider_attempt_audit_drafts,
    provider_attempt_audit_drafts_for_source,
)
from elspeth.web.sessions._persist_payload import AuditMessageDraft

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection

    from elspeth.web.sessions.continuation import StoredContinuationSnapshot


class TitleProviderResultSnapshot(ContinuationValue):
    schema_version: Literal["composer.title-provider-result.v1"] = "composer.title-provider-result.v1"
    logical: LogicalRequestBinding
    actor_user_id: str = Field(min_length=1)
    effect_key: Literal["provider:3:0"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    creator_sequence: int = Field(ge=0)
    creator_generation: int = Field(ge=0)
    source: ProviderAuditSource
    response_reference: PrivateChunkRef
    response_canonical: str
    audit_drafts_json: str


@dataclass(frozen=True, slots=True)
class DecodedTitleProviderResult:
    snapshot: TitleProviderResultSnapshot
    completion: _AdmittedLLMCompletion
    audit_call: ComposerLLMCall
    audit_drafts: tuple[AuditMessageDraft, ...]
    source: ProviderAuditSource


@dataclass(frozen=True, slots=True)
class CapturedTitleProviderResult:
    chunk: PrivateChunk
    snapshot: TitleProviderResultSnapshot
    completion: _AdmittedLLMCompletion
    audit_call: ComposerLLMCall
    audit_drafts: tuple[AuditMessageDraft, ...]
    source: ProviderAuditSource


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class VerifiedV4TitleStoredSource:
    """Stored V4 policy and TITLE response coordinates proved by the SQL reader."""

    session_id: str
    operation_id: str
    actor_user_id: str
    input_sha256: str
    title_reference: PrivateChunkRef
    policy_hash: str


@dataclass(frozen=True, slots=True)
class _IssuedV4TitleStoredSource:
    connection: Connection
    transaction: object
    nested_transaction: object | None


_ISSUED_STORED_V4_SOURCES: WeakKeyDictionary[VerifiedV4TitleStoredSource, _IssuedV4TitleStoredSource] = WeakKeyDictionary()


def _issue_v4_title_stored_source(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    sequence: int | None,
    generation: int | None,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
    title_reference: PrivateChunkRef,
) -> VerifiedV4TitleStoredSource | None:
    """Derive a V4-only comparison grant from actual stored TITLE lineage."""
    from sqlalchemy import select

    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
    from elspeth.web.sessions.continuation_title_provider import _frame, title_source_chain_data_on_connection
    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    transaction = conn.get_transaction()
    if transaction is None or not conn.in_transaction():
        raise AuditIntegrityError("TITLE stored V4 source lacks its original SQL transaction")
    root = decode_continuation_snapshot(selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4:
        return None
    descriptor, _ref, stage, _source = title_source_chain_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        sequence=sequence,
        generation=generation,
        selected=selected,
        names=names,
    )
    parent = conn.execute(select(jobs).where(jobs.c.session_id == str(session_id), jobs.c.operation_id == operation_id)).one()
    if (
        stage not in ("received", "settled")
        or parent.continuation_policy_version != policy.schema_version
        or parent.continuation_policy_sha256 != policy.policy_hash
        or root.logical.session_id != str(session_id)
        or root.logical.operation_id != operation_id
        or _frame(selected).title_response != title_reference
    ):
        raise AuditIntegrityError("TITLE V4 diagnostic comparison lost its stored policy/source")
    proof = VerifiedV4TitleStoredSource(
        session_id=str(session_id),
        operation_id=operation_id,
        actor_user_id=parent.actor_user_id,
        input_sha256=descriptor.input_sha256,
        title_reference=title_reference,
        policy_hash=policy.policy_hash,
    )
    _ISSUED_STORED_V4_SOURCES[proof] = _IssuedV4TitleStoredSource(conn, transaction, conn.get_nested_transaction())
    return proof


def measure_title_provider_result_size(snapshot: TitleProviderResultSnapshot) -> BoundedCanonicalSize:
    """Measure the complete outer TITLE value, including nested JSON escaping.

    This is a size observation over a validated nominal snapshot, not an
    alternative capture, audit, credential or settlement path.
    """
    if type(snapshot) is not TitleProviderResultSnapshot:
        raise TypeError("TITLE size measurement requires its complete nominal snapshot")
    return bounded_canonical_json_size(snapshot.model_dump(mode="json"), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)


def _require_response_audit(completion: _AdmittedLLMCompletion, call: ComposerLLMCall, *, v4_diagnostic: bool = False) -> None:
    if type(completion) is not _AdmittedLLMCompletion or type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("TITLE result requires complete nominal completion and physical audit")
    metadata = completion.provider_metadata
    if v4_diagnostic:
        from elspeth.web.composer.provider_result_admission import project_v4_provider_diagnostic_metadata

        metadata = project_v4_provider_diagnostic_metadata(metadata)
    require_successful_response_audit(
        call,
        metadata,
        attempt_id=call.call_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )


def capture_title_provider_result(
    *,
    logical: LogicalRequestBinding,
    actor_user_id: str,
    effect_key: Literal["provider:3:0"],
    input_sha256: str,
    creator_sequence: int,
    creator_generation: int,
    completion: _AdmittedLLMCompletion,
    audit_call: ComposerLLMCall,
    context: SessionOperationContext,
    audit_drafts: tuple[AuditMessageDraft, ...],
    env_ref_names: frozenset[str],
) -> CapturedTitleProviderResult:
    if type(logical) is not LogicalRequestBinding:
        raise AuditIntegrityError("TITLE result requires its actual logical DATA")
    source = capture_provider_audit_source(context)
    from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, V4ProviderSdkReturn

    witness = completion.provider_metadata.v4_completeness
    if witness is not None:
        if (
            type(witness) is not V4ProviderResultCompleteness
            or type(witness.sdk_return) is not V4ProviderSdkReturn
            or witness.sdk_return.attempt_id != audit_call.call_id
        ):
            raise AuditIntegrityError("TITLE capture lacks its original source-issued provider return")
        witness.require_result(
            completion.provider_metadata,
            content=completion.message.content,
            tool_calls=completion.tool_batch.calls,
        )
    _require_response_audit(completion, audit_call, v4_diagnostic=witness is not None)
    if audit_drafts_json(audit_drafts) != audit_drafts_json(provider_attempt_audit_drafts(audit_call, context)):
        raise AuditIntegrityError("TITLE result changed its original complete audit draft cohort")
    response = encode_continuation_response(completion, audit_call=audit_call)
    snapshot = TitleProviderResultSnapshot(
        logical=logical,
        actor_user_id=actor_user_id,
        effect_key=effect_key,
        input_sha256=input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        source=source,
        response_reference=response.reference,
        response_canonical=response.canonical,
        audit_drafts_json=audit_drafts_json(audit_drafts),
    )
    chunk = encode_private_chunk(
        codec="composer.title-provider-result.v1",
        canonical=canonical_json(snapshot.model_dump(mode="json")),
        env_ref_names=env_ref_names,
    )
    decode_title_provider_result(chunk, env_ref_names=env_ref_names, original_v4_completion=completion if witness is not None else None)
    return CapturedTitleProviderResult(chunk, snapshot, completion, audit_call, audit_drafts, source)


def decode_title_provider_result(
    chunk: PrivateChunk,
    *,
    env_ref_names: frozenset[str],
    original_v4_completion: _AdmittedLLMCompletion | None = None,
    stored_v4_source: VerifiedV4TitleStoredSource | None = None,
) -> DecodedTitleProviderResult:
    if original_v4_completion is not None and stored_v4_source is not None:
        raise AuditIntegrityError("TITLE response cannot combine live and stored V4 source proofs")
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.title-provider-result.v1":
        raise AuditIntegrityError("TITLE provider result has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        snapshot = TitleProviderResultSnapshot.model_validate_json(chunk.canonical)
    except (ValidationError, ValueError, TypeError) as cause:
        raise AuditIntegrityError("TITLE provider result violates its complete owned shape") from cause
    if canonical_json(snapshot.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("TITLE provider result omitted or normalized retained fields")
    if (
        type(snapshot.source) is not ProviderAuditSource
        or snapshot.logical.session_id != snapshot.source.session_id
        or not snapshot.actor_user_id.strip()
    ):
        raise AuditIntegrityError("TITLE provider result changed its original source session")
    admitted = encode_private_chunk(
        codec="composer.title-provider-result.v1",
        canonical=chunk.canonical,
        env_ref_names=env_ref_names,
    )
    if admitted != chunk:
        raise AuditIntegrityError("TITLE provider result changed its complete private binding")
    response = PrivateChunk(snapshot.response_reference, snapshot.response_canonical)
    completion = decode_continuation_response(response)
    try:
        retained = RetainedCompletion.model_validate_json(response.canonical)
    except ValidationError as cause:
        raise AuditIntegrityError("TITLE response lost its original complete audit") from cause
    if retained.audit_call_json is None or retained.advisor_result_metadata is not None:
        raise AuditIntegrityError("TITLE response requires its original audit and no Advisor result")
    audit_call = decode_physical_audit_call(retained.audit_call_json)
    if stored_v4_source is not None and (
        type(stored_v4_source) is not VerifiedV4TitleStoredSource
        or (issued := _ISSUED_STORED_V4_SOURCES.get(stored_v4_source)) is None
        or issued.connection.get_transaction() is not issued.transaction
        or issued.connection.get_nested_transaction() is not issued.nested_transaction
        or snapshot.logical.session_id != stored_v4_source.session_id
        or snapshot.logical.operation_id != stored_v4_source.operation_id
        or snapshot.actor_user_id != stored_v4_source.actor_user_id
        or snapshot.input_sha256 != stored_v4_source.input_sha256
        or chunk.reference != stored_v4_source.title_reference
        or len(stored_v4_source.policy_hash) != 64
    ):
        raise AuditIntegrityError("TITLE response differs from its verified stored V4 source")
    if original_v4_completion is not None:
        from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, V4ProviderSdkReturn

        original = original_v4_completion
        if type(original) is not _AdmittedLLMCompletion:
            raise AuditIntegrityError("TITLE response lacks its original source-issued V4 result")
        witness = original.provider_metadata.v4_completeness
        if (
            type(witness) is not V4ProviderResultCompleteness
            or type(witness.sdk_return) is not V4ProviderSdkReturn
            or witness.sdk_return.attempt_id != audit_call.call_id
        ):
            raise AuditIntegrityError("TITLE response lacks its original source-issued V4 result")
        witness.require_result(
            original.provider_metadata,
            content=original.message.content,
            tool_calls=original.tool_batch.calls,
        )
        if encode_continuation_response(original, audit_call=audit_call) != response:
            raise AuditIntegrityError("TITLE retained response changed its original source-issued V4 result")
    _require_response_audit(completion, audit_call, v4_diagnostic=stored_v4_source is not None or original_v4_completion is not None)
    if encode_continuation_response(completion, audit_call=audit_call) != response:
        raise AuditIntegrityError("TITLE response omitted or changed original complete response fields")
    drafts = decode_audit_drafts_json(snapshot.audit_drafts_json)
    if audit_drafts_json(provider_attempt_audit_drafts_for_source(audit_call, snapshot.source)) != snapshot.audit_drafts_json:
        raise AuditIntegrityError("TITLE result changed its original complete audit draft cohort")
    return DecodedTitleProviderResult(snapshot, completion, audit_call, drafts, snapshot.source)
