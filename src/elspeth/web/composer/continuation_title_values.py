"""Complete received TITLE provider result DATA, before financial settlement.

Decoded source coordinates never materialize a context or authorize a writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

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


def _require_response_audit(completion: _AdmittedLLMCompletion, call: ComposerLLMCall) -> None:
    if type(completion) is not _AdmittedLLMCompletion or type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("TITLE result requires complete nominal completion and physical audit")
    require_successful_response_audit(
        call,
        completion.provider_metadata,
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
    _require_response_audit(completion, audit_call)
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
    decode_title_provider_result(chunk, env_ref_names=env_ref_names)
    return CapturedTitleProviderResult(chunk, snapshot, completion, audit_call, audit_drafts, source)


def decode_title_provider_result(chunk: PrivateChunk, *, env_ref_names: frozenset[str]) -> DecodedTitleProviderResult:
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
    _require_response_audit(completion, audit_call)
    if encode_continuation_response(completion, audit_call=audit_call) != response:
        raise AuditIntegrityError("TITLE response omitted or changed original complete response fields")
    drafts = decode_audit_drafts_json(snapshot.audit_drafts_json)
    if audit_drafts_json(provider_attempt_audit_drafts_for_source(audit_call, snapshot.source)) != snapshot.audit_drafts_json:
        raise AuditIntegrityError("TITLE result changed its original complete audit draft cohort")
    return DecodedTitleProviderResult(snapshot, completion, audit_call, drafts, snapshot.source)
