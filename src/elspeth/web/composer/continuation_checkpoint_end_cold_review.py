"""Reconstruct a first paid END Review consequence from its durable H."""

from __future__ import annotations

from dataclasses import dataclass, replace
from weakref import WeakKeyDictionary

from pydantic import TypeAdapter

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _EndCompletionDisposition
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGatePassed
from elspeth.web.composer.composer_preflight import _state_contains_blob_ref
from elspeth.web.composer.composition_completion import (
    _end_disposition_after_terminal_barriers,
    _TerminalNoToolAdvisorGateOutcome,
    _with_advisor_gate_decision,
)
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness, ContinuationMandatoryCaptureOverflow
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    ColdCheckpointEndConsequenceSource,
    ColdCheckpointEndGate,
    require_original_cold_checkpoint_end_gate,
)
from elspeth.web.composer.continuation_checkpoint_end_complete_terminal import _original_blocked_absent_preflight_is_qualified
from elspeth.web.composer.continuation_checkpoint_end_input import CheckpointOrdinaryEndCaller
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _capture_owners, _registered_read
from elspeth.web.composer.continuation_tool_batch import _decode_owned
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff, state_is_structurally_empty
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gate_fingerprint
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.interpretation_state import interpretation_sites
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed, read_v4_checkpoint_end_owed_on_connection
from elspeth.web.sessions.continuation_end import EndReviewExecution
from elspeth.web.sessions.locking import locked_session_transaction

_PREFLIGHT = TypeAdapter(ValidationResult)
_GATES = TypeAdapter(CompletionGateFacts)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdProjectedEndReview:
    gate: ColdCheckpointEndGate
    execution: EndReviewExecution
    captured: CapturedContinuationRuntime
    withheld_candidate: str | None

    @property
    def source(self) -> ColdCheckpointEndConsequenceSource:
        return self.gate.source


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdProjectedEndReviewOverflow:
    gate: ColdCheckpointEndGate
    witness: ContinuationCaptureOverflowWitness


_issued: WeakKeyDictionary[ColdProjectedEndReview, tuple[object, ...]] = WeakKeyDictionary()
_overflows: WeakKeyDictionary[ColdProjectedEndReviewOverflow, tuple[object, ...]] = WeakKeyDictionary()


def require_original_cold_end_review(projected: ColdProjectedEndReview) -> None:
    original = _issued.get(projected) if type(projected) is ColdProjectedEndReview else None
    if original is None or any(
        actual is not expected
        for actual, expected in zip(
            (projected.gate, projected.execution, projected.captured, projected.withheld_candidate), original, strict=True
        )
    ):
        raise AuditIntegrityError("cold END Review lost its issued original H projection")
    require_original_cold_checkpoint_end_gate(projected.gate)


def require_original_cold_end_review_overflow(projected: ColdProjectedEndReviewOverflow) -> ContinuationCaptureOverflowWitness:
    original = _overflows.get(projected) if type(projected) is ColdProjectedEndReviewOverflow else None
    if original is None or original != (projected.gate, projected.witness):
        raise AuditIntegrityError("cold END Review overflow lost its original H witness")
    require_original_cold_checkpoint_end_gate(projected.gate)
    return projected.witness


async def project_first_cold_end_review(*, gate: ColdCheckpointEndGate) -> ColdProjectedEndReview | ColdProjectedEndReviewOverflow:
    """Apply the original no-site END gate on isolated H owners only."""
    require_original_cold_checkpoint_end_gate(gate)
    source, computation = gate.source, gate.computation
    runtime, live, end_input = source.runtime, source.runtime.live, source.end_input
    if (
        source.owed.retry_ordinal not in (0, 1)
        or (source.owed.retry_ordinal == 1) != (source.owed.retry_history is not None)
        or type(end_input.caller) is not CheckpointOrdinaryEndCaller
    ):
        raise AuditIntegrityError("cold END Review requires its bounded ordinary paid action")
    if end_input.caller.proof_runtime_preflight_json is not None:
        raise AuditIntegrityError("cold END Review cannot borrow a proof preflight override")
    if computation.action not in ("blocked_or_orphan_terminal", "fallthrough"):
        raise AuditIntegrityError("cold END Review cannot consume authoring repair")
    prior = live.last_runtime_preflight
    held = None if end_input.runtime_preflight_json is None else _decode_owned(_PREFLIGHT, end_input.runtime_preflight_json)
    gates = None if end_input.completion_gates_json is None else _decode_owned(_GATES, end_input.completion_gates_json)
    graph_blocked = (
        gates is not None
        and gates.advisor_signoff is not None
        and gates.advisor_signoff.for_graph == completion_gate_fingerprint(live.state)
        and gates.advisor_signoff.cause is AdvisorBlockCause.GRAPH_REJECTED
    )
    no_effectful_recheck = (
        live.state.version == live.initial_version
        and not _state_contains_blob_ref(live.state)
        and (
            prior is not None
            or live.state.validate().is_valid
            or (prior is None and state_is_structurally_empty(live.state))
            or (
                computation.action == "blocked_or_orphan_terminal"
                and _original_blocked_absent_preflight_is_qualified(live.state, initial_version=live.initial_version, last_preflight=prior)
            )
        )
        and held == prior
        and not interpretation_sites(live.state)
        and not end_input.orphaned_precheck
        and end_input.orphan_events_json == "[]"
        and not live.advisor_repair_context_introduced
        and not graph_blocked
    )
    if not no_effectful_recheck:
        raise AuditIntegrityError("cold END Review needs an effectful preflight or interpretation consequence")
    index = end_input.superseded_reply_index
    if index is not None and (
        not 0 <= index < len(live.llm_messages)
        or live.llm_messages[index] != {"role": "assistant", "content": computation.candidate}
        or end_input.superseded_reply_sha256 != stable_hash(live.llm_messages[index])
    ):
        # The original END input reader already checks its stored hash. The
        # current dialogue equality is the check needed after restoration.
        raise AuditIntegrityError("cold END Review changed its superseded reply")
    withheld: str | None = None
    if computation.action == "blocked_or_orphan_terminal":
        malformed_final = (
            source.owed.retry_ordinal == 1
            and source.owed.retry_history is not None
            and not computation.verdict.ok
            and computation.verdict.failure_class == "malformed"
            and computation.is_last_pass
        )
        flagged_final = (
            computation.verdict.ok
            and computation.verdict.blocking
            and computation.verdict.publication_verdict == "WITHHOLD"
            and computation.is_last_pass
        )
        if (
            not (flagged_final or malformed_final)
            or computation.publication_admitted
            or computation.genuine_orphans
            or (held is not None and (not held.is_valid or is_pending_interpretation_handoff(held)))
            or (
                held is None
                and not _original_blocked_absent_preflight_is_qualified(
                    live.state, initial_version=live.initial_version, last_preflight=prior
                )
            )
        ):
            raise AuditIntegrityError("cold END blocked Review lacks the first qualified terminal branch")
        saved_scope = live.turn_evidence.render_saved_scope()
        withheld = computation.candidate if computation.candidate.strip() else None
        result = runtime.materialization.service._advisor_checkpoint._advisor_blocked_result(
            reason="malformed" if malformed_final else "flagged_final_pass",
            verdict=computation.verdict,
            state=live.state,
            assistant_message=_AdmittedAssistantMessage(content=saved_scope),
            recorder=live.recorder,
            repair_turns_used=live.repair_turns_used,
            persisted_assistant_message_id=live.persisted_assistant_message_id,
            persisted_assistant_content=live.persisted_assistant_content,
            persisted_tool_call_turn=live.persisted_tool_call_turn,
            runtime_preflight=held,
            outstanding_findings=None,
            publication_binding=computation.publication_review.binding,
            saved_scope=saved_scope,
        )
        result = replace(result, graph_review_identity=computation.review_identity)
        result = runtime.materialization.service._completion._append_checkpoint_receipt(result, turn_evidence=live.turn_evidence)
        disposition = _EndCompletionDisposition.BLOCKED
    else:
        if not computation.publication_admitted:
            raise AuditIntegrityError("cold END clean Review lost publication admission")
        completion = runtime.materialization.service._completion
        result = await completion._finalize_no_tool_response(
            content=computation.candidate,
            state=live.state,
            initial_version=live.initial_version,
            user_id=runtime.bindings.user_id,
            session_id=runtime.bindings.session_id,
            last_runtime_preflight=prior,
            runtime_preflight_cache=live.runtime_preflight_cache,
            session_scope=runtime.bindings.session_scope,
            user_message=runtime.bindings.message,
            mutation_success_seen=live.mutation_success_seen,
            tool_invocations=live.recorder.invocations,
            llm_calls=live.recorder.llm_calls,
            plugin_snapshot=runtime.bindings.plugin_snapshot,
            session_operation_context=runtime.bindings.session_operation_context,
        )
        decision = (
            AdvisorGatePassed(for_graph=completion_gate_fingerprint(live.state))
            if prior is not None and prior.is_valid and not state_is_structurally_empty(live.state)
            else None
        )
        result = _with_advisor_gate_decision(result, gates, decision, computation.review_identity)
        result = replace(
            result,
            repair_turns_used=live.repair_turns_used,
            persisted_assistant_message_id=live.persisted_assistant_message_id,
            persisted_assistant_content=live.persisted_assistant_content,
            persisted_tool_call_turn=live.persisted_tool_call_turn,
        )
        result = completion._append_checkpoint_receipt(result, turn_evidence=live.turn_evidence)
        disposition = _end_disposition_after_terminal_barriers(
            result,
            _TerminalNoToolAdvisorGateOutcome(
                action="fall_through",
                advisor_gate_decision=decision,
                publication_binding=computation.publication_review.binding,
                graph_review_identity=computation.review_identity,
            ),
        )
    if index is not None:
        del live.llm_messages[index]
    live.advisor_review_state = computation.review_state
    live.advisor_checkpoint_passes_used += computation.passes_delta
    if (
        result.pipeline_commit_intent is not None
        or result.graph_recovery_intent is not None
        or result.recovery_audit_drafts
        or result.llm_calls[-1:] != (live.recorder.llm_calls[-1],)
    ):
        raise AuditIntegrityError("cold END Review changed its original paid result")
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding or work.running is None:
        raise AuditIntegrityError("cold END Review lacks original required work")
    running = work.running

    def read_h() -> VerifiedCheckpointEndOwed:
        with locked_session_transaction(runtime.materialization.authority._engine, str(running.claim.session_id)) as conn:
            owed = read_v4_checkpoint_end_owed_on_connection(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                attempt_id=source.owed.attempt_id,
                names=runtime.env_ref_names,
            )
            if owed != source.owed:
                raise AuditIntegrityError("cold END Review changed H during projection")
            return owed

    before = await _registered_read(work, VerifiedCheckpointEndOwed, read_h)
    capture_source = replace(runtime.source, selected=source.selected)
    try:
        captured = _capture_owners(runtime, source=capture_source, inventory=runtime.materialization.inventory)
    except (ComposerContextUnsupported, AuditIntegrityError) as error:
        witness = error.__cause__
        if type(witness) is not ContinuationMandatoryCaptureOverflow:
            raise
        after = await _registered_read(work, VerifiedCheckpointEndOwed, read_h)
        if after != before:
            raise AuditIntegrityError("cold END Review changed H across overflow capture") from error
        overflow = ColdProjectedEndReviewOverflow(gate, witness.witness)
        _overflows[overflow] = (gate, witness.witness)
        return overflow
    after = await _registered_read(work, VerifiedCheckpointEndOwed, read_h)
    if after != before:
        raise AuditIntegrityError("cold END Review changed H across complete capture")
    execution = EndReviewExecution(source.selected, runtime, result, computation.publication_review.binding, disposition)
    projected = ColdProjectedEndReview(gate, execution, captured, withheld)
    _issued[projected] = (gate, execution, captured, withheld)
    return projected
