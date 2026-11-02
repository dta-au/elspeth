"""Adopt a genuine V4 value batch only inside its full reserved SQL tail."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_limits import ContinuationDocumentTooLarge
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_value_batch_preparation import ValueBatchPreparationRefused, prepare_value_batch
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_value_batch_tail import retain_value_batch_tail_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases


@dataclass(frozen=True, slots=True)
class _ValueBatchAdmission:
    snapshot: StoredContinuationSnapshot | None


async def try_admit_value_batch_tool(
    owner: ContinuationLivePhases,
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> bool:
    """Return false only without adoption; keep original failures and clocks."""
    try:
        batch = await prepare_value_batch(owner, runtime=runtime, selected=selected, inventory=inventory)
    except (ValueBatchPreparationRefused, ContinuationDocumentTooLarge, ComposerContextUnsupported):
        return False
    sessions = owner.service._sessions_service
    if type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("value batch lost its actual session publication owner")

    def write() -> _ValueBatchAdmission:
        try:
            with (
                locked_session_transaction(owner.authority._engine, str(inventory.running.claim.session_id)) as conn,
                sessions._session_write_lock(conn, str(inventory.running.claim.session_id)),
            ):
                applied = retain_value_batch_tail_on_connection(
                    conn, inventory.running, service=sessions, batch=batch, published_at=datetime.now(UTC)
                )
        except (ContinuationRetainedCapacityExceeded, ContinuationDocumentTooLarge, ComposerContextUnsupported):
            # The original transaction has rolled back before this owned
            # no-effect refusal is delivered to its ordinary live owner.
            return _ValueBatchAdmission(None)
        # The real transaction committed. Preserve its immutable locator before
        # required-work delivery can surface an original cancellation instead.
        owner.latest_snapshot = applied.snapshot
        owner.runtime = None
        owner.primary = None
        owner.p4 = None
        owner.finished_batch = None
        return _ValueBatchAdmission(applied.snapshot)

    retained = await _registered(
        work=owner.work, source=RequiredWorkSource.CONTINUATION_STATE_SQL, operation=write, expected=_ValueBatchAdmission
    )
    return retained.snapshot is not None
