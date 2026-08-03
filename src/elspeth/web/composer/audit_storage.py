"""Redacted storage projection for composer tool-invocation evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json as primitive_canonical_json
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.authority_hashing import (
    composer_authority_canonical_json,
    project_composer_authority_payload,
    restore_composer_authority_payload,
)
from elspeth.web.composer.pipeline_proposal import (
    is_owned_composition_state_authority,
    owned_composition_state_review_arguments,
)
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
    try:
        redacted = redact_tool_call_arguments(
            invocation.tool_name,
            cast(dict[str, Any], review_arguments),
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
    return redacted


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
) -> tuple[str, dict[str, object]]:
    """Return the one manifest-redacted projection allowed in chat storage.

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
            if raw_result.get("pipeline_content_hash_schema") != "composer.pipeline-dispatch-result.v1":
                raise AuditIntegrityError("pipeline dispatch executor-content schema is malformed")
            executor_content_hash = raw_result.get("pipeline_content_hash")
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
    return content, {"_kind": "audit", "invocation": invocation_payload}


__all__ = ["redacted_tool_invocation_content_and_envelope"]
