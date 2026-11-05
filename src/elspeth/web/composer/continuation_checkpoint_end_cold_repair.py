"""Prospective first-H repair from independently restored END DATA.

No SDK or SQL mutation occurs here. The resulting capture still needs a
separate cold-source atomic writer under the original H and session fence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    ColdCheckpointEndConsequenceSource,
    ColdCheckpointEndGate,
    require_original_cold_checkpoint_end_gate,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import EndGateComputation
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _capture_owners, _registered_runtime_capture
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.continuation_checkpoint_end_complete_withheld import (
    PreparedEndRepairWithheldReply,
    prepare_end_repair_withheld_reply,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdProjectedEndRepair:
    source: ColdCheckpointEndConsequenceSource
    gate: ColdCheckpointEndGate
    captured: CapturedContinuationRuntime
    withheld: PreparedEndRepairWithheldReply | None

    @property
    def computation(self) -> EndGateComputation:
        return self.gate.computation


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdProjectedEndRepairOverflow:
    source: ColdCheckpointEndConsequenceSource
    gate: ColdCheckpointEndGate
    witness: ContinuationCaptureOverflowWitness


_issued: WeakKeyDictionary[ColdProjectedEndRepair, tuple[object, ...]] = WeakKeyDictionary()
_overflows: WeakKeyDictionary[ColdProjectedEndRepairOverflow, tuple[object, ...]] = WeakKeyDictionary()


def require_original_cold_end_repair(projected: ColdProjectedEndRepair) -> None:
    original = _issued.get(projected) if type(projected) is ColdProjectedEndRepair else None
    if original is None or any(
        actual is not expected
        for actual, expected in zip((projected.source, projected.gate, projected.captured, projected.withheld), original, strict=True)
    ):
        raise AuditIntegrityError("cold END repair lost its original H projection")
    require_original_cold_checkpoint_end_gate(projected.gate)


def require_original_cold_end_repair_overflow(projected: ColdProjectedEndRepairOverflow) -> ContinuationCaptureOverflowWitness:
    original = _overflows.get(projected) if type(projected) is ColdProjectedEndRepairOverflow else None
    if original is None or any(
        actual is not expected for actual, expected in zip((projected.source, projected.gate, projected.witness), original, strict=True)
    ):
        raise AuditIntegrityError("cold END repair overflow lost its original H projection")
    require_original_cold_checkpoint_end_gate(projected.gate)
    return projected.witness


async def project_first_cold_end_repair(*, gate: ColdCheckpointEndGate) -> ColdProjectedEndRepair | ColdProjectedEndRepairOverflow:
    """Apply the same dialogue/counter delta to the isolated H runtime once."""
    require_original_cold_checkpoint_end_gate(gate)
    source = gate.source
    computation = gate.computation
    if (
        source.owed.retry_ordinal not in (0, 1)
        or (source.owed.retry_ordinal == 1) != (source.owed.retry_history is not None)
        or computation.action != "repair_continue"
        or computation.repair_dialogue_content is None
        or computation.advisor_injection_index != len(source.runtime.live.llm_messages)
    ):
        raise AuditIntegrityError("cold END repair changed its first usable gate decision")
    live = source.runtime.live
    superseded_index = source.end_input.superseded_reply_index
    if superseded_index is not None and (
        not 0 <= superseded_index < len(live.llm_messages)
        or live.llm_messages[superseded_index] != {"role": "assistant", "content": computation.candidate}
    ):
        raise AuditIntegrityError("cold END repair changed its original superseded reply")
    withheld = prepare_end_repair_withheld_reply(computation.candidate) if superseded_index is not None else None
    live.advisor_review_state = computation.review_state
    live.advisor_checkpoint_passes_used += computation.passes_delta
    live.llm_messages.append({"role": "user", "content": computation.repair_dialogue_content})
    live.advisor_repair_context_introduced = True
    if source.end_input.elide_advisor_exchange:
        live.pending_advisor_elision_indices.append(computation.advisor_injection_index)
        if superseded_index is not None:
            live.pending_advisor_elision_indices.append(superseded_index)
    live.turn_evidence.transcript.observe_messages(live.llm_messages)
    runtime = source.runtime
    inventory = runtime.materialization.inventory
    selected = source.selected
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("cold END repair lost its original required-work owner")

    def read_h() -> VerifiedCheckpointEndOwed:
        from elspeth.web.sessions.continuation_checkpoint_end_owed import read_v4_checkpoint_end_owed_on_connection
        from elspeth.web.sessions.locking import locked_session_transaction

        running = work.running
        if running is None:
            raise AuditIntegrityError("cold END repair lost its actual running claim")
        with locked_session_transaction(runtime.materialization.authority._engine, str(running.claim.session_id)) as conn:
            owed = read_v4_checkpoint_end_owed_on_connection(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                attempt_id=source.owed.attempt_id,
                names=inventory.names,
            )
            if owed != source.owed:
                raise AuditIntegrityError("cold END repair changed its original H during projection")
            return owed

    capture_source = replace(runtime.source, selected=selected)
    capture = await _registered_runtime_capture(
        work,
        read_source=read_h,
        capture=lambda: _capture_owners(runtime, source=capture_source, inventory=inventory),
        changed_source_message="cold END repair changed H across complete capture",
    )
    if capture.overflow is not None:
        projected_overflow = ColdProjectedEndRepairOverflow(source, gate, capture.overflow)
        _overflows[projected_overflow] = (source, gate, capture.overflow)
        return projected_overflow
    captured = capture.captured
    if captured is None:
        raise AuditIntegrityError("cold END repair lost captured DATA without a size witness")
    projected = ColdProjectedEndRepair(source, gate, captured, withheld)
    _issued[projected] = (source, gate, captured, withheld)
    return projected
