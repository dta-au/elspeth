"""Redacted storage projection for composer tool-invocation evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast, final

from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateFailureFacts
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json as primitive_canonical_json
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.authority_hashing import (
    composer_authority_canonical_json,
    project_composer_authority_payload,
    restore_composer_authority_payload,
)
from elspeth.web.composer.diagnostic_evidence import (
    ComposerDiagnosticEvidence,
    DiagnosticPayload,
    build_private_tool_evidence,
    diagnostic_evidence_identity,
)
from elspeth.web.composer.pipeline_custody import inline_custody_audit_projection, inline_custody_manifest_redaction_input
from elspeth.web.composer.pipeline_proposal import (
    is_owned_composition_state_authority,
    owned_composition_state_review_arguments,
)
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, capture_provider_audit_source
from elspeth.web.composer.redaction import (
    MANIFEST,
    redact_arg_error_response,
    redact_failure_response,
    redact_tool_call_arguments,
    redact_tool_call_response,
)
from elspeth.web.composer.redaction_telemetry import OtelRedactionTelemetry
from elspeth.web.composer.tool_error_payloads import (
    INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
    unknown_tool_arguments_redaction,
    unknown_tool_response_redaction,
)
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operations import ComposerOperationError

INVOCATION_AUTHORITY_REVOKED_KEY: Final = "authority_revoked"

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_tool_failure import RetainedToolFailureSource


@dataclass(frozen=True, slots=True)
class RetainedOperationFailureFacts:
    durable_operation_id: str
    request_id: str | None
    context: SessionOperationContext
    source: RetainedToolFailureSource
    publication_observation: PrivateFailureFacts | None = None

    def __post_init__(self) -> None:
        from elspeth.web.sessions.continuation_tool_failure import RetainedToolFailureSource

        if (
            type(self.source) is not RetainedToolFailureSource
            or type(self.context) is not SessionOperationContext
            or type(self.durable_operation_id) is not str
            or not self.durable_operation_id
            or (self.request_id is not None and type(self.request_id) is not str)
            or (self.publication_observation is not None and type(self.publication_observation) is not PrivateFailureFacts)
        ):
            raise AuditIntegrityError("Retained operation failure facts lost their actual closed origins")

    def validate_for(self, *, durable_operation_id: str, request_id: str | None, context: SessionOperationContext) -> None:
        if (self.durable_operation_id, self.request_id, self.context) != (durable_operation_id, request_id, context):
            raise AuditIntegrityError("Retained failure publication crossed its current publisher")
        if (self.source.selected.stack.logical.session_id, self.source.selected.stack.logical.operation_id) != (
            context.fence.session_id,
            durable_operation_id,
        ):
            raise AuditIntegrityError("Retained failure publication crossed its original logical source")


@final
@dataclass(frozen=True, slots=True)
class ComposerOperationFailureFacts:
    """Immutable private failure capture bound to its original live operation."""

    durable_operation_id: str
    request_id: str | None
    context: SessionOperationContext
    facts: PrivateFailureFacts

    def __post_init__(self) -> None:
        if (
            type(self.durable_operation_id) is not str
            or not self.durable_operation_id
            or (self.request_id is not None and type(self.request_id) is not str)
            or type(self.context) is not SessionOperationContext
            or type(self.facts) is not PrivateFailureFacts
        ):
            raise AuditIntegrityError("Operation failure facts lack nominal correlation")

    def validate_for(self, *, durable_operation_id: str, request_id: str | None, context: SessionOperationContext) -> None:
        if self.durable_operation_id != durable_operation_id or self.request_id != request_id or self.context != context:
            raise AuditIntegrityError("Operation failure facts belong to another exact request or fence")


def private_operation_failure_draft(
    capture: ComposerOperationFailureFacts | RetainedOperationFailureFacts, *, failure: ComposerOperationError
) -> AuditMessageDraft:
    """Correlate private captured facts with the actually selected public outcome."""
    if type(capture) not in (ComposerOperationFailureFacts, RetainedOperationFailureFacts) or type(failure) is not ComposerOperationError:
        raise AuditIntegrityError("Operation failure projection is not nominal")
    correlation = "operation:" + capture.durable_operation_id
    if type(capture) is RetainedOperationFailureFacts:
        source = capture.source
        refusal = source.failure.refusal_source
        facts = source.invocation.private_failure
        result = source.invocation.result_canonical if refusal is not None else None if facts is None else facts.canonical
        if result is None:
            raise AuditIntegrityError("Retained failure publication lacks its actual crash/refusal facts")
        origin = {
            "kind": "retained_successful_refusal" if refusal is not None else "retained_crash",
            "response": source.response.reference.model_dump(mode="json"),
            "issue": source.issue.model_dump(mode="json"),
            "evidence": source.evidence.reference.model_dump(mode="json"),
            "invocation_call_id": source.invocation.tool_call_id,
            "refusal_kind": None if refusal is None else refusal.kind,
            "provider_call_id": None if refusal is None else refusal.provider_call_id,
            **(
                {"publication_observation": capture.publication_observation.canonical}
                if capture.publication_observation is not None
                else {}
            ),
        }
    elif isinstance(capture, ComposerOperationFailureFacts):
        result = capture.facts.canonical
        origin = None
    else:
        raise AuditIntegrityError("Operation failure facts have no actual closed origin")
    evidence = build_private_tool_evidence(
        session_id=capture.context.fence.session_id,
        operation_id=capture.context.fence.operation_id,
        operation_epoch=capture.context.fence.operation_epoch,
        evidence_id=diagnostic_evidence_identity("operation-failure", correlation),
        tool_call_id=correlation,
        tool_name="composer_failure",
        status="operation_failure",
        arguments_canonical=canonical_json(
            {
                "durable_operation_id": capture.durable_operation_id,
                "request_id": capture.request_id,
                "diagnostic_id": failure.diagnostic_id,
                "failure_code": failure.failure_code,
                "correlation_kind": "internal_operation",
                **({"retained_origin": origin} if origin is not None else {}),
            }
        ),
        result_canonical=result,
    )
    return AuditMessageDraft(
        role="audit",
        content="Private credential-filtered operation failure facts",
        tool_calls=(evidence.to_envelope(),),
        inherit_composition_state=False,
    )


def private_failure_evidence(
    facts: PrivateFailureFacts,
    *,
    identity: str,
    tool_call_id: str,
    status: str,
    context: SessionOperationContext,
) -> ComposerDiagnosticEvidence:
    return private_failure_evidence_for_source(
        facts,
        identity=identity,
        tool_call_id=tool_call_id,
        status=status,
        source=capture_provider_audit_source(context),
    )


def private_diagnostic_drafts(
    record: ComposerToolInvocation | ComposerLLMCall,
    *,
    context: SessionOperationContext,
    composition_state_id: str | None = None,
) -> tuple[AuditMessageDraft, ...]:
    return private_diagnostic_drafts_for_source(
        record,
        source=capture_provider_audit_source(context),
        composition_state_id=composition_state_id,
    )


def private_failure_evidence_for_source(
    facts: PrivateFailureFacts,
    *,
    identity: str,
    tool_call_id: str,
    status: str,
    source: ProviderAuditSource,
) -> ComposerDiagnosticEvidence:
    if type(source) is not ProviderAuditSource:
        raise AuditIntegrityError("private diagnostic source is not nominal DATA")
    if type(facts) is not PrivateFailureFacts:
        raise AuditIntegrityError("private failure facts are not nominal")
    return build_private_tool_evidence(
        session_id=source.session_id,
        operation_id=source.operation_id,
        operation_epoch=source.operation_epoch,
        tool_call_id=tool_call_id,
        evidence_id=identity,
        tool_name="composer_failure",
        status=status,
        arguments_canonical=None,
        result_canonical=facts.canonical,
    )


def private_diagnostic_drafts_for_source(
    record: ComposerToolInvocation | ComposerLLMCall,
    *,
    source: ProviderAuditSource,
    composition_state_id: str | None = None,
) -> tuple[AuditMessageDraft, ...]:
    if type(source) is not ProviderAuditSource:
        raise AuditIntegrityError("private diagnostic source is not nominal DATA")
    evidence: list[ComposerDiagnosticEvidence] = []
    if type(record) is ComposerToolInvocation:
        identity = diagnostic_evidence_identity("tool-failure", record.tool_call_id)
        call_id = record.tool_call_id
        status = "tool_failure"
    elif type(record) is ComposerLLMCall:
        call_id = record.call_id or f"legacy:{record.started_at.isoformat()}:{record.planner_call_ordinal}"
        identity = diagnostic_evidence_identity("provider-failure", call_id)
        status = "provider_failure"
        if record.private_request is not None:
            request = record.private_request
            evidence.append(
                ComposerDiagnosticEvidence(
                    session_id=source.session_id,
                    operation_id=source.operation_id,
                    operation_epoch=source.operation_epoch,
                    tool_call_id=call_id,
                    evidence_id=diagnostic_evidence_identity("provider-request", call_id),
                    tool_name="composer_provider_context",
                    status="provider_request",
                    arguments=DiagnosticPayload(
                        request.disposition,
                        "json",
                        request.canonical,
                        hashlib.sha256(request.canonical.encode("utf-8")).hexdigest() if request.canonical is not None else None,
                        request.reasons,
                    ),
                    authority_arguments=None,
                    result=DiagnosticPayload("not_returned", "json", None, None),
                    wire_arguments=DiagnosticPayload("unavailable", "wire_text", None, None, ("not_recorded",)),
                )
            )
        retained_arguments_remaining = 1024 * 1024
        for receipt in record.private_diagnostics:
            item = build_private_tool_evidence(
                session_id=source.session_id,
                operation_id=source.operation_id,
                operation_epoch=source.operation_epoch,
                tool_call_id=receipt.tool_call_id,
                evidence_id=receipt.evidence_id,
                tool_name=receipt.tool_name,
                status="received_response",
                arguments_canonical=receipt.arguments_canonical,
                result_canonical=receipt.result_canonical,
                raw_arguments=receipt.raw_arguments,
            )
            if item.arguments.content is not None:
                size = len(item.arguments.content.encode("utf-8"))
                if size > retained_arguments_remaining:
                    facts = json.loads(receipt.result_canonical)
                    facts["argument_disposition"] = "canonical_bounds"
                    item = build_private_tool_evidence(
                        session_id=source.session_id,
                        operation_id=source.operation_id,
                        operation_epoch=source.operation_epoch,
                        tool_call_id=receipt.tool_call_id,
                        evidence_id=receipt.evidence_id,
                        tool_name=receipt.tool_name,
                        status="received_response",
                        arguments_canonical=None,
                        raw_arguments=None,
                        result_canonical=canonical_json(facts),
                    )
                else:
                    retained_arguments_remaining -= size
            evidence.append(item)
    else:
        raise AuditIntegrityError("private diagnostic owner is not nominal")
    if record.private_failure is not None:
        evidence.append(
            private_failure_evidence_for_source(
                record.private_failure, identity=identity, tool_call_id=call_id, status=status, source=source
            )
        )
    return tuple(
        AuditMessageDraft(
            role="audit",
            content="Private credential-filtered diagnostic facts",
            tool_calls=(item.to_envelope(),),
            composition_state_id=composition_state_id if type(record) is ComposerToolInvocation else None,
            inherit_composition_state=type(record) is ComposerToolInvocation,
        )
        for item in evidence
    )


def _hash_canonical_payload(canonical_payload: str) -> str:
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _load_canonical_mapping(canonical_payload: str | None) -> dict[str, object] | None:
    """Decode Tier-1 canonical audit JSON, failing closed on corruption.

    Dispatch always authors a mapping through ``canonical_json``. A non-None
    payload that no longer decodes to an object is therefore corruption, not
    honest absence. Returning ``None`` would also let callers bypass manifest
    redaction and persist the damaged raw payload.
    """
    if canonical_payload is None:
        return None
    try:
        decoded = json.loads(canonical_payload)
    except json.JSONDecodeError as exc:
        raise AuditIntegrityError("Tier 1 audit anomaly: composer tool-invocation canonical payload is not valid JSON.") from exc
    if type(decoded) is not dict:
        raise AuditIntegrityError("Tier 1 audit anomaly: composer tool-invocation canonical payload must decode to a JSON object.")
    return cast(dict[str, object], decoded)


def _validated_canonical_mapping_pair(
    canonical_payload: str,
    payload_hash: str,
    *,
    label: str,
) -> dict[str, object]:
    payload = _load_canonical_mapping(canonical_payload)
    assert payload is not None
    if primitive_canonical_json(payload) != canonical_payload:
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {label} is not the exact canonical representation.")
    if _hash_canonical_payload(canonical_payload) != payload_hash:
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {label} hash mismatch.")
    return payload


def _validated_invocation_arguments(invocation: ComposerToolInvocation) -> dict[str, object]:
    """Validate both argument bindings and restore authored pipeline order."""
    arguments = _validated_canonical_mapping_pair(
        invocation.arguments_canonical,
        invocation.arguments_hash,
        label="composer tool-invocation arguments",
    )
    authority_canonical = invocation.authority_arguments_canonical
    authority_hash = invocation.authority_arguments_hash
    if invocation.tool_name != "set_pipeline":
        if authority_canonical is not None or authority_hash is not None:
            raise AuditIntegrityError("non-set_pipeline invocation carries a set-pipeline authority binding")
        return arguments
    if authority_canonical is None and authority_hash is None:
        if invocation.status is ComposerToolStatus.SUCCESS:
            raise AuditIntegrityError("successful set_pipeline invocation is missing its authority binding")
        return arguments
    if type(authority_canonical) is not str or type(authority_hash) is not str:
        raise AuditIntegrityError("set_pipeline invocation authority binding is incomplete")
    authority = _validated_canonical_mapping_pair(
        authority_canonical,
        authority_hash,
        label="set_pipeline authority arguments",
    )
    try:
        restored = restore_composer_authority_payload(authority)
    except ValueError as exc:
        raise AuditIntegrityError("set_pipeline invocation authority projection is malformed") from exc
    if primitive_canonical_json(restored) != invocation.arguments_canonical:
        raise AuditIntegrityError("set_pipeline invocation authority projection differs from its tool arguments")
    if primitive_canonical_json(project_composer_authority_payload(restored)) != authority_canonical:
        raise AuditIntegrityError("set_pipeline invocation authority projection is not reversible")
    return cast(dict[str, object], restored)


def _redacted_arguments(
    invocation: ComposerToolInvocation,
    arguments: dict[str, object],
    *,
    telemetry: OtelRedactionTelemetry,
) -> dict[str, object]:
    if invocation.status == ComposerToolStatus.ARG_ERROR:
        arg_error_projection = redact_arg_error_response(
            error_class=invocation.error_class,
            error_category=invocation.error_category,
            error_message=None,
        )
        return {
            "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
            "error_class": arg_error_projection["error_class"],
            "field_count": len(arguments),
        }
    if invocation.tool_name not in MANIFEST:
        return dict(unknown_tool_arguments_redaction(telemetry=telemetry))
    review_arguments = (
        owned_composition_state_review_arguments(arguments)
        if invocation.tool_name == "set_pipeline" and is_owned_composition_state_authority(arguments)
        else arguments
    )
    redaction_input = (
        inline_custody_manifest_redaction_input(review_arguments) if invocation.tool_name == "set_pipeline" else review_arguments
    )
    custody_projection = redaction_input != review_arguments
    try:
        redacted = redact_tool_call_arguments(
            invocation.tool_name,
            cast(dict[str, Any], redaction_input),
            telemetry=telemetry,
        )
    except PydanticValidationError:
        if invocation.status not in {ComposerToolStatus.PLUGIN_CRASH, ComposerToolStatus.CANCELLED}:
            raise
        failure_projection = redact_failure_response(
            status=invocation.status.value,
            error_class=invocation.error_class,
            error_message=invocation.error_message,
        )
        redacted = {
            "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
            "error_class": failure_projection["error_class"],
            "field_count": len(arguments),
        }
    return dict(inline_custody_audit_projection(redacted)) if custody_projection else redacted


def _redacted_result_canonical(
    invocation: ComposerToolInvocation,
    *,
    telemetry: OtelRedactionTelemetry,
) -> str | None:
    result = _load_canonical_mapping(invocation.result_canonical)
    if invocation.status in {ComposerToolStatus.PLUGIN_CRASH, ComposerToolStatus.CANCELLED}:
        return None
    if invocation.tool_name not in MANIFEST:
        return canonical_json(unknown_tool_response_redaction()) if result is not None else None
    if result is None:
        return invocation.result_canonical
    if invocation.status == ComposerToolStatus.ARG_ERROR:
        return canonical_json(
            redact_arg_error_response(
                error_class=invocation.error_class,
                error_category=invocation.error_category,
                error_message=invocation.error_message,
                result=result,
            )
        )
    return canonical_json(
        redact_tool_call_response(
            invocation.tool_name,
            result,
            telemetry=telemetry,
        )
    )


def redacted_tool_invocation_content_and_envelope(
    invocation: ComposerToolInvocation,
    *,
    authority_revoked: bool = False,
) -> tuple[str, dict[str, object]]:
    """Return the one manifest-redacted projection allowed in chat storage.

    ``authority_revoked`` marks an invocation whose result P4 refused after it
    ran. Its record stays exact (it did run and returned this result); the
    marker says the result was not the turn's outcome, so history must never
    replay it as one.

    ``ComposerToolInvocation`` retains exact in-memory arguments/results for
    convergence and audit binding. Chat storage is a separate persistence
    boundary: hashes are recomputed over the redacted canonical payloads, and
    the canonical pipeline executor hash is carried forward explicitly so
    later proposal settlement can still verify the exact execution result.
    """

    telemetry = OtelRedactionTelemetry()
    arguments = _validated_invocation_arguments(invocation)
    redacted_arguments = _redacted_arguments(invocation, arguments, telemetry=telemetry)
    arguments_canonical = canonical_json(redacted_arguments)
    result_canonical = _redacted_result_canonical(invocation, telemetry=telemetry)
    arg_error_projection = (
        redact_arg_error_response(
            error_class=invocation.error_class,
            error_category=invocation.error_category,
            error_message=invocation.error_message,
            result=_load_canonical_mapping(invocation.result_canonical),
        )
        if invocation.status == ComposerToolStatus.ARG_ERROR
        else None
    )
    failure_projection = (
        redact_failure_response(
            status=invocation.status.value,
            error_class=invocation.error_class,
            error_message=invocation.error_message,
        )
        if invocation.status in {ComposerToolStatus.PLUGIN_CRASH, ComposerToolStatus.CANCELLED}
        or (invocation.status is ComposerToolStatus.SUCCESS and result_canonical is None)
        else None
    )
    if invocation.tool_name == "set_pipeline" and invocation.status is ComposerToolStatus.SUCCESS:
        raw_result = _load_canonical_mapping(invocation.result_canonical)
        if raw_result is not None and ("pipeline_content_hash_schema" in raw_result or "pipeline_content_hash" in raw_result):
            if (
                "pipeline_content_hash_schema" not in raw_result
                or raw_result["pipeline_content_hash_schema"] != "composer.pipeline-dispatch-result.v1"
            ):
                raise AuditIntegrityError("pipeline dispatch executor-content schema is malformed")
            if "pipeline_content_hash" not in raw_result:
                raise AuditIntegrityError("pipeline dispatch content hash is malformed")
            executor_content_hash = raw_result["pipeline_content_hash"]
            if not is_lower_sha256_hex(executor_content_hash):
                raise AuditIntegrityError("pipeline dispatch content hash is malformed")
            redacted_result = _load_canonical_mapping(result_canonical)
            if redacted_result is None:
                raise AuditIntegrityError("pipeline dispatch redacted result is missing")
            redacted_result["pipeline_content_hash_schema"] = "composer.pipeline-dispatch-result.v1"
            redacted_result["pipeline_content_hash"] = executor_content_hash
            result_canonical = canonical_json(redacted_result)

    invocation_payload: dict[str, Any] = invocation.to_dict()
    invocation_payload["arguments_canonical"] = arguments_canonical
    invocation_payload["arguments_hash"] = _hash_canonical_payload(arguments_canonical)
    if invocation.tool_name == "set_pipeline":
        authority_arguments_canonical = composer_authority_canonical_json(redacted_arguments)
        invocation_payload["authority_arguments_canonical"] = authority_arguments_canonical
        invocation_payload["authority_arguments_hash"] = _hash_canonical_payload(authority_arguments_canonical)
    invocation_payload["result_canonical"] = result_canonical
    invocation_payload["result_hash"] = _hash_canonical_payload(result_canonical) if result_canonical is not None else None
    if arg_error_projection is not None:
        invocation_payload["error_class"] = arg_error_projection["error_class"]
        invocation_payload["error_message"] = arg_error_projection["error_message"]
    elif failure_projection is not None:
        invocation_payload["error_class"] = failure_projection["error_class"]
        invocation_payload["error_message"] = failure_projection["error_message"]

    if arg_error_projection is not None:
        content = canonical_json(arg_error_projection)
    elif failure_projection is not None:
        content = canonical_json(failure_projection)
    elif result_canonical is not None:
        content = result_canonical
    else:
        content = canonical_json(
            redact_failure_response(
                status="failure",
                error_class=invocation.error_class,
                error_message=invocation.error_message,
            )
        )
    if authority_revoked:
        return content, {"_kind": "audit", "invocation": invocation_payload, INVOCATION_AUTHORITY_REVOKED_KEY: True}
    return content, {"_kind": "audit", "invocation": invocation_payload}


def private_tool_invocation_draft(
    invocation: ComposerToolInvocation,
    *,
    session_id: str,
    session_operation_context: SessionOperationContext,
    composition_state_id: str | None = None,
) -> AuditMessageDraft:
    """Keep credential-filtered authoring beside its display in one cohort."""
    from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence

    _validated_invocation_arguments(invocation)
    if invocation.result_canonical is not None:
        if invocation.result_hash is None:
            raise AuditIntegrityError("private tool evidence result has no binding")
        _validated_canonical_mapping_pair(invocation.result_canonical, invocation.result_hash, label="private tool result")
    fence = session_operation_context.fence
    if fence.session_id != session_id:
        raise AuditIntegrityError("private tool evidence belongs to another session")
    authoring = invocation.private_authoring
    evidence = build_private_tool_evidence(
        session_id=session_id,
        operation_id=fence.operation_id,
        operation_epoch=fence.operation_epoch,
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        status=invocation.status.value,
        arguments_canonical=authoring.arguments_canonical if authoring is not None else invocation.arguments_canonical,
        authority_arguments_canonical=(
            authoring.authority_arguments_canonical if authoring is not None else invocation.authority_arguments_canonical
        ),
        raw_arguments=authoring.raw_arguments if authoring is not None else None,
        result_canonical=invocation.result_canonical,
        error_category=invocation.error_category.value if invocation.error_category is not None else None,
    )
    return AuditMessageDraft(
        role="audit",
        content="Private credential-filtered tool evidence",
        tool_calls=(evidence.to_envelope(),),
        composition_state_id=composition_state_id,
    )


__all__ = [
    "INVOCATION_AUTHORITY_REVOKED_KEY",
    "private_tool_invocation_draft",
    "redacted_tool_invocation_content_and_envelope",
]
