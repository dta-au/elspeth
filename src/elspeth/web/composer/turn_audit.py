"""Redact and persist one Composer tool-call turn (P4)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedToolCall,
    _PersistOutcome,
    _ToolOutcome,
)
from elspeth.web.composer.bounded_json import bounded_json_loads
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.redaction_telemetry import RedactionTelemetry
from elspeth.web.composer.tool_error_payloads import (
    INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
    unknown_tool_arguments_redaction,
)
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.interpretation_state import pending_execution_interpretation_sites
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions._persist_payload import AuditOutcome, RedactedToolRow, RejectionRecord
from elspeth.web.sessions.protocol import SessionServiceProtocol


def _serialize_response_via_walker(
    outcome: _ToolOutcome,
    *,
    telemetry: Any,
    failure_status: ComposerToolStatus | None = None,
) -> str:
    """Serialize one Step 1 outcome through the redaction response walker."""

    # Keep redaction imports local to this cold-path helper.
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.redaction import (
        MANIFEST,
        redact_arg_error_response,
        redact_failure_response,
        redact_tool_call_response,
    )
    from elspeth.web.composer.tool_error_payloads import unknown_tool_response_redaction

    if outcome.error_class is None:
        response = outcome.response
        # ``response`` is the closed sum type ``ToolResult | Mapping | None``
        # (see ``_ToolOutcome``). The ``None`` arm is the error path and is
        # already excluded here by the enclosing ``error_class is None`` guard
        # (handled by the final error-envelope return below). The two live arms
        # come from distinct producers — a ``Mapping`` is the serialized
        # ``request_advisor_hint`` envelope built outside ``execute_tool``; a
        # ``ToolResult`` is every other path — so this ``isinstance`` is union
        # dispatch between real producer variants, not a defensive shape-guard
        # on a single guaranteed type, and the variants are not interchangeable
        # (Mapping → deep_thaw, ToolResult → to_dict).
        if isinstance(response, Mapping):
            response_payload = deep_thaw(response)
        else:
            result = cast(ToolResult, response)
            response_payload = result.to_dict()
        if outcome.call.function.name not in MANIFEST:
            return canonical_json(unknown_tool_response_redaction())
        redacted = redact_tool_call_response(
            tool_name=outcome.call.function.name,
            response=response_payload,
            telemetry=telemetry,
        )
        return canonical_json(redacted)
    status = ComposerToolStatus.ARG_ERROR if failure_status is None else failure_status
    if status is not ComposerToolStatus.ARG_ERROR:
        return canonical_json(
            redact_failure_response(
                status=status.value,
                error_class=outcome.error_class,
                error_message=outcome.error_message,
            )
        )
    if outcome.error_category is None:
        raise AuditIntegrityError("ARG_ERROR tool outcome carries no error_category")
    return canonical_json(
        redact_arg_error_response(
            error_class=outcome.error_class,
            error_category=outcome.error_category,
            error_message=outcome.error_message,
        )
    )


def _state_payload_for_compose_turn(
    response: Any,
    *,
    ingress: CompositionIngressRecord | None = None,
    chat_ingress_inputs: list[ChatIngressInput] | None = None,
) -> Any:
    """Build a StatePayload for the current interim Step 2 redacted row.

    The persisted ``is_valid`` here is the AUTHORING-ONLY lane: Stage-1
    ``validate()`` (no plugin config instantiation, no runtime preflight)
    narrowed by :func:`pending_execution_interpretation_sites` — a state
    still carrying mandatory interpretation reviews must not persist
    ``is_valid=True`` while the strict turn-end writer would refuse it
    over the same content (elspeth-67c6fa691d; two writers, one column).
    The strict lane stays with the turn-end writer
    (``_composition_state_data_for_persist``); ``composer_meta``'s
    ``validation_lane`` marker records which predicate produced each row.
    The TOOL-RESULT validation surface deliberately keeps the bare
    Stage-1 verdict — it drives the planner repair loop and is not
    persisted here.
    """

    from elspeth.web.sessions._persist_payload import StatePayload
    from elspeth.web.sessions.protocol import CompositionStateData, CompositionValidationError

    result = cast(ToolResult, response)
    state_d = result.updated_state.to_dict()
    pending_sites = pending_execution_interpretation_sites(result.updated_state)
    validation_errors = tuple(
        CompositionValidationError(message=error.message, error_code=error.error_code, component=error.component)
        for error in result.validation.errors
    )
    if pending_sites:
        # Component id + kind only: user_term is user/planner-authored
        # content and stays out of the persisted error records (same
        # non-content rule as the runtime placeholder telemetry).
        validation_errors += tuple(
            CompositionValidationError(message=site.kind.value, error_code="interpretation_review_pending", component=site.component_id)
            for site in pending_sites
        )
    return StatePayload(
        data=CompositionStateData(
            sources=state_d["sources"],
            nodes=state_d["nodes"],
            edges=state_d["edges"],
            outputs=state_d["outputs"],
            metadata_=state_d["metadata"],
            is_valid=result.validation.is_valid and not pending_sites,
            validation_errors=validation_errors,
            composer_meta={
                "validation_lane": "authoring_only",
                **({"ingress": ingress} if ingress is not None else {}),
                **({"chat_ingress_inputs": chat_ingress_inputs} if chat_ingress_inputs is not None else {}),
            },
        ),
        # persist_compose_turn inserts composition state rows under
        # the session write lock and re-derives
        # lineage from per-session version ordering when this is None
        # (spec §5.7.1). The async loop deliberately does not fabricate a
        # predecessor id for a row that has not been allocated yet.
        derived_from_state_id=None,
    )


def build_rejection_records(tool_outcomes: tuple[_ToolOutcome, ...]) -> tuple[RejectionRecord, ...]:
    """Extract one durable rejection record per refused mutation
    (elspeth-3e28029d2f).

    The chat ``tool`` row persists REDACTED; this record carries the exact
    payload the planner saw — the text and the reasoning — for the session
    store (operator ruling 2026-09-02: session data, not Landscape data).

    Three outcome shapes (see ``_ToolOutcome``): failure envelopes with
    ``error_class`` set (argument errors, plugin crashes); ``ToolResult``
    with ``success=False`` (validation rejections); everything else —
    successes and advisor mapping envelopes — records nothing.
    """
    records: list[RejectionRecord] = []
    for outcome in tool_outcomes:
        tool_name = outcome.call.function.name
        if outcome.error_class is not None:
            records.append(
                RejectionRecord(
                    tool_call_id=outcome.call.id,
                    tool_name=tool_name,
                    error_code=outcome.error_class,
                    message=outcome.error_message or "",
                    planner_payload=json.dumps(
                        {
                            "error_class": outcome.error_class,
                            "error_message": outcome.error_message,
                        }
                    ),
                )
            )
        elif isinstance(outcome.response, ToolResult) and not outcome.response.success:
            errors = outcome.response.validation.errors
            primary = next((entry for entry in errors if entry.error_code), errors[0] if errors else None)
            records.append(
                RejectionRecord(
                    tool_call_id=outcome.call.id,
                    tool_name=tool_name,
                    error_code=primary.error_code if primary is not None else None,
                    message=primary.message if primary is not None else "",
                    planner_payload=serialize_tool_result(outcome.response),
                )
            )
    return tuple(records)


if TYPE_CHECKING:
    from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord


async def persist_turn_audit(
    *,
    sessions_service: SessionServiceProtocol | None,
    redaction_telemetry: RedactionTelemetry,
    tool_outcomes: tuple[_ToolOutcome, ...],
    decoded_args_by_call_id: Mapping[str, Mapping[str, Any]],
    assistant_message: _AdmittedAssistantMessage,
    raw_assistant_content: str | None,
    assistant_tool_calls: tuple[_AdmittedToolCall, ...],
    crash_pending: bool,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None,
    current_state_id: str | None,
    persisted_tool_call_turn: bool,
    persisted_assistant_message_id: str | None,
    persisted_assistant_content: str | None,
    assistant_row_uses_current_dispatch: bool,
    ingress: CompositionIngressRecord | None = None,
    chat_ingress_inputs: list[ChatIngressInput] | None = None,
    required_work: RequiredWorkBinding | None = None,
) -> _PersistOutcome:
    """Phase P4 of the compose loop — redact then persist the turn audit.

    Two sub-steps that share an invariant (a mid-step raise must leave
    the DB in its pre-step shape):

    1. **Redaction (pure / async).** Walks ``tool_outcomes`` via the
       redaction manifest and builds the immutable
       ``redacted_assistant_tool_calls`` / ``redacted_tool_rows`` shapes
       the persister expects.
    2. **Persistence.** Calls
       ``sessions_service.persist_compose_turn_async`` exactly once
       when ``session_id`` is set. The AuditIntegrityError catch
       stamps ``failed_turn`` with ``tool_responses_persisted=0`` and
       re-raises so the route handler sees the partial-write story.
       Unwind-audit invariants are checked after the persist returns;
       failures raise additional AuditIntegrityError(s).

    Crash propagation is intentionally *not* in this helper. P3 supplies a
    discriminated carrier (plugin crash or first-party advisor failure), and
    the driver raises it only after this phase publishes the closed row.

    ``assistant_row_uses_current_dispatch`` is the caller-owned P4
    disposition for whether ``assistant_message`` still contains the current
    dispatch's model prose. It is required because the caller performs the
    advisor-repair substitution; row presence cannot recover that fact.
    """
    from pydantic import ValidationError as PydanticValidationError

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.pipeline_custody import (
        inline_custody_audit_projection,
        inline_custody_manifest_redaction_input,
    )
    from elspeth.web.composer.redaction import (
        MANIFEST,
        redact_arg_error_response,
        redact_failure_response,
        redact_tool_call_arguments,
    )

    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...] = ()
    for index, tool_outcome in enumerate(tool_outcomes):
        tc = tool_outcome.call
        decoded_args: dict[str, Any]
        persisted_arguments: Mapping[str, Any]
        if tc.id in decoded_args_by_call_id:
            # deep_thaw restores plain dict/list types from any
            # MappingProxyType / tuple introduced by the carrier's
            # freeze_fields contract. redact_tool_call_arguments has a
            # ``dict[str, Any]`` signature and json.dumps cannot serialise
            # MappingProxyType, so the thaw here is load-bearing once
            # _DispatchOutcome carries frozen args.
            decoded_args = deep_thaw(decoded_args_by_call_id[tc.id])
        elif tool_outcome.error_class is not None:
            try:
                decoded_json = bounded_json_loads(
                    tc.function.arguments,
                    label="composer tool-call arguments",
                )
            except (TypeError, ValueError):
                decoded_args = {
                    "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                    "error_class": tool_outcome.error_class,
                }
            else:
                decoded_args = (
                    # bounded_json_loads forwards to json.loads with no
                    # object_hook and no object_pairs_hook at this call site,
                    # so a decoded object is always an exact dict, never a
                    # subclass.
                    {"_decoded_non_object": decoded_json}
                    if type(decoded_json) is not dict
                    else {
                        "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                        "error_class": tool_outcome.error_class,
                    }
                )
        else:
            decoded_args = {"_raw_arguments": tc.function.arguments}
        is_arg_error = tool_outcome.error_class is not None and not (crash_pending and index == len(tool_outcomes) - 1)
        if is_arg_error:
            arg_error_projection = redact_arg_error_response(
                error_class=tool_outcome.error_class,
                error_category=tool_outcome.error_category,
                error_message=None,
            )
            persisted_arguments = {
                "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                "error_class": arg_error_projection["error_class"],
                "field_count": len(decoded_args),
            }
        elif tc.function.name in MANIFEST:
            try:
                redaction_input = (
                    inline_custody_manifest_redaction_input(decoded_args) if tc.function.name == "set_pipeline" else decoded_args
                )
                persisted_arguments = redact_tool_call_arguments(
                    tc.function.name,
                    redaction_input,
                    telemetry=redaction_telemetry,
                )
                if tc.function.name == "set_pipeline":
                    persisted_arguments = cast(
                        dict[str, Any],
                        inline_custody_audit_projection(persisted_arguments),
                    )
            except PydanticValidationError:
                if tool_outcome.error_class is None:
                    raise
                failure_projection = redact_failure_response(
                    status=ComposerToolStatus.PLUGIN_CRASH.value,
                    error_class=tool_outcome.error_class,
                    error_message=tool_outcome.error_message,
                )
                persisted_arguments = {
                    "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                    "error_class": failure_projection["error_class"],
                    "field_count": len(decoded_args),
                }
        else:
            persisted_arguments = unknown_tool_arguments_redaction(telemetry=redaction_telemetry)
        redacted_assistant_tool_calls = (
            *redacted_assistant_tool_calls,
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": json.dumps(persisted_arguments),
                },
                # The call's wire facts (D1): which strict key was sent for the
                # tool and whether its raw arguments conformed to the W they
                # were sent under. Always present; null when not applicable.
                "strict_sent": tool_outcome.strict_sent,
                "wire_conformant": tool_outcome.wire_conformant,
            },
        )
    redacted_tool_rows = tuple(
        RedactedToolRow(
            tool_call_id=tool_outcome.call.id,
            content=_serialize_response_via_walker(
                tool_outcome,
                telemetry=redaction_telemetry,
                failure_status=(
                    None
                    if tool_outcome.error_class is None
                    else (
                        ComposerToolStatus.PLUGIN_CRASH
                        if crash_pending and index == len(tool_outcomes) - 1
                        else ComposerToolStatus.ARG_ERROR
                    )
                ),
            ),
            composition_state_payload=(
                _state_payload_for_compose_turn(tool_outcome.response, ingress=ingress, chat_ingress_inputs=chat_ingress_inputs)
                if tool_outcome.post_version > tool_outcome.pre_version
                else None
            ),
        )
        for index, tool_outcome in enumerate(tool_outcomes)
    )
    failed_turn: FailedTurnMetadata | None = None
    unwind_audit_failed = False
    audit_outcome: AuditOutcome | None = None
    if session_id is not None:
        if type(session_operation_context) is not SessionOperationContext:
            raise AuditIntegrityError("Compose turn audit persistence requires exact session operation authority")
        if sessions_service is None:
            raise RuntimeError("sessions_service not wired")
        checkpoint_sql = checkpoint_projection = None
        if required_work is not None:
            if type(required_work) is not RequiredWorkBinding:
                raise AuditIntegrityError("Compose checkpoint requires a nominal required-work binding")
            required_work.validate_context(session_operation_context)
            checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
                RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
            )
        try:
            if checkpoint_sql is None:
                audit_outcome = await sessions_service.persist_compose_turn_async(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    rejection_records=build_rejection_records(tool_outcomes),
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    writer_principal="compose_loop",
                    plugin_crash_pending=crash_pending,
                    session_operation_context=session_operation_context,
                )
            else:
                audit_outcome = await sessions_service.persist_compose_turn_async(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    rejection_records=build_rejection_records(tool_outcomes),
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    writer_principal="compose_loop",
                    plugin_crash_pending=crash_pending,
                    session_operation_context=session_operation_context,
                    required_work=checkpoint_sql,
                )
        except AuditIntegrityError as exc:
            if checkpoint_projection is not None and checkpoint_sql is not None and checkpoint_sql.complete:
                checkpoint_projection.complete_without_submission()
            exc.failed_turn = FailedTurnMetadata(
                assistant_message_id=None,
                tool_calls_attempted=len(assistant_tool_calls),
                tool_responses_persisted=0,
            )
            raise
        except BaseException:
            if checkpoint_projection is not None and checkpoint_sql is not None and checkpoint_sql.complete:
                checkpoint_projection.complete_without_submission()
            raise
        if checkpoint_projection is not None:
            checkpoint_projection.begin_projection()
            if type(audit_outcome) is not AuditOutcome:
                failure = AuditIntegrityError("Compose checkpoint returned a foreign audit outcome")
                checkpoint_projection.complete_owned(failure)
                raise failure
            checkpoint_projection.complete_owned()
        unwind_audit_failed = audit_outcome.unwind_audit_failed
        current_state_id = audit_outcome.current_state_id
        failed_turn = FailedTurnMetadata(
            assistant_message_id=audit_outcome.assistant_id,
            tool_calls_attempted=len(assistant_tool_calls),
            tool_responses_persisted=0 if audit_outcome.assistant_id is None else len(redacted_tool_rows),
        )
        if audit_outcome.assistant_id is None and not crash_pending:
            raise AuditIntegrityError(
                "persist_compose_turn_async returned unwind_audit_failed without an in-flight plugin crash",
                failed_turn=failed_turn,
            )
        if audit_outcome.assistant_id is None and not audit_outcome.unwind_audit_failed:
            raise AuditIntegrityError(
                "persist_compose_turn_async returned no assistant id without the unwind-audit-failed disposition",
                failed_turn=failed_turn,
            )
        persisted_assistant_message_id = audit_outcome.assistant_id
        # Derived here rather than by the caller because this is the only
        # frame that knows what reached the row: the persist call above sends
        # ``assistant_message.content or ""``, and ``assistant_message`` is
        # already the substituted message on the advisor-repair branch. A
        # caller reconstructing it from the turn prose would record the wrong
        # bytes for that branch (elspeth-d581b3da7f). Rolled back (no
        # assistant id) means no row holds anything — the pair goes to None
        # together.
        persisted_assistant_content = None if audit_outcome.assistant_id is None else (assistant_message.content or "")
        # The unwind-failure outcome means the transaction rolled back: no
        # assistant or tool row survived, so the driver must retain the
        # in-flight invocation evidence on the propagated plugin crash.
        persisted_tool_call_turn = audit_outcome.assistant_id is not None
    return _PersistOutcome(
        current_state_id=current_state_id,
        persisted_assistant_message_id=persisted_assistant_message_id,
        persisted_assistant_content=persisted_assistant_content,
        persisted_tool_call_turn=persisted_tool_call_turn,
        persisted_assistant_matches_current_dispatch=(persisted_assistant_message_id is not None and assistant_row_uses_current_dispatch),
        unwind_audit_failed=unwind_audit_failed,
        failed_turn=failed_turn,
        redacted_assistant_tool_calls=redacted_assistant_tool_calls,
        redacted_tool_rows=redacted_tool_rows,
        audit_outcome=audit_outcome,
    )
