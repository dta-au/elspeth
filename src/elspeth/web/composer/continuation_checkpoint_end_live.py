"""Run the original paid END Review without entering the legacy retry loop.

The application still selects its admission policy independently. This caller
consumes the source-owned V4 leaves and exits through their committed controls;
it never returns a verdict for the legacy pass writer to persist again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointPaidOverflowTerminal,
    _EndCheckpointDispositionControl,
)
from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained, run_v4_checkpoint_end_first_return
from elspeth.web.composer.continuation_checkpoint_end_format_retry import (
    CheckpointEndFormatRetrySource,
    prepare_checkpoint_end_format_retry,
)
from elspeth.web.composer.continuation_checkpoint_end_locals import require_original_checkpoint_end_gate_locals
from elspeth.web.composer.continuation_checkpoint_end_physical import observe_checkpoint_end_paid_incomplete_terminal
from elspeth.web.composer.continuation_checkpoint_end_terminal import (
    EndCheckpointCompleteReview,
    EndCheckpointFormatRetryReady,
    EndCheckpointRepairContinued,
    _EndCheckpointCompleteReviewControl,
    _EndCheckpointFormatRetryReadyControl,
    _EndCheckpointPaidIncompleteControl,
    _EndCheckpointRepairContinuedControl,
)
from elspeth.web.composer.continuation_checkpoint_end_usable import complete_v4_checkpoint_end_usable_return
from elspeth.web.composer.provider_quota import V4PaidIncompleteCommitted

if TYPE_CHECKING:
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.continuation_checkpoint_end_locals import CheckpointEndGateLocals
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner


async def run_v4_checkpoint_end_live(
    *,
    advisor: AdvisorCheckpointOwner,
    phase_owner: CheckpointPhaseOwner,
    gate_locals: CheckpointEndGateLocals,
    deadline: float | None,
    format_retry: CheckpointEndFormatRetrySource | None = None,
) -> Never:
    """Run one physical call; a format retry needs its own admitted action."""
    from elspeth.web.composer.advisor_checkpoint import (
        _AdvisorCheckpointComposeDeadlineExpired,
        _compose_deadline_time,
        _parse_advisor_checkpoint_guidance,
    )

    require_original_checkpoint_end_gate_locals(gate_locals, phase_owner=phase_owner)
    if format_retry is not None:
        from elspeth.web.composer.continuation_checkpoint_end_format_retry import require_original_checkpoint_end_format_retry

        require_original_checkpoint_end_format_retry(format_retry)
        if format_retry.phase_owner is not phase_owner or format_retry.gate_locals is not gate_locals:
            raise AuditIntegrityError("V4 END live retry changed its original issued action source")
    provider_owner = phase_owner.phases.provider_owner
    budget = provider_owner.cost_budget
    if budget is None or phase_owner.phase_source.phase != "end":
        raise AuditIntegrityError("V4 END live caller lost its original phase or cost owner")

    async def call(
        owner: CheckpointPhaseOwner,
        gate: CheckpointEndGateLocals,
        retry: CheckpointEndFormatRetrySource | None = None,
    ) -> V4EndFirstRetained:
        remaining = None if deadline is None else deadline - _compose_deadline_time()
        if remaining is not None and remaining <= 0:
            raise _AdvisorCheckpointComposeDeadlineExpired
        budget.require_end_headroom()
        try:
            returned = await run_v4_checkpoint_end_first_return(
                advisor=advisor,
                phase_owner=owner,
                gate_locals=gate,
                recorder=owner.runtime.live.recorder,
                provider_owner=provider_owner,
                timeout=remaining,
                format_retry=retry,
            )
        except V4PaidIncompleteCommitted as control:
            terminal = observe_checkpoint_end_paid_incomplete_terminal(control, phase_owner=owner, gate_locals=gate)
            raise _EndCheckpointPaidIncompleteControl(terminal) from None
        if type(returned) is EndCheckpointPaidOverflowTerminal:
            raise _EndCheckpointDispositionControl(returned)
        if type(returned) is not V4EndFirstRetained:
            raise AuditIntegrityError("V4 END live caller lost its original retained physical return")
        return returned

    retained = await call(phase_owner, gate_locals, format_retry)
    guidance = retained.returned.message.content
    if type(guidance) is not str:
        raise AuditIntegrityError("V4 END live caller lost its complete returned guidance text")
    if format_retry is None and not _parse_advisor_checkpoint_guidance(guidance, phase="end").ok:
        retry = await prepare_checkpoint_end_format_retry(retained)
        if type(retry) is EndCheckpointPaidOverflowTerminal:
            raise _EndCheckpointDispositionControl(retry)
        if type(retry) is not CheckpointEndFormatRetrySource:
            raise AuditIntegrityError("V4 END live caller lost its issued one-format-retry source")
        raise _EndCheckpointFormatRetryReadyControl(EndCheckpointFormatRetryReady(retry.proof, retry.proof.selected))
    completed = await complete_v4_checkpoint_end_usable_return(
        physical=retained.physical, prepared=retained.prepared, owed=retained.owed, returned=retained.returned
    )
    if type(completed) is EndCheckpointCompleteReview:
        raise _EndCheckpointCompleteReviewControl(completed)
    if type(completed) is EndCheckpointRepairContinued:
        raise _EndCheckpointRepairContinuedControl(completed)
    if type(completed) is EndCheckpointPaidOverflowTerminal:
        raise _EndCheckpointDispositionControl(completed)
    raise AuditIntegrityError("V4 END live caller returned an unfamiliar committed consequence")
