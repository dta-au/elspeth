"""Current owed-P4 readmission; historical DATA never becomes a writer witness.

Only the actual selected committed pending P4 result may use this read facet.
Ordinary inventory/admission guards and process-drain policy remain unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import (
    ContinuationEnvSelection,
    CurrentContinuationEnvInventory,
    _read_current_env_names,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, require_snapshot_cas
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, read_p4_result_on_connection
from elspeth.web.sessions.models import composer_continuation_effects_table as effects

if TYPE_CHECKING:
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


def read_owed_p4_env_selection_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    expected_checkpoint_id: str,
    expected_cursor: int,
    effect_key: str,
) -> ContinuationEnvSelection:
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    stored = require_snapshot_cas(
        conn, session_id=sid, operation_id=operation_id, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor
    )
    root = decode_continuation_snapshot(stored.payload_json)
    frame = root.frames[-1]
    if (
        (root.logical.session_id, root.logical.operation_id) != (sid, operation_id)
        or type(frame) is not ToolBatchFrame
        or effect_key != f"batch:{frame.response.digest}:p4"
    ):
        raise AuditIntegrityError("owed P4 inventory changed its exact selected batch/effect")
    pending = tuple(item for item in root.pending_owner_results if item.effect_key == effect_key)
    if len(pending) != 1 or pending[0].kind != "persisted_turn":
        raise AuditIntegrityError("owed P4 inventory lacks its exact pending persisted result")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one_or_none()
    ref = pending[0].result
    if (
        effect is None
        or effect.owner != "session_sql"
        or effect.disposition != "committed"
        or effect.result_chunk_key != f"value:{ref.codec}:{ref.digest}"
    ):
        raise AuditIntegrityError("owed P4 inventory lacks its actual committed result binding")
    return ContinuationEnvSelection(stored.checkpoint_id, stored.cursor, encode_continuation_snapshot(root))


def require_owed_p4_env_inventory_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    inventory: CurrentContinuationEnvInventory,
    expected_checkpoint_id: str,
    expected_cursor: int,
    effect_key: str,
) -> frozenset[str]:
    if (
        type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
        or type(inventory.selection) is not ContinuationEnvSelection
        or type(inventory.names) is not frozenset
        or any(type(name) is not str for name in inventory.names)
    ):
        raise AuditIntegrityError("owed P4 read lost its actual current inventory owner")
    current = read_owed_p4_env_selection_on_connection(
        conn, running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor, effect_key=effect_key
    )
    if current != inventory.selection:
        raise AuditIntegrityError("owed P4 inventory belongs to another complete selected checkpoint")
    return inventory.names


def read_owed_p4_result_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    inventory: CurrentContinuationEnvInventory,
    expected_checkpoint_id: str,
    expected_cursor: int,
    effect_key: str,
) -> ReadmittedP4Result:
    names = require_owed_p4_env_inventory_on_connection(
        conn,
        running,
        inventory=inventory,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
        effect_key=effect_key,
    )
    # Reuse the actual complete receipt/row/creator verifier in the SAME transaction.
    try:
        return read_p4_result_on_connection(
            conn,
            running,
            expected_checkpoint_id=expected_checkpoint_id,
            expected_cursor=expected_cursor,
            effect_key=effect_key,
            env_ref_names=names,
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("owed P4 storage is not strict UTF-8") from error


def _require_current_read_owners(
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
) -> ComposerOperationRunning:
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not SelectedContinuationState
        or type(required_work) is not RequiredWorkBinding
        or type(required_work.running) is not ComposerOperationRunning
    ):
        raise AuditIntegrityError("owed P4 read requires its actual selected current owners")
    running = required_work.running
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("owed P4 read changed its required-work logical claim")
    return running


async def _read_owed_p4_selection(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
    effect_key: str,
) -> ContinuationEnvSelection:
    running = _require_current_read_owners(authority, selected, required_work)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_owed_p4_env_selection,
            running,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            effect_key=effect_key,
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
    result: ContinuationEnvSelection | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if (
                type(outcome.value) is not ContinuationEnvSelection
                or outcome.value.checkpoint_id != selected.checkpoint_id
                or outcome.value.cursor != selected.stack.cursor
                or outcome.value.snapshot_json != encode_continuation_snapshot(selected.stack)
            ):
                raise AuditIntegrityError("owed P4 read changed its complete selected root")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("owed P4 selection returned an unknown SQL outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Owed P4 selection outcomes", errors)
    if result is None:
        raise AuditIntegrityError("owed P4 selection lost its actual result")
    return result


async def materialize_owed_p4_env_inventory(
    *,
    advisor: AdvisorCheckpointOwner,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
    effect_key: str,
) -> CurrentContinuationEnvInventory:
    from elspeth.web.sessions.service import SessionServiceImpl

    running = _require_current_read_owners(authority, selected, required_work)
    if type(advisor) is not AdvisorCheckpointOwner:
        raise AuditIntegrityError("owed P4 inventory requires the actual nominal Advisor")
    sessions = advisor._require_sessions_service()
    if type(sessions) is not SessionServiceImpl or sessions._engine is not authority._engine:
        raise AuditIntegrityError("owed P4 inventory and selected SQL require the same session store")
    before = await _read_owed_p4_selection(authority=authority, selected=selected, required_work=required_work, effect_key=effect_key)
    names = await _read_current_env_names(advisor, selected.stack.logical.session_id)
    after = await _read_owed_p4_selection(authority=authority, selected=selected, required_work=required_work, effect_key=effect_key)
    if before != after:
        raise AuditIntegrityError("owed P4 inventory changed checkpoint during metadata read")
    return CurrentContinuationEnvInventory(running, after, names)


def require_selected_owed_p4_inventory(
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    running: ComposerOperationRunning,
) -> None:
    """Bind supplied DATA to this inventory; SQL still rechecks actual custody."""
    if (
        type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or type(inventory.selection) is not ContinuationEnvSelection
        or inventory.running is not running
    ):
        raise AuditIntegrityError("owed P4 read changed its actual selected inventory owners")
    selected.__post_init__()
    if (
        inventory.selection.checkpoint_id != selected.checkpoint_id
        or inventory.selection.cursor != selected.stack.cursor
        or inventory.selection.snapshot_json != encode_continuation_snapshot(selected.stack)
    ):
        raise AuditIntegrityError("owed P4 read changed its supplied complete selected root")


async def read_selected_owed_p4_result(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
    inventory: CurrentContinuationEnvInventory,
    effect_key: str,
) -> ReadmittedP4Result:
    """Read actual checked application input; never rerun a writer or finisher."""
    running = _require_current_read_owners(authority, selected, required_work)
    require_selected_owed_p4_inventory(selected, inventory, running)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_owed_p4_result,
            running,
            inventory=inventory,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            effect_key=effect_key,
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
    result: ReadmittedP4Result | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not ReadmittedP4Result:
                raise AuditIntegrityError("owed P4 read returned another nominal result")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("owed P4 read returned an unknown SQL outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Owed P4 application input outcomes", errors)
    if result is None:
        raise AuditIntegrityError("owed P4 read lost its actual application input")
    return result
