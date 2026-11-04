"""Explicit original EARLY/END source, passed through the existing call chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _registered_read
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.continuation_retention_memo import borrow_v4_preparation_memo_scope

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource


@dataclass(frozen=True, slots=True)
class CheckpointPhaseOwner:
    phases: ContinuationLivePhases
    runtime: RestoredContinuationRuntime
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    phase_source: CheckpointPhaseSource
    superseded_reply_index: int | None
    elide_advisor_exchange: bool

    def __post_init__(self) -> None:
        from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
        from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource

        if (
            type(self.phases) is not ContinuationLivePhases
            or type(self.runtime) is not RestoredContinuationRuntime
            or type(self.selected) is not SelectedContinuationState
            or type(self.inventory) is not CurrentContinuationEnvInventory
            or type(self.phase_source) is not CheckpointPhaseSource
            or type(self.runtime.source.policy) is not ContinuationAdmissionPolicyV4
            or self.runtime.source.selected != self.selected
            or self.phase_source.current.checkpoint_id != self.selected.checkpoint_id
            or self.phase_source.current.cursor != self.selected.stack.cursor
            or self.runtime.bindings.required_work is not self.phases.work
            or self.runtime.bindings.provider_owner is not self.phases.provider_owner
            or self.runtime.materialization.authority is not self.phases.authority
            or self.inventory.running is not self.phases.work.running
            or self.inventory.names != self.runtime.env_ref_names
            or type(self.elide_advisor_exchange) is not bool
        ):
            raise AuditIntegrityError("checkpoint phase owner changed its original V4 runtime or SQL source")
        if self.superseded_reply_index is not None and (
            type(self.superseded_reply_index) is not int
            or not 0 <= self.superseded_reply_index < len(self.runtime.live.llm_messages)
            or self.phase_source.phase != "end"
        ):
            raise AuditIntegrityError("checkpoint phase owner lost its original END reply position")


async def prepare_checkpoint_phase_owner(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    *,
    superseded_reply_index: int | None,
    elide_advisor_exchange: bool,
) -> CheckpointPhaseOwner:
    """Read the original dispatched outer phase; grant no physical admission."""
    from elspeth.web.sessions.continuation import _require_current_continuation_running, load_snapshot
    from elspeth.web.sessions.continuation_checkpoint_paid import checkpoint_phase_source_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction

    running = phases.work.running
    if running is None or runtime.source.selected != selected or phases.runtime is not runtime:
        raise AuditIntegrityError("checkpoint phase preparation lost its current runtime owner")

    def read() -> CheckpointPhaseOwner:
        with (
            borrow_v4_preparation_memo_scope(phases.action_clock),
            locked_session_transaction(phases.authority._engine, str(running.claim.session_id)) as conn,
        ):
            _require_current_continuation_running(conn, running)
            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            if stored.checkpoint_id != selected.checkpoint_id or stored.cursor != selected.stack.cursor:
                raise AuditIntegrityError("checkpoint phase preparation lost its current SQL head")
            source = checkpoint_phase_source_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=stored
            )
            if source is None or source.terminal:
                raise AuditIntegrityError("checkpoint phase preparation lacks an original runnable outer source")
            return CheckpointPhaseOwner(
                phases, runtime, selected, runtime.materialization.inventory, source, superseded_reply_index, elide_advisor_exchange
            )

    return await _registered_read(phases.work, CheckpointPhaseOwner, read)
