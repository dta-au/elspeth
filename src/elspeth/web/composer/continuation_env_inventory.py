"""Current selected names-only inventory; ephemeral, never a retained authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, require_snapshot_cas

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


@dataclass(frozen=True, slots=True)
class ContinuationEnvSelection:
    checkpoint_id: str
    cursor: int
    snapshot_json: str


@dataclass(frozen=True, slots=True)
class CurrentContinuationEnvInventory:
    running: ComposerOperationRunning
    selection: ContinuationEnvSelection
    names: frozenset[str]


def read_env_selection_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    expected_checkpoint_id: str,
    expected_cursor: int,
) -> ContinuationEnvSelection:
    _require_current_continuation_running(conn, running)
    stored = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    root = decode_continuation_snapshot(stored.payload_json)
    if root.logical.session_id != str(running.claim.session_id) or root.logical.operation_id != running.claim.operation_id:
        raise AuditIntegrityError("current inventory selection changed its logical operation")
    return ContinuationEnvSelection(stored.checkpoint_id, stored.cursor, encode_continuation_snapshot(root))


async def _read_env_selection(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
) -> ContinuationEnvSelection:
    running = required_work.running
    if type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("inventory read requires its actual running owner")
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("inventory read changed its required-work logical claim")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_env_selection,
            running,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    actual: ContinuationEnvSelection | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not ContinuationEnvSelection:
                raise AuditIntegrityError("inventory read returned another nominal selection")
            if (
                outcome.value.checkpoint_id != selected.checkpoint_id
                or outcome.value.cursor != selected.stack.cursor
                or outcome.value.snapshot_json != encode_continuation_snapshot(selected.stack)
            ):
                raise AuditIntegrityError("inventory read changed the complete selected root")
            actual = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("inventory read returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Continuation inventory read outcomes", errors)
    if actual is None:
        raise AuditIntegrityError("inventory read lost its current result")
    return actual


async def _read_current_env_names(advisor: AdvisorCheckpointOwner, session_id: str) -> frozenset[str]:
    """The one names-only producer; callers own their selected SQL brackets."""
    names = await AdvisorCheckpointOwner._current_env_ref_names(advisor, session_id)
    if type(names) is not frozenset or any(type(name) is not str for name in names):
        raise AuditIntegrityError("current Advisor environment inventory lost its actual nominal names")
    return names


async def materialize_continuation_env_inventory(
    *,
    advisor: AdvisorCheckpointOwner,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
) -> CurrentContinuationEnvInventory:
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(advisor) is not AdvisorCheckpointOwner
        or type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not SelectedContinuationState
        or type(required_work) is not RequiredWorkBinding
        or type(required_work.running) is not ComposerOperationRunning
    ):
        raise AuditIntegrityError("inventory materialization requires its actual current owners")
    sessions = advisor._require_sessions_service()
    if type(sessions) is not SessionServiceImpl or sessions._engine is not authority._engine:
        raise AuditIntegrityError("inventory metadata and selected SQL require the same actual session store")
    before = await _read_env_selection(authority=authority, selected=selected, required_work=required_work)
    # This is the existing names-only producer, including its real no-secret-service empty result.
    # It performs no secret resolution and does not derive names from retained request strings.
    names = await _read_current_env_names(advisor, selected.stack.logical.session_id)
    after = await _read_env_selection(authority=authority, selected=selected, required_work=required_work)
    if before != after:
        raise AuditIntegrityError("current inventory changed its selected checkpoint during metadata read")
    return CurrentContinuationEnvInventory(required_work.running, after, names)


def require_current_env_inventory_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    inventory: CurrentContinuationEnvInventory,
    expected_checkpoint_id: str,
    expected_cursor: int,
) -> frozenset[str]:
    if (
        type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
        or type(inventory.selection) is not ContinuationEnvSelection
        or type(inventory.names) is not frozenset
        or any(type(name) is not str for name in inventory.names)
    ):
        raise AuditIntegrityError("handoff lost its actual current inventory owner")
    selected = read_env_selection_on_connection(
        conn, running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor
    )
    if selected != inventory.selection:
        raise AuditIntegrityError("handoff inventory belongs to another selected checkpoint")
    return inventory.names
