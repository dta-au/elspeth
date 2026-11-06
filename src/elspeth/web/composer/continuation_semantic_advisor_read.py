"""Execute an original paid Advisor read through its real discovery owner.

The caller first installs the prescribed action and prepares its SQL read
effect. This owner re-enters current admission, executes the real read-only
dispatcher outside a SQL transaction, and commits the original result and
action receipt under fresh admission in the same physical finish-once job.
It does not manufacture a PRIMARY response, tool-call ID or tool invocation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.async_workers import run_required_read_finish_once
from elspeth.web.composer.advisor_output import ReadContinuationAction
from elspeth.web.composer.attachment_admission import AttachmentInspectionPolicy
from elspeth.web.composer.continuation_result_credentials import require_retained_argument_credentials
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
from elspeth.web.composer.continuation_semantic_source import _plugin_snapshot_json
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredReadProduced, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_owner_results import ContinuationReadObservations, build_read_owner_result
from elspeth.web.sessions.continuation_semantic_actions import (
    SemanticActionAdmission,
    SemanticReadCommitted,
    commit_semantic_read_result_on_connection,
    require_semantic_read_execution_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


@dataclass(frozen=True, slots=True)
class SemanticAdvisorReadExecution:
    """Actual handler result and its SQL-issued retained result/action receipt."""

    result: ToolResult
    committed: SemanticReadCommitted


@dataclass(frozen=True, slots=True)
class _OriginalReadProduction:
    produced: RequiredReadProduced
    admission: SemanticActionAdmission


def _read_context(
    service: ComposerServiceImpl,
    runtime: RestoredContinuationRuntime,
    running: ComposerOperationRunning,
    admission: SemanticActionAdmission,
    observations: ContinuationReadObservations,
) -> ToolContext:
    raw = admission.paid.invocation.source
    state, bindings = runtime.live.state, runtime.bindings
    if (
        runtime.materialization.service is not service
        or bindings.session_operation_context is not running.session_operation_context
        or bindings.session_id != str(running.claim.session_id)
        or bindings.user_id != raw.actor_user_id
        or bindings.message != raw.original_user_message
        or canonical_json(state.to_dict()) != raw.state.canonical
        or runtime.env_ref_names != raw.env_ref_names
    ):
        raise AuditIntegrityError("semantic read changed its original principal, state or runtime context")
    snapshot, catalog = service._policy_context.build(raw.actor_user_id)
    if (
        snapshot.policy_hash != raw.plugin_policy_hash
        or snapshot.snapshot_hash != raw.catalog_snapshot_hash
        or _plugin_snapshot_json(snapshot) != raw.plugin_snapshot_json
        or bindings.plugin_snapshot.snapshot_hash != snapshot.snapshot_hash
        or bindings.policy_catalog.snapshot is not bindings.plugin_snapshot
    ):
        raise AuditIntegrityError("semantic read changed its original effective plugin policy")
    session_service = service._require_sessions_service()
    if not isinstance(session_service, SessionServiceImpl):
        raise AuditIntegrityError("semantic read requires its actual session service")
    return ToolContext(
        catalog=catalog,
        plugin_snapshot=snapshot,
        data_dir=service._data_dir,
        require_data_dir_for_paths=True,
        session_engine=session_service._engine,
        session_id=bindings.session_id,
        session_operation_context=running.session_operation_context,
        session_operation_authority=session_service.session_operation_authority,
        secret_service=service._secret_service,
        secret_wiring_policy=service._secret_wiring_policy,
        user_id=raw.actor_user_id,
        current_validation=runtime.live.last_validation,
        max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
        user_message_id=bindings.user_message_id,
        user_message_content=bindings.message,
        attachment_inspection_policy=AttachmentInspectionPolicy(
            service._settings.composer_attachment_inspection_max_bytes, env_ref_names=raw.env_ref_names
        ),
        continuation_read_observations=observations,
    )


def _produce_read(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    runtime: RestoredContinuationRuntime,
) -> _OriginalReadProduction:
    work = runtime.bindings.required_work
    if work is None or work.coordinator.sql_deadline_owner is None:
        raise AuditIntegrityError("semantic read lost its original bound action clock")
    clock_owner = work.coordinator.sql_deadline_owner
    original_clock = clock_owner.capture_read_result_deadline()
    sid = str(running.claim.session_id)
    with locked_session_transaction(authority._engine, sid) as conn:
        admission = require_semantic_read_execution_on_connection(conn, running, selected=selected, effect_key=effect_key)
        actor = conn.execute(
            select(jobs.c.actor_user_id).where(jobs.c.session_id == sid, jobs.c.operation_id == running.claim.operation_id)
        ).scalar_one()
        principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at).where(sessions.c.id == sid)).one()
        if actor != admission.paid.invocation.source.actor_user_id or actor != principal.user_id or principal.archived_at is not None:
            raise AuditIntegrityError("semantic read lost its current authenticated session principal")
    action = admission.action
    if type(action) is not ReadContinuationAction or admission.action_key is None or admission.owner_effect_key is None:
        raise AuditIntegrityError("semantic read has no original prepared read action")
    arguments, error = parse_json_strict(action.arguments_json)
    if error is not None or type(arguments) is not dict:
        raise AuditIntegrityError("semantic read lost its original argument object")
    names = admission.paid.invocation.source.env_ref_names
    require_retained_argument_credentials(action.tool_name, arguments, env_ref_names=names)
    observations = ContinuationReadObservations()
    context = _read_context(service, runtime, running, admission, observations)
    state = runtime.live.state
    if (
        clock_owner.capture_read_result_deadline() != original_clock
        or clock_owner.drain_signal.started_at is not None
        or clock_owner.remaining_dispatch(monotonic_now=time.monotonic()) <= 0
    ):
        raise TimeoutError("semantic read exhausted its original dispatch deadline before the handler")
    result = execute_discovery_tool_with_context(action.tool_name, arguments, state, context)
    retained = build_read_owner_result(
        tool_name=action.tool_name,
        arguments=arguments,
        result=result,
        pre_state=state,
        observations=observations,
        env_ref_names=names,
    ).model_copy(update={"prescribing_provider_call_id": admission.paid.audit_call.call_id})
    _read_context(service, runtime, running, admission, observations)
    return _OriginalReadProduction(RequiredReadProduced(result, retained, names), admission)


def _finish_read(
    *,
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    original: _OriginalReadProduction,
) -> SemanticAdvisorReadExecution:
    produced, admission = original.produced, original.admission
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        committed = commit_semantic_read_result_on_connection(
            conn, running, selected=selected, effect_key=effect_key, result=produced.owned_result
        )
    if (
        type(committed) is not SemanticReadCommitted
        or committed.original_result is not produced.owned_result
        or committed.owner_effect_key != admission.owner_effect_key
        or committed.receipt.action_key != admission.action_key
        or committed.receipt.owner_receipt_id != admission.owner_effect_key
        or committed.receipt.disposition != "committed"
    ):
        raise AuditIntegrityError("semantic read lost its actual committed result/action receipt")
    return SemanticAdvisorReadExecution(produced.result, committed)


async def execute_semantic_advisor_read(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    runtime: RestoredContinuationRuntime,
    required_work: RequiredWorkBinding,
) -> SemanticAdvisorReadExecution:
    """Join the real prepared read and its original result before cancellation escapes."""
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if type(service) is not ComposerServiceImpl:
        raise AuditIntegrityError("semantic read requires its actual composer service")
    session_service = service._require_sessions_service()
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(running) is not ComposerOperationRunning
        or type(selected) is not StoredContinuationSnapshot
        or type(runtime) is not RestoredContinuationRuntime
        or type(required_work) is not RequiredWorkBinding
        or required_work.running is not running
        or runtime.bindings.required_work is not required_work
        or runtime.materialization.authority is not authority
        or not isinstance(session_service, SessionServiceImpl)
        or session_service._engine is not authority._engine
    ):
        raise AuditIntegrityError("semantic read requires its actual current runtime/SQL/work owners")
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("semantic read changed its required-work claim")
    originals: list[_OriginalReadProduction] = []

    def produce() -> RequiredReadProduced:
        original = _produce_read(
            service=service,
            authority=authority,
            running=running,
            selected=selected,
            effect_key=effect_key,
            runtime=runtime,
        )
        originals.append(original)
        return original.produced

    def finish(produced: RequiredReadProduced) -> SemanticAdvisorReadExecution:
        if len(originals) != 1 or originals[0].produced is not produced:
            raise AuditIntegrityError("semantic read disposition substituted its actual physical return")
        return _finish_read(authority=authority, running=running, selected=selected, effect_key=effect_key, original=originals[0])

    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_READ_RESULT_SQL, RequiredWorkSource.CONTINUATION_READ_RESULT_PROJECTION
    )
    try:
        outcome = await run_required_read_finish_once(sql, produce, finish)
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_RESULT_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    failures: list[BaseException] = []
    actual: SemanticAdvisorReadExecution | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        failures.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not SemanticAdvisorReadExecution:
                raise AuditIntegrityError("semantic read projection lost its actual original outcome")
            actual = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            failures.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        failures.append(AuditIntegrityError("semantic read received a foreign SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not error for error in failures):
            failures.append(cancellation)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Semantic read original physical outcomes", failures)
    if actual is None:
        raise AuditIntegrityError("semantic read lost its actual returned outcome")
    return actual
