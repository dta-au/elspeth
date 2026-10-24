"""The actual attachment B2 suffix followed by one current root transaction."""

from __future__ import annotations

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_attachment_finisher import finish_current_attachment_result
from elspeth.web.composer.continuation_attachment_history import decode_attachment_history
from elspeth.web.composer.continuation_attachment_result import _owners, _project
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.tool_batch import ToolBatchContext
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.continuation_attachment_application import StoredAttachmentApplication
from elspeth.web.sessions.continuation_runtime import runtime_chunk


async def read_current_applied_attachment(
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    effect_key: str,
) -> StoredAttachmentApplication:
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("attachment replay lost its actual current work owner")
    authority = runtime.materialization.authority
    running = _owners(authority, inventory, work)
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql, authority.read_applied_attachment, running, selected=selected, inventory=inventory, effect_key=effect_key
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(work, sql, projection, RequiredWorkSource.CONTINUATION_READ_SQL, outcome)
    if type(result) is not StoredAttachmentApplication:
        raise AuditIntegrityError("attachment replay lost its actual verified SQL application")
    return result


async def apply_current_attachment_runtime(
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    ctx: ToolBatchContext,
    call_model: _CallModelOutcome,
    effect_key: str,
) -> StoredAttachmentApplication:
    """Finish once in the live graph; commit once under the actual selected CAS.

    An uncertain/cancelled commit is reconciled by a new current selected read.
    The mutated old runtime is never an implicit retry cache. Warm callers keep
    their original context/registry/audit; cold callers explicitly readmit only
    the separately retained post-read audit at the original B2 seam.
    """
    if type(runtime) is not RestoredContinuationRuntime or type(ctx) is not ToolBatchContext:
        raise AuditIntegrityError("attachment application requires its actual complete runtime/context")
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("attachment application lost its actual required work")
    authority, service = runtime.materialization.authority, runtime.materialization.service
    running = _owners(authority, inventory, work)
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("attachment application lost its actual Compose owner")
    history = decode_attachment_history(
        runtime_chunk(selected, compose[0].attachment_admissions),
        session_id=selected.stack.logical.session_id,
        operation_id=selected.stack.logical.operation_id,
        env_ref_names=inventory.names,
    )
    if any(value.effect_key == effect_key for value in history.applications):
        # Only the actual current SQL verifier, not the decoded history branch,
        # can return the already-applied disposition.
        return await read_current_applied_attachment(runtime=runtime, selected=selected, inventory=inventory, effect_key=effect_key)
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame or effect_key != f"attachment:{frame.response.digest}:{frame.next_tool_index}":
        raise AuditIntegrityError("attachment application selected another exact result identity")
    completed = await finish_current_attachment_result(
        service=service,
        authority=authority,
        selected=selected,
        inventory=inventory,
        required_work=work,
        ctx=ctx,
        call_model=call_model,
        restored=runtime.graph,
    )
    if completed.source.effect_key != effect_key:
        raise AuditIntegrityError("attachment application changed the exact requested result identity")
    captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.apply_attachment_application,
            running,
            selected=selected,
            inventory=inventory,
            projection=completed,
            captured=captured,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, outcome)
    if type(result) is not StoredAttachmentApplication:
        raise AuditIntegrityError("attachment application lost its actual atomic returned owner")
    return result
