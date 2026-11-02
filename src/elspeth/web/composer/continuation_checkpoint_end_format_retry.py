"""One format retry after the complete original END result is durable.

The original malformed call is settled and retained before a fresh phase/gate
is captured. Only this issued source selects the existing format re-prompt;
neither a decoded receipt nor an SDK failure grants another paid call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_audit_buffer import RECORDER_CODEC
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness, ContinuationMandatoryCaptureOverflow
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointPaidOverflowTerminal
from elspeth.web.composer.continuation_checkpoint_end_locals import (
    CheckpointEndGateLocals,
    OrdinaryEndCallerLocals,
    SourceProofEndCallerLocals,
    capture_checkpoint_end_gate_locals,
    require_original_checkpoint_end_gate_locals,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import require_original_checkpoint_end_physical_source
from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner, prepare_checkpoint_phase_owner
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_runtime import _registered_read
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import _require_current_continuation_running, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_retry import (
    VerifiedEndFormatRetry,
    read_end_format_retry_on_connection,
    retain_end_format_retry_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEndFormatRetrySource:
    proof: VerifiedEndFormatRetry
    phase_owner: CheckpointPhaseOwner
    gate_locals: CheckpointEndGateLocals


_issued: WeakKeyDictionary[CheckpointEndFormatRetrySource, tuple[VerifiedEndFormatRetry, CheckpointPhaseOwner, CheckpointEndGateLocals]] = (
    WeakKeyDictionary()
)


def require_original_checkpoint_end_format_retry(source: CheckpointEndFormatRetrySource) -> None:
    original = _issued.get(source) if type(source) is CheckpointEndFormatRetrySource else None
    if original is None or any(
        actual is not expected for actual, expected in zip((source.proof, source.phase_owner, source.gate_locals), original, strict=True)
    ):
        raise AuditIntegrityError("END format retry requires its original SQL and gate source")
    owner, proof = source.phase_owner, source.proof
    if (
        owner.phase_source.current != proof.selected
        or owner.phase_source.source != proof.owed.phase_source
        or owner.phase_source.completed_children != (proof.marker.effect_key,)
        or owner.runtime.env_ref_names != frozenset(proof.marker.end_env_ref_names)
        or source.gate_locals.pass_index != proof.marker.pass_index
    ):
        raise AuditIntegrityError("END format retry changed its original pass or retained phase")
    require_original_checkpoint_end_gate_locals(source.gate_locals, phase_owner=owner)


async def prepare_checkpoint_end_format_retry(
    first: V4EndFirstRetained,
) -> CheckpointEndFormatRetrySource | EndCheckpointPaidOverflowTerminal:
    """Close one malformed H, then capture the actual next gate on that cut."""
    from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained

    if type(first) is not V4EndFirstRetained or first.prepared.retry_ordinal != 0:
        raise AuditIntegrityError("END format retry requires its first original complete result")
    physical = first.physical
    require_original_checkpoint_end_physical_source(physical)
    before = physical.phase_owner
    phases = before.phases
    running = phases.work.running
    sessions = phases.service._sessions_service
    if (
        running is None
        or type(sessions) is not SessionServiceImpl
        or first.prepared.running is not running
        or phases.runtime is not before.runtime
    ):
        raise AuditIntegrityError("END format retry lost its original running runtime")
    sid, operation = str(running.claim.session_id), running.claim.operation_id

    def retain() -> VerifiedEndFormatRetry:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            return retain_end_format_retry_on_connection(
                conn,
                service=sessions,
                prepared=first.prepared,
                owed_admission=first.owed,
                returned=first.returned,
            )

    async def settle_overflow(
        failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
    ) -> EndCheckpointPaidOverflowTerminal:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=lambda: sessions.settle_v4_checkpoint_end_owed_overflow_sync(
                prepared=first.prepared, owed=first.owed, returned=first.returned, failure=failure
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        return EndCheckpointPaidOverflowTerminal(settled)

    try:
        proof = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=retain,
            expected=VerifiedEndFormatRetry,
        )
    except ContinuationRetainedCapacityExceeded as excess:
        return await settle_overflow(excess)
    except AuditIntegrityError as error:
        cause = error.__cause__
        if type(cause) is not ContinuationMandatoryCaptureOverflow or cause.witness.codec not in (RECORDER_CODEC, "composer.evidence.v2"):
            raise
        return await settle_overflow(cause.witness)
    # V4 custody deliberately defers ordinary cost settlement until its SQL
    # consequence is durable. Project this exact proved call before restoring
    # the next runtime; its cost owner must agree with the complete ledger.
    budget = phases.provider_owner.cost_budget
    call = first.returned.audit_call
    accountable = call.cost_response_accountable
    if (
        budget is None
        or type(accountable) is not bool
        or before.runtime.bindings.provider_owner is not phases.provider_owner
        or proof.marker.attempt_id != call.call_id
        or budget.snapshot().physical_calls != len(budget.snapshot().charges) + 1
        or any(charge.call_id == call.call_id for charge in budget.snapshot().charges)
    ):
        raise AuditIntegrityError("END retry lost its original unsettled cost projection")
    budget.consume_settled(call, response_accountable=accountable)
    phases.latest_snapshot = proof.selected
    selected = await load_selected_continuation(
        authority=phases.authority, running=running, selected=proof.selected, required_work=phases.work
    )
    runtime, _inventory = await phases.current(selected)
    owner = await prepare_checkpoint_phase_owner(
        phases,
        runtime,
        selected,
        superseded_reply_index=before.superseded_reply_index,
        elide_advisor_exchange=before.elide_advisor_exchange,
    )

    def reprove() -> VerifiedEndFormatRetry:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            _require_current_continuation_running(conn, running)
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            if current != proof.selected or current.payload_json != encode_continuation_snapshot(selected.stack):
                raise AuditIntegrityError("END format retry replaced its actual current checkpoint")
            return read_end_format_retry_on_connection(
                conn, session_id=sid, operation_id=operation, selected=current, names=runtime.env_ref_names
            )

    proof = await _registered_read(phases.work, VerifiedEndFormatRetry, reprove)
    surfacing = phases.service._interpretation_surfacing
    observation = await surfacing.observe_pending_interpretation_review_sites(runtime.live.state, session_id=sid)
    previous = physical.gate_locals
    if observation.source_sha256 != previous.orphan_observation.source_sha256:
        raise AuditIntegrityError("END format retry changed its original pending interpretation cohort")
    dialogue = None
    if type(previous.caller) is OrdinaryEndCallerLocals:
        dialogue = runtime.live.llm_messages
    elif type(previous.caller) is SourceProofEndCallerLocals:
        dialogue = []
    gate = capture_checkpoint_end_gate_locals(
        replace(previous, orphan_observation=observation),
        phase_owner=owner,
        state=runtime.live.state,
        evidence=runtime.live.turn_evidence,
        surfacing=surfacing,
        session_id=sid,
        request=runtime.bindings.message,
        dialogue=dialogue,
    )
    source = CheckpointEndFormatRetrySource(proof, owner, gate)
    _issued[source] = (proof, owner, gate)
    require_original_checkpoint_end_format_retry(source)
    return source
