"""Selected-current complete-original reads through the real discovery owner.

This returns a live result and complete historical DATA. Neither supplies an
owner-result commit or prompt trust; those remain the caller's current effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.attachment_admission import AttachmentInspectionPolicy
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, owned_json
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.redaction import READ_ATTACHMENT_MAX_UTF8_BYTES
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadObservations,
    ContinuationReadOwnerResult,
    build_read_owner_result,
    require_read_versions_on_connection,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


@dataclass(frozen=True, slots=True)
class CurrentAttachmentRead:
    result: ToolResult
    retained: ContinuationReadOwnerResult


def _execute_current_attachment_read(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    tool_name: str,
    arguments: Mapping[str, Any],
    state: CompositionState,
) -> CurrentAttachmentRead:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if (
        type(service) is not ComposerServiceImpl
        or type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or type(inventory.running) is not ComposerOperationRunning
        or type(state) is not CompositionState
    ):
        raise AuditIntegrityError("attachment read requires its actual current owners")
    selected.__post_init__()
    session_service = service._require_sessions_service()
    if (
        type(session_service) is not SessionServiceImpl
        or session_service._engine is not authority._engine
        or session_service._data_dir is None
    ):
        raise AuditIntegrityError("attachment read requires one actual session store and storage root")
    if tool_name not in {"read_attachment", "get_blob_content"}:
        raise AuditIntegrityError("current attachment owner received another selected tool")
    running = inventory.running
    sid = str(running.claim.session_id)
    if inventory.selection.snapshot_json != encode_continuation_snapshot(selected.stack):
        raise AuditIntegrityError("attachment read changed the complete selected root")
    # Finish the selected SQL transaction before taking BLOB_CUSTODY. The real
    # blob owner brackets its filesystem phase with exact-context metadata reads.
    with authority._engine.begin() as conn:
        names = require_current_env_inventory_on_connection(
            conn,
            running,
            inventory=inventory,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
        )
        actor = conn.execute(
            select(jobs.c.actor_user_id).where(jobs.c.session_id == sid, jobs.c.operation_id == running.claim.operation_id)
        ).scalar_one()
        principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at).where(sessions.c.id == sid)).one()
        if actor != principal.user_id or principal.archived_at is not None:
            raise AuditIntegrityError("attachment read changed its current session principal")
    snapshot, catalog = service._policy_context.build(actor)
    observations = ContinuationReadObservations()
    policy = AttachmentInspectionPolicy(READ_ATTACHMENT_MAX_UTF8_BYTES, env_ref_names=names)
    context = ToolContext(
        catalog=catalog,
        plugin_snapshot=snapshot,
        data_dir=str(session_service._data_dir),
        session_engine=authority._engine,
        session_id=sid,
        user_id=actor,
        session_operation_context=running.session_operation_context,
        session_operation_authority=session_service.session_operation_authority,
        attachment_inspection_policy=policy,
        continuation_read_observations=observations,
    )
    result = execute_discovery_tool_with_context(tool_name, arguments, state, context)
    retained = build_read_owner_result(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        pre_state=state,
        observations=observations,
        env_ref_names=names,
    )
    # Returned DATA cannot outlive a changed selection or immutable version.
    # Failed handler results retain completed inspections too, if any occurred.
    with authority._engine.begin() as conn:
        require_current_env_inventory_on_connection(
            conn,
            running,
            inventory=inventory,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
        )
        require_read_versions_on_connection(conn, session_id=sid, result=retained, env_ref_names=names)
    return CurrentAttachmentRead(result, retained)


async def read_current_attachment(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    tool_name: str,
    arguments: Mapping[str, Any],
    state: CompositionState,
    required_work: RequiredWorkBinding,
) -> CurrentAttachmentRead:
    if type(required_work) is not RequiredWorkBinding or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("attachment read requires its actual required-work inventory")
    running = required_work.running
    if type(running) is not ComposerOperationRunning or inventory.running is not running:
        raise AuditIntegrityError("attachment read changed its current running owner")
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("attachment read changed its required-work claim")
    captured_arguments = owned_json(arguments)
    if type(captured_arguments) is not dict:
        raise AuditIntegrityError("attachment read requires an argument object")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            _execute_current_attachment_read,
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            tool_name=tool_name,
            arguments=captured_arguments,
            state=state,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql,
            expected_source=RequiredWorkSource.CONTINUATION_READ_SQL,
            actual_outcome=outcome,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    failures: list[BaseException] = []
    actual: CurrentAttachmentRead | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        failures.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not CurrentAttachmentRead or outcome.value.result.updated_state is not state:
                raise AuditIntegrityError("attachment projection lost its exact result/pre-state")
            actual = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            failures.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        failures.append(AuditIntegrityError("attachment read returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in failures):
            failures.append(cancellation)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Continuation attachment read outcomes", failures)
    if actual is None:
        raise AuditIntegrityError("attachment read lost its owned result")
    return actual
