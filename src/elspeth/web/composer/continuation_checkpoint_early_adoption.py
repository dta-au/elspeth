"""Adopt one committed EARLY graph without replacing its physical owners.

Cost projection runs in the actual completion delivery path before deferred
cancellation escapes. Warm restoration is optional business work inside the
same driver action; it never starts another clock or spends settlement reserve.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from functools import partial
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation_checkpoint_early_live import _admitted, _require_return
from elspeth.web.composer.continuation_checkpoint_early_physical import CheckpointEarlyPhysicalSource
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_checkpoint_projection import ProjectedEarlyCheckpoint
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_finished_batch_data import project_cold_finished_batch_data
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _registered_read, restore_continuation_runtime
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkTicket
from elspeth.web.sessions.continuation_checkpoint_early_owed_overflow import (
    EarlyCheckpointCompletion,
    OriginalEarlyCompletionReadback,
    require_original_early_completion_readback,
)
from elspeth.web.sessions.continuation_checkpoint_early_return import EarlyCompleteReturnAdmission
from elspeth.web.sessions.continuation_checkpoint_early_sql import EarlyCheckpointAdvanced
from elspeth.web.sessions.continuation_iteration_return import AppliedIterationP4Data, read_iteration_applied_p4_result_on_connection
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult, RetainedPrimaryPhaseData
from elspeth.web.sessions.locking import locked_session_transaction


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _SettledEarly:
    advanced: EarlyCheckpointAdvanced
    returned: CheckpointReturnedResponseObservation
    original_primary: PrimaryProviderResult | RetainedPrimaryPhaseData
    runtime: RestoredContinuationRuntime


_settled: WeakKeyDictionary[CheckpointEarlyPhysicalSource, ReferenceType[_SettledEarly]] = WeakKeyDictionary()


def project_committed_early_cost(
    source: CheckpointEarlyPhysicalSource,
    *,
    advanced: EarlyCheckpointAdvanced,
    returned: CheckpointReturnedResponseObservation,
    owed: EarlyCompleteReturnAdmission,
    projected: ProjectedEarlyCheckpoint,
    read_ticket: RequiredWorkTicket,
    readback: RequiredSQLReturned[EarlyCheckpointCompletion | None],
    proof: OriginalEarlyCompletionReadback,
) -> None:
    """Prove committed SQL and project its original paid call exactly once.

    Invoke in completion delivery before its errors or cancellation escape.
    Consume the completion owner's already-validated actual readback, not a
    second deep SQL read. Its caller retains and delivers cancellation after
    this synchronous projection on the original event-loop budget.
    """
    require_original_early_completion_readback(
        proof,
        source=source,
        advanced=advanced,
        returned=returned,
        owed=owed,
        projected=projected,
        read_ticket=read_ticket,
        readback=readback,
    )
    _require_return(source, returned)
    _prepared, admission = _admitted(source)
    phases = source.phase_owner.phases
    runtime = source.phase_owner.runtime
    primary = phases.primary
    budget = phases.provider_owner.cost_budget
    if (
        type(advanced) is not EarlyCheckpointAdvanced
        or primary is None
        or type(primary) not in (PrimaryProviderResult, RetainedPrimaryPhaseData)
        or source in _settled
        or budget is None
        or runtime.bindings.provider_owner is not phases.provider_owner
        or owed.original is not admission
        or returned.audit_call.call_id != admission.attempt.attempt_id
    ):
        raise AuditIntegrityError("EARLY cost projection lost its original unprojected owners")
    actual = readback.value
    if actual is None or type(actual.result) is not EarlyCheckpointAdvanced or actual.result is not advanced:
        raise AuditIntegrityError("EARLY cost projection lacks its exact committed SQL consequence")
    # Preserve the committed head even if the following local cost projection
    # fails. Failure does not roll back the already committed SQL transaction.
    phases.latest_snapshot = advanced.completed
    call = returned.audit_call
    accountable = call.cost_response_accountable
    before = budget.snapshot()
    if (
        type(accountable) is not bool
        or before.physical_calls != len(before.charges) + 1
        or any(charge.call_id == call.call_id for charge in before.charges)
    ):
        raise AuditIntegrityError("EARLY completion lost its original unsettled cost projection")
    budget.consume_settled(call, response_accountable=accountable)
    settled = _SettledEarly(advanced, returned, primary, runtime)
    object.__setattr__(source, "_settled_record", settled)
    _settled[source] = ref(settled)


async def adopt_committed_early(
    source: CheckpointEarlyPhysicalSource,
    *,
    advanced: EarlyCheckpointAdvanced,
    returned: CheckpointReturnedResponseObservation,
) -> RestoredContinuationRuntime:
    """Publish a complete current graph before the original driver action ends.

    This is not a completion callback: Stop/cancellation suppresses adoption,
    while the independently proved cost projection must already have happened.
    No old physical source verifier may be used after the final publication.
    """
    _require_return(source, returned)
    prepared, _admission = _admitted(source)
    reference = _settled.get(source)
    settled = None if reference is None else reference()
    phases = source.phase_owner.phases
    old = source.phase_owner.runtime
    clock = source.clock
    sql_owner = phases.work.coordinator.sql_deadline_owner
    if (
        settled is None
        or settled is not source._settled_record
        or settled.advanced is not advanced
        or settled.returned is not returned
        or settled.runtime is not old
        or phases.runtime is not old
        or phases.primary is not settled.original_primary
        or phases.latest_snapshot is not advanced.completed
        or sql_owner is None
        or phases.work.running is not prepared.running
        or old.bindings.progress is not phases.progress
        or old.bindings.required_work is not phases.work
        or old.bindings.provider_owner is not phases.provider_owner
    ):
        raise AuditIntegrityError("EARLY adoption lost its original settled runtime owners")
    sql_owner.require_action_clock(clock)
    provider_owner, budget = phases.provider_owner, phases.provider_owner.cost_budget
    work, lease, progress, p4 = phases.work, phases.lease, phases.progress, phases.p4
    now = time.monotonic()
    action_ceiling = min(
        old.bindings.deadline,
        clock.started_monotonic + clock.policy.total,
        now + sql_owner.remaining_dispatch(monotonic_now=now),
    )

    def require_adoption_time() -> None:
        sql_owner.require_action_clock(clock)
        lease.raise_if_lost()
        if lease.closed or phases.cancellation_requested.is_set():
            raise asyncio.CancelledError("EARLY graph adoption stopped after committed settlement")
        current = time.monotonic()
        if current >= action_ceiling or sql_owner.remaining_dispatch(monotonic_now=current) <= 0:
            raise TimeoutError("EARLY graph adoption exceeded its original action allowance")
        if (
            phases.runtime is not old
            or phases.primary is not settled.original_primary
            or phases.provider_owner is not provider_owner
            or provider_owner.cost_budget is not budget
            or phases.work is not work
            or phases.lease is not lease
            or phases.progress is not progress
            or phases.p4 is not p4
            or phases.action_clock is not clock
        ):
            raise AuditIntegrityError("EARLY adoption replaced an original live owner")

    require_adoption_time()
    selected = await load_selected_continuation(
        authority=phases.authority, running=prepared.running, selected=advanced.completed, required_work=work
    )
    require_adoption_time()
    inventory = await materialize_continuation_env_inventory(
        advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=selected, required_work=work
    )
    require_adoption_time()
    restored = await restore_continuation_runtime(
        service=phases.service,
        authority=phases.authority,
        selected=selected,
        inventory=inventory,
        required_work=work,
        provider_owner=provider_owner,
        progress=progress,
    )
    require_adoption_time()
    # The persistent deadline belongs to the segment. The old EARLY action
    # ceiling applies only to this adoption, never to a later fitted action.
    restored = replace(
        restored,
        bindings=replace(
            restored.bindings,
            deadline=min(old.bindings.deadline, restored.bindings.deadline, sql_owner.hard_expires_at),
        ),
    )

    def read_applied() -> AppliedIterationP4Data:
        with locked_session_transaction(phases.authority._engine, str(prepared.running.claim.session_id)) as conn:
            return read_iteration_applied_p4_result_on_connection(conn, prepared.running, selected=selected, inventory=inventory)

    applied = await _registered_read(work, AppliedIterationP4Data, read_applied)
    require_adoption_time()
    compose, batch_frame = applied_batch_cut(selected.stack.frames)
    if compose.pending_response is None or compose.pending_response != batch_frame.response:
        raise AuditIntegrityError("EARLY adoption lost the original applied PRIMARY response")
    historical = await _registered_read(
        work,
        RetainedPrimaryPhaseData,
        partial(
            phases.authority.read_continuation_phase_primary,
            prepared.running,
            selected=selected,
            reference=compose.pending_response,
            env_ref_names=inventory.names,
        ),
    )
    require_adoption_time()
    original_primary = settled.original_primary
    data = restored.graph.batch
    audit = historical.outcome.audit_call
    if (
        data is None
        or audit is None
        or historical.response != original_primary.response
        or historical.outcome != original_primary.outcome
        or historical.outcome.completion.tool_batch.calls != data.calls
    ):
        raise AuditIntegrityError("EARLY adoption changed its original PRIMARY DATA")
    matching = tuple(call for call in restored.live.recorder.llm_calls if call.call_id == audit.call_id)
    if len(matching) != 1 or matching[0] != audit:
        raise AuditIntegrityError("EARLY adoption changed its restored PRIMARY recorder DATA")
    completion = historical.outcome.completion
    historical = RetainedPrimaryPhaseData(
        historical.response,
        _CallModelOutcome(replace(completion, tool_batch=replace(completion.tool_batch, calls=data.calls)), matching[0]),
    )
    context = await phases._tool_context(restored)
    require_adoption_time()
    context = replace(context, deadline=min(context.deadline, action_ceiling))
    finished = project_cold_finished_batch_data(
        runtime=restored,
        selected=selected,
        primary=historical,
        p4_result=applied.result,
        iteration_stage=applied.stage,
        context=context,
    )
    # Source identity is intentionally checked for the final time BEFORE the
    # complete new graph replaces its old runtime. No await splits publication.
    _require_return(source, returned)
    require_adoption_time()
    phases.runtime = restored
    phases.finished_batch = finished.dispatch, finished.persist, finished.pre_batch_state
    del _settled[source]
    object.__setattr__(source, "_settled_record", None)
    return restored
