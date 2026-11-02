"""Pure observation of one complete PRIMARY paid result.

The overflow value is incomplete DATA. It grants no SQL, accounting, retry,
classification, or result-publication authority. A future owner must rederive
the physical attempt and original effect from its current source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from elspeth.contracts.bounded_serialization import bounded_canonical_json_size, utf8_size
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata, _CallModelOutcome
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_application_graph import (
    capture_fresh_application_recorder_source,
    encode_application_graph,
)
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot, decode_recorder_snapshot
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    _continuation_response_json_value,
    encode_continuation_response,
    measure_continuation_response_size,
)
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, admit_turn_evidence, require_evidence_fields_admission
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.continuation_message_shelf import require_message_shelf_integrity
from elspeth.web.composer.continuation_provider_outcomes import (
    _physical_audit_json_value,
    measure_physical_audit_call_size,
    require_successful_response_audit,
)
from elspeth.web.composer.continuation_retention import retained_model_json_view
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, capture_provider_audit_source
from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, project_v4_provider_diagnostic_metadata
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, encode_paid_overflow_cost_amount

_LIMIT = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
_PAIR_LIMIT = CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
_MAX_LEDGER_TOKENS = 2**31 - 1


@dataclass(frozen=True, slots=True)
class PrimaryCompleteObservation:
    """The three exact ordinary chunks, before current SQL authorization."""

    outcome: _CallModelOutcome
    response: PrivateChunk
    recorder: PrivateChunk
    evidence: PrivateChunk


@dataclass(frozen=True, slots=True)
class PrimaryOverflowUsageObservation:
    status: Literal["observed", "unavailable"]
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True, slots=True)
class PrimaryOverflowObservation:
    source: ProviderAuditSource
    attempt_id: str
    representation: Literal["physical_audit", "response", "recorder", "evidence_pair", "retained_history"]
    document_codec: Literal[
        "composer.physical-audit.v1",
        "composer.response.v1",
        "composer.audit-buffer.v2",
        "composer.evidence.v2",
        "composer.continuation.history.v4",
    ]
    measurement: Literal["at_least"]
    observed_bytes: int
    limit_bytes: int
    cost: PaidOverflowCost
    usage: PrimaryOverflowUsageObservation


type PrimaryPaidObservation = PrimaryCompleteObservation | PrimaryOverflowObservation


def observed_primary_aggregate_overflow(
    *, complete: PrimaryCompleteObservation, context: SessionOperationContext, allowance_bytes: int
) -> PrimaryOverflowObservation:
    """Saturate one SQL-proved full-batch capacity refusal without inventing its length."""
    if type(complete) is not PrimaryCompleteObservation or type(context) is not SessionOperationContext:
        raise TypeError("PRIMARY aggregate overflow requires owned original complete response and context")
    if type(allowance_bytes) is not int or not 0 < allowance_bytes < 2**53 - 1:
        raise ValueError("PRIMARY aggregate overflow requires its original positive bounded allowance")
    call = complete.outcome.audit_call
    if type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("PRIMARY aggregate overflow lost its original physical audit")
    return PrimaryOverflowObservation(
        capture_provider_audit_source(context),
        call.call_id,
        "retained_history",
        "composer.continuation.history.v4",
        "at_least",
        allowance_bytes + 1,
        allowance_bytes,
        _cost(call),
        _usage(call),
    )


def _cost(call: ComposerLLMCall) -> PaidOverflowCost:
    if call.provider_cost is None:
        return PaidOverflowCost(status="unknown", amount=None, source="not_available")
    return PaidOverflowCost(status="known", amount=encode_paid_overflow_cost_amount(call.provider_cost), source=call.provider_cost_source)


def _usage(call: ComposerLLMCall) -> PrimaryOverflowUsageObservation:
    model = call.model_returned if call.model_returned is not None else call.model_requested
    tokens = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
    if (
        not model
        or len(model) > 256
        or any(value is not None and (type(value) is not int or value < 0 or value > _MAX_LEDGER_TOKENS) for value in tokens)
    ):
        return PrimaryOverflowUsageObservation("unavailable", None, None, None, None, None)
    return PrimaryOverflowUsageObservation("observed", model, *tokens)


def observe_primary_provider_result(
    *,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    previous_evidence: AdmittedTurnEvidence,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
) -> PrimaryPaidObservation:
    """Observe legacy DATA with exact original metadata/audit equality."""
    if type(outcome) is not _CallModelOutcome:
        raise AuditIntegrityError("PRIMARY observation requires its nominal outcome")
    return _observe_primary_provider_result(
        outcome=outcome,
        recorder=recorder,
        previous_evidence=previous_evidence,
        context=context,
        env_ref_names=env_ref_names,
        audit_metadata=outcome.completion.provider_metadata,
    )


def _observe_primary_provider_result(
    *,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    previous_evidence: AdmittedTurnEvidence,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
    audit_metadata: _AdmittedLLMProviderMetadata,
) -> PrimaryPaidObservation:
    """Measure exact PRIMARY success projections after validating their sources.

    The counter saturates at limit + 1. Its excess is a lower bound even when
    a fully materialized document is larger. No missing provider output is used.
    """
    if (
        type(outcome) is not _CallModelOutcome
        or type(recorder) is not RecorderSnapshot
        or type(previous_evidence) is not AdmittedTurnEvidence
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or type(env_ref_names) is not frozenset
        or previous_evidence.env_ref_names != env_ref_names
        or outcome.audit_call is None
        or type(outcome.audit_call) is not ComposerLLMCall
        or outcome.audit_call.call_id is None
    ):
        raise AuditIntegrityError("PRIMARY observation requires original complete nominal sources")
    call = outcome.audit_call
    attempt_id = call.call_id
    if attempt_id is None:
        raise AuditIntegrityError("PRIMARY observation lost its original physical attempt")
    source = capture_provider_audit_source(context)
    if (
        previous_evidence.snapshot.logical.session_id != context.fence.session_id
        or previous_evidence.snapshot.physical_operation_id != context.fence.operation_id
        or previous_evidence.snapshot.physical_operation_epoch != context.fence.operation_epoch
    ):
        raise AuditIntegrityError("PRIMARY observation changed its original session operation")
    require_successful_response_audit(
        call,
        audit_metadata,
        attempt_id=attempt_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )
    _require_no_credential_material_in_completion(outcome.completion, surface="composer_primary_response")
    require_no_credential_material(
        _continuation_response_json_value(outcome.completion),
        surface="composer_primary_overflow_response",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    measure_continuation_response_size(outcome.completion)
    require_no_credential_material(
        _physical_audit_json_value(call),
        surface="composer_primary_overflow_physical_audit",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    previous = decode_recorder_snapshot(previous_evidence.recorder_chunk)
    if (
        previous_evidence.snapshot.recorder != previous_evidence.recorder_chunk.reference
        or replace(previous, llm_calls=(*previous.llm_calls, call)) != recorder
        or not recorder.llm_calls
        or recorder.llm_calls[-1] is not call
        or any(item.call_id == attempt_id for item in previous.llm_calls)
    ):
        raise AuditIntegrityError("PRIMARY observation changed its original recorder prefix")

    # The fresh source validates the full recorder, including an oversized
    # candidate, before any capacity decision. Its normal chunk is reused.
    recorder_source = capture_fresh_application_recorder_source(recorder, env_ref_names=env_ref_names)
    audit = recorder_source.chunk
    snapshot = previous_evidence.snapshot
    updated = snapshot.model_copy(update={"recorder": audit.reference})
    graph = previous_evidence.graph
    if graph is not None:
        extended = replace(graph.recorder, llm_calls=(*graph.recorder.llm_calls, call))
        if extended != recorder:
            raise AuditIntegrityError("PRIMARY observation changed its original graph recorder prefix")
        graph_json = encode_application_graph(
            replace(graph, recorder=extended), env_ref_names=env_ref_names, recorder_source=recorder_source
        )
        if snapshot.application_entry is not None:
            updated = updated.model_copy(
                update={"application_entry": snapshot.application_entry.model_copy(update={"graph_json": graph_json})}
            )
        else:
            updated = updated.model_copy(update={"joint_graph_json": graph_json})
    # This validator retains malformed/credential/graph precedence even if an
    # earlier nested representation already measured too large.
    require_evidence_fields_admission(
        updated,
        env_ref_names=env_ref_names,
        recorder_chunk=audit,
        sources=previous_evidence.sources,
        recorder_source=recorder_source,
    )
    require_message_shelf_integrity(updated.transcript, env_ref_names=env_ref_names)
    evidence_size = bounded_canonical_json_size(retained_model_json_view(updated), limit=_LIMIT)
    recorder_bytes = utf8_size(audit.canonical, limit=_LIMIT)

    def excess(
        representation: Literal["physical_audit", "response", "recorder", "evidence_pair"],
        document_codec: Literal["composer.physical-audit.v1", "composer.response.v1", "composer.audit-buffer.v2", "composer.evidence.v2"],
        limit: int,
    ) -> PrimaryOverflowObservation:
        return PrimaryOverflowObservation(
            source, attempt_id, representation, document_codec, "at_least", limit + 1, limit, _cost(call), _usage(call)
        )

    if measure_physical_audit_call_size(call).exceeded:
        return excess("physical_audit", "composer.physical-audit.v1", _LIMIT)
    if measure_continuation_response_size(outcome.completion, audit_call=call).exceeded:
        return excess("response", "composer.response.v1", _LIMIT)
    if recorder_bytes > _LIMIT:
        return excess("recorder", "composer.audit-buffer.v2", _LIMIT)
    if evidence_size.exceeded:
        return excess("evidence_pair", "composer.evidence.v2", _LIMIT)
    evidence_bytes = evidence_size.admitted_bytes
    if evidence_bytes is None:
        raise AuditIntegrityError("PRIMARY evidence measurement lost its admitted bytes")
    if recorder_bytes + evidence_bytes > _PAIR_LIMIT:
        return excess("evidence_pair", "composer.evidence.v2", _PAIR_LIMIT)
    response = encode_continuation_response(outcome.completion, audit_call=call)
    admitted = admit_turn_evidence(updated, env_ref_names=env_ref_names, source_chunks=(audit, *previous_evidence.sources.chunks))
    return PrimaryCompleteObservation(outcome, response, audit, admitted.source_chunk)


def observe_v4_primary_provider_result(
    *,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    previous_evidence: AdmittedTurnEvidence,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
) -> PrimaryPaidObservation:
    """Require one source-issued, complete SDK return before measuring V4 bytes."""
    if type(outcome) is not _CallModelOutcome or type(outcome.audit_call) is not ComposerLLMCall:
        raise AuditIntegrityError("V4 PRIMARY result lacks its original complete provider audit")
    metadata = outcome.completion.provider_metadata
    witness = metadata.v4_completeness
    if (
        type(witness) is not V4ProviderResultCompleteness
        or type(witness.sdk_return.attempt_id) is not str
        or witness.sdk_return.attempt_id != outcome.audit_call.call_id
    ):
        raise AuditIntegrityError("V4 PRIMARY result lacks its original complete SDK return")
    witness.require_result(metadata, content=outcome.completion.message.content, tool_calls=outcome.completion.tool_batch.calls)
    return _observe_primary_provider_result(
        outcome=outcome,
        recorder=recorder,
        previous_evidence=previous_evidence,
        context=context,
        env_ref_names=env_ref_names,
        audit_metadata=project_v4_provider_diagnostic_metadata(metadata),
    )
