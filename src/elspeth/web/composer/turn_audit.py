"""Redact and persist one Composer tool-call turn (P4)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateToolAuthoring
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
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
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_error_payloads import (
    INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
    unknown_tool_arguments_redaction,
)
from elspeth.web.composer.tools._common import ToolResult, _failure_result
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.interpretation_state import pending_execution_interpretation_sites
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions._persist_payload import AuditOutcome, RedactedToolRow, RejectionRecord
from elspeth.web.sessions.protocol import SessionServiceProtocol

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4


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
            if primary is not None and primary.error_code == "auto_mutation_revoked":
                # This terminal refusal is persisted as the real tool result.
                # No subsequent model call receives planner feedback for it.
                continue
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


def _revoked_graph_rows(
    *,
    rows: tuple[RedactedToolRow, ...],
    outcomes: tuple[_ToolOutcome, ...],
    base_state: CompositionState,
    redaction_telemetry: RedactionTelemetry,
) -> tuple[RedactedToolRow, ...]:
    if tuple(row.tool_call_id for row in rows) != tuple(outcome.call.id for outcome in outcomes):
        raise AuditIntegrityError("Refusal preparation lost its actual ordered tool cohort")
    if not any(row.composition_state_payload is not None for row in rows):
        return rows
    result: list[RedactedToolRow] = []
    for row, outcome in zip(rows, outcomes, strict=True):
        if row.composition_state_payload is None:
            result.append(row)
            continue
        if row.visible_event is None:
            raise AuditIntegrityError("Graph refusal lost its original visible tool event")
        failed = _failure_result(
            base_state,
            "This candidate graph was not saved because automatic mutation authority was revoked.",
            error_code="auto_mutation_revoked",
            with_state_validation=False,
        )
        display = _serialize_response_via_walker(
            replace(outcome, response=failed, post_version=outcome.pre_version),
            telemetry=redaction_telemetry,
            failure_status=None,
        )
        result.append(
            replace(
                row, content=display, composition_state_payload=None, visible_event=row.visible_event.with_authority_revocation(display)
            )
        )
    return tuple(result)


if TYPE_CHECKING:
    from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord


async def persist_turn_audit(
    *,
    sessions_service: SessionServiceProtocol | None,
    redaction_telemetry: RedactionTelemetry,
    tool_outcomes: tuple[_ToolOutcome, ...],
    mutation_ceiling: ComposerMutationCeiling | None,
    authority_revocation: ComposerMutationRevocation | None,
    base_state: CompositionState,
    decoded_args_by_call_id: Mapping[str, Mapping[str, Any]],
    private_authorings: tuple[PrivateToolAuthoring, ...],
    private_invocations: tuple[ComposerToolInvocation, ...],
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
    turn_evidence: TurnEvidence | None = None,
    continuation: ContinuationLiveP4 | None = None,
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

    def visible_event_for(outcome: _ToolOutcome, display_result: str) -> VisibleToolEvent | None:
        if session_operation_context is None:
            return None
        authoring = next((item for item in private_authorings if item.tool_call_id == outcome.call.id), None)
        received, effective, selected = (
            turn_evidence.transcript.current_tool_strings(outcome.call.id, outcome.call.function.name)
            if turn_evidence is not None
            else (None, None, None)
        )
        display_call = next(item for item in redacted_assistant_tool_calls if item["id"] == outcome.call.id)
        return VisibleToolEvent.capture(
            context=session_operation_context,
            request_id=turn_evidence.request_id if turn_evidence is not None else None,
            tool_call_id=outcome.call.id,
            tool_name=outcome.call.function.name,
            received_arguments=received if received is not None else authoring.raw_arguments if authoring is not None else None,
            effective_arguments=effective if effective is not None else outcome.call.function.arguments,
            selected_result=selected,
            display_arguments=display_call["function"]["arguments"],
            display_result=display_result,
        )

    redacted_rows: list[RedactedToolRow] = []
    for index, tool_outcome in enumerate(tool_outcomes):
        display_result = _serialize_response_via_walker(
            tool_outcome,
            telemetry=redaction_telemetry,
            failure_status=(
                None
                if tool_outcome.error_class is None
                else (
                    ComposerToolStatus.PLUGIN_CRASH if crash_pending and index == len(tool_outcomes) - 1 else ComposerToolStatus.ARG_ERROR
                )
            ),
        )
        redacted_rows.append(
            RedactedToolRow(
                tool_call_id=tool_outcome.call.id,
                content=display_result,
                composition_state_payload=(
                    _state_payload_for_compose_turn(tool_outcome.response, ingress=ingress, chat_ingress_inputs=chat_ingress_inputs)
                    if tool_outcome.post_version > tool_outcome.pre_version
                    else None
                ),
                visible_event=visible_event_for(tool_outcome, display_result),
            )
        )
    redacted_tool_rows = tuple(redacted_rows)
    revoked_tool_rows = (
        _revoked_graph_rows(
            rows=redacted_tool_rows,
            outcomes=tool_outcomes,
            base_state=base_state,
            redaction_telemetry=redaction_telemetry,
        )
        if session_id is not None
        else redacted_tool_rows
    )
    failed_turn: FailedTurnMetadata | None = None
    unwind_audit_failed = False
    checkpoint_projection = None
    effect_verification_failure = None
    audit_outcome: AuditOutcome | None = None
    effects_before = turn_evidence.effects if turn_evidence is not None else None
    if session_id is not None:
        if type(session_operation_context) is not SessionOperationContext:
            raise AuditIntegrityError("Compose turn audit persistence requires exact session operation authority")
        if sessions_service is None:
            raise RuntimeError("sessions_service not wired")
        from elspeth.web.composer.audit_storage import _validated_canonical_mapping_pair, private_failure_evidence
        from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity

        authorings = {authoring.tool_call_id: authoring for authoring in private_authorings}
        invocations = {invocation.tool_call_id: invocation for invocation in private_invocations}
        expected_ids = {outcome.call.id for outcome in tool_outcomes}
        if (
            set(authorings) != expected_ids
            or len(authorings) != len(private_authorings)
            or set(invocations) != expected_ids
            or len(invocations) != len(private_invocations)
        ):
            raise AuditIntegrityError("private tool checkpoint lost its exact authoring or invocation set")
        private_tool_evidence = []
        for outcome in tool_outcomes:
            authoring = authorings[outcome.call.id]
            invocation = invocations[outcome.call.id]
            if invocation.tool_name != outcome.call.function.name:
                raise AuditIntegrityError("private tool checkpoint invocation names another tool")
            if invocation.private_authoring is not None and invocation.private_authoring != authoring:
                raise AuditIntegrityError("private tool checkpoint receipt differs from its invocation")
            if invocation.result_canonical is not None:
                if invocation.result_hash is None:
                    raise AuditIntegrityError("private tool checkpoint result has no binding")
                _validated_canonical_mapping_pair(invocation.result_canonical, invocation.result_hash, label="private checkpoint result")
            elif invocation.result_hash is not None:
                raise AuditIntegrityError("private tool checkpoint absent result has a hash")
            private_tool_evidence.append(
                build_private_tool_evidence(
                    session_id=session_id,
                    operation_id=session_operation_context.fence.operation_id,
                    operation_epoch=session_operation_context.fence.operation_epoch,
                    tool_call_id=outcome.call.id,
                    tool_name=outcome.call.function.name,
                    status=invocation.status.value,
                    arguments_canonical=authoring.arguments_canonical,
                    authority_arguments_canonical=authoring.authority_arguments_canonical,
                    raw_arguments=authoring.raw_arguments,
                    result_canonical=invocation.result_canonical,
                    error_category=outcome.error_category.value if outcome.error_category is not None else None,
                )
            )
        private_tool_failures = tuple(
            private_failure_evidence(
                invocation.private_failure,
                identity=diagnostic_evidence_identity("tool-failure", invocation.tool_call_id),
                tool_call_id=invocation.tool_call_id,
                status="tool_failure",
                context=session_operation_context,
            )
            for invocation in private_invocations
            if invocation.private_failure is not None
        )
        checkpoint_sql = checkpoint_projection = None
        if required_work is not None:
            if type(required_work) is not RequiredWorkBinding:
                raise AuditIntegrityError("Compose checkpoint requires a nominal required-work binding")
            required_work.validate_context(session_operation_context)
            if continuation is None:
                checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
                    RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
                )
        try:
            if continuation is not None:
                from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
                from elspeth.web.composer.continuation_prepared_p4 import capture_prepared_p4

                if (
                    type(continuation) is not ContinuationLiveP4
                    or continuation.work is not required_work
                    or continuation.inventory.running.session_operation_context is not session_operation_context
                    or type(required_work) is not RequiredWorkBinding
                ):
                    raise AuditIntegrityError("V2 P4 publication changed its retained current phase/ticket owners")
                prepared = capture_prepared_p4(
                    context=session_operation_context,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_calls=redacted_assistant_tool_calls,
                    redacted_rows=redacted_tool_rows,
                    revoked_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
                    rejection_records=build_rejection_records(tool_outcomes),
                    withheld_replies=continuation.withheld_replies,
                    invocations=private_invocations,
                    authorings=private_authorings,
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    assistant_row_uses_current_dispatch=assistant_row_uses_current_dispatch,
                    crash_pending=crash_pending,
                    env_ref_names=continuation.inventory.names,
                )
                commit = await continuation.prepare(prepared)
                checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
                    RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
                )
                try:
                    continuation.bind_checkpoint_ticket(checkpoint_sql)
                except BaseException as original:
                    # This exact local reservation has not reached the physical
                    # service call. Preserve its real no-submission failure;
                    # the existing outer handler closes the paired projection.
                    try:
                        checkpoint_sql.complete_without_submission(original)
                    except BaseException as cleanup:
                        if cleanup is original:
                            raise
                        raise BaseExceptionGroup("P4 checkpoint binding and no-submission cleanup", (original, cleanup)) from None
                    raise
                physical = await sessions_service.persist_continuation_compose_turn(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    revoked_tool_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
                    rejection_records=build_rejection_records(tool_outcomes),
                    parent_composition_state_id=current_state_id,
                    expected_current_state_id=current_state_id,
                    writer_principal="compose_loop",
                    plugin_crash_pending=crash_pending,
                    session_operation_context=session_operation_context,
                    continuation=commit,
                    continuation_prepared=prepared,
                    continuation_env_ref_names=continuation.inventory.names,
                    required_work=checkpoint_sql,
                    continuation_observation=continuation.writer_observation,
                )
                audit_outcome = continuation.accept_physical(checkpoint_sql, physical)
            elif checkpoint_sql is None:
                audit_outcome = await sessions_service.persist_compose_turn_async(
                    session_id=session_id,
                    assistant_content=assistant_message.content or "",
                    raw_content=raw_assistant_content,
                    redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                    redacted_tool_rows=redacted_tool_rows,
                    revoked_tool_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
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
                    revoked_tool_rows=revoked_tool_rows,
                    mutation_ceiling=mutation_ceiling,
                    authority_revocation=authority_revocation,
                    private_tool_evidence=tuple(private_tool_evidence),
                    private_tool_failures=private_tool_failures,
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
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        if audit_outcome.assistant_id is not None and audit_outcome.published_assistant_content is None:
            failure = AuditIntegrityError("Committed P4 omitted its actual publication content")
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        unwind_audit_failed = audit_outcome.unwind_audit_failed
        effect_verification_failure = audit_outcome.continuation_retention_failure
        authority_revocation = audit_outcome.authority_revocation
        redacted_tool_rows = audit_outcome.published_tool_rows
        current_state_id = audit_outcome.current_state_id
        failed_turn = FailedTurnMetadata(
            assistant_message_id=audit_outcome.assistant_id,
            tool_calls_attempted=len(assistant_tool_calls),
            tool_responses_persisted=0 if audit_outcome.assistant_id is None else len(redacted_tool_rows),
        )
        if audit_outcome.assistant_id is None and not crash_pending:
            failure = AuditIntegrityError(
                "persist_compose_turn_async returned unwind_audit_failed without an in-flight plugin crash",
                failed_turn=failed_turn,
            )
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        if audit_outcome.assistant_id is None and not audit_outcome.unwind_audit_failed:
            failure = AuditIntegrityError(
                "persist_compose_turn_async returned no assistant id without the unwind-audit-failed disposition",
                failed_turn=failed_turn,
            )
            if checkpoint_projection is not None:
                checkpoint_projection.complete_owned(failure)
            raise failure
        persisted_assistant_message_id = audit_outcome.assistant_id
        # Publication selection occurs under the SQL lock. Adopt its actual
        # committed bytes rather than inferring them from the candidate.
        persisted_assistant_content = audit_outcome.published_assistant_content
        # The unwind-failure outcome means the transaction rolled back: no
        # assistant or tool row survived, so the driver must retain the
        # in-flight invocation evidence on the propagated plugin crash.
        persisted_tool_call_turn = audit_outcome.assistant_id is not None
        if turn_evidence is not None and audit_outcome.checkpoint_disposition == "committed":
            try:
                witness = audit_outcome.checkpoint
                if witness is None or audit_outcome.assistant_id is None:
                    raise AuditIntegrityError("New compose checkpoint is missing its committed row witness")
                expected_state = next(
                    (
                        outcome.response.updated_state
                        for outcome in reversed(tool_outcomes)
                        if outcome.post_version > outcome.pre_version
                        and isinstance(outcome.response, ToolResult)
                        and outcome.response.updated_state is not None
                    ),
                    None,
                )
                if expected_state is None:
                    raise AuditIntegrityError("Committed compose checkpoint has no corresponding dispatched state")
                turn_evidence.accept_checkpoint(
                    witness,
                    expected_state=expected_state,
                    tool_call_ids=tuple(item.call.id for item in tool_outcomes),
                    assistant_message_id=audit_outcome.assistant_id,
                )
            except Exception as exc:
                # These rows already committed. Return the real metadata and
                # propagate after the driver records it, never use rollback or
                # unpersisted-suffix handling for a comparison failure.
                if isinstance(exc, AuditIntegrityError):
                    effect_verification_failure = exc
                else:
                    effect_verification_failure = AuditIntegrityError("Committed compose effect verification failed")
                    effect_verification_failure.__cause__ = exc
                effect_verification_failure.failed_turn = failed_turn
                turn_evidence.mark_verification_unavailable()
    try:
        result = _PersistOutcome(
            current_state_id=current_state_id,
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            persisted_tool_call_turn=persisted_tool_call_turn,
            persisted_assistant_matches_current_dispatch=(
                persisted_assistant_message_id is not None and assistant_row_uses_current_dispatch and authority_revocation is None
            ),
            unwind_audit_failed=unwind_audit_failed,
            failed_turn=failed_turn,
            redacted_assistant_tool_calls=redacted_assistant_tool_calls,
            redacted_tool_rows=redacted_tool_rows,
            audit_outcome=audit_outcome,
            effect_verification_failure=effect_verification_failure,
            authority_revocation=authority_revocation,
        )
        if continuation is not None:
            from elspeth.web.composer.continuation_live_p4_application import finish_live_p4_application

            continuation.publication = result
            if effects_before is None:
                raise AuditIntegrityError("continuation publication lost its actual before effects owner")
            await finish_live_p4_application(continuation, effects_before=effects_before)
    except BaseException as exc:
        if checkpoint_projection is not None:
            checkpoint_projection.complete_owned(exc)
        raise
    if checkpoint_projection is not None:
        checkpoint_projection.complete_owned(effect_verification_failure)
    if continuation is not None:
        continuation.raise_after_publication()
    return result
