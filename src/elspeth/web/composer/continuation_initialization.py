"""Publish complete original SendMessage bootstrap before any TITLE work.

This bounded owner ends at title_pending/ready. It does not dispatch TITLE,
enter Compose, implement Recompose, or enable the continuation worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.composer.continuation_bootstrap import capture_continuation_bootstrap, encode_continuation_bootstrap
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.composer_turn import _read_retained_operation_context
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_bootstrap_source import (
    PreparedPretitleBootstrap,
    StoredPretitleBootstrap,
    publish_pretitle_bootstrap_on_connection,
    read_pretitle_bootstrap_on_connection,
    require_pretitle_inserted_origin,
)
from elspeth.web.sessions.continuation_title_source import ContinuationTitleSource, read_title_source_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.routes._helpers import _join_freeform_owned_task
from elspeth.web.sessions.service import SessionServiceImpl


def _sessions(service: ComposerServiceImpl) -> SessionServiceImpl:
    sessions = service._require_sessions_service()
    if type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("pre-title bootstrap requires the actual sessions owner")
    return sessions


def _read_inserted(
    service: ComposerServiceImpl, running: ComposerOperationRunning, selected: StoredContinuationSnapshot
) -> ContinuationTitleSource:
    sessions = _sessions(service)
    with locked_session_transaction(sessions._engine, str(running.claim.session_id)) as conn:
        source = read_title_source_on_connection(conn, sessions, running, selected=selected)
        require_pretitle_inserted_origin(conn, running, selected, generation=running.claim.ownership_generation)
        return source


def _read_published(
    service: ComposerServiceImpl, running: ComposerOperationRunning, selected: StoredContinuationSnapshot
) -> StoredPretitleBootstrap:
    sessions = _sessions(service)
    with locked_session_transaction(sessions._engine, str(running.claim.session_id)) as conn:
        return read_pretitle_bootstrap_on_connection(conn, sessions, running, selected=selected)


def _publish(
    service: ComposerServiceImpl, running: ComposerOperationRunning, prepared: PreparedPretitleBootstrap
) -> StoredPretitleBootstrap:
    sessions = _sessions(service)
    with locked_session_transaction(sessions._engine, str(running.claim.session_id)) as conn:
        return publish_pretitle_bootstrap_on_connection(conn, service, running, prepared=prepared)


async def _registered[T](*, work: RequiredWorkBinding, source: RequiredWorkSource, operation: Callable[[], T], expected: type[T]) -> T:
    projection_source = (
        RequiredWorkSource.CONTINUATION_READ_PROJECTION
        if source is RequiredWorkSource.CONTINUATION_READ_SQL
        else RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    if source not in (RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_STATE_SQL):
        raise AuditIntegrityError("pre-title bootstrap has no registered SQL source")
    sql, projection = work.reserve_pair(source, projection_source)
    try:
        outcome = await run_required_sql_finish_once(sql, operation)
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        work.coordinator.validate_continuation_sql_outcome(ticket=sql, expected_source=source, actual_outcome=outcome)
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    result: T | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not expected:
                raise AuditIntegrityError("pre-title bootstrap SQL lost its actual nominal outcome")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("pre-title bootstrap SQL returned no owned finish-once outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Pre-title bootstrap original SQL outcomes", errors)
    if result is None:
        raise AuditIntegrityError("pre-title bootstrap projection lost its complete result")
    return result


async def _prepare_skill_history(service: ComposerServiceImpl, work: RequiredWorkBinding) -> None:
    async def owned() -> BaseException | None:
        producer = work.coordinator.reserve(
            RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER,
            transition_ordinal=work.transition_ordinal,
            semantic_ordinal=work.semantic_ordinal,
        )
        try:
            await service._maybe_upsert_skill_markdown_history()
        except BaseException as error:
            producer.complete_owned(error)
            return error
        producer.complete_owned()
        return None

    coroutine = owned()
    try:
        task = asyncio.create_task(coroutine, name="continuation-pretitle-skill-history")
    except BaseException:
        coroutine.close()
        raise
    cancellations: list[asyncio.CancelledError] = []
    failure, _ = await _join_freeform_owned_task(task, cancellation_observations=cancellations)
    errors: list[BaseException] = [] if failure is None else [failure]
    for cancellation in cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Pre-title skill preparation original outcomes", errors)


def _initial_disclosure(service: ComposerServiceImpl, session_id: str) -> tuple[tuple[Literal["source", "transform", "sink"], str], ...]:
    result: list[tuple[Literal["source", "transform", "sink"], str]] = []
    for kind, name in sorted(service._schema_disclosure.schemas_loaded_for_session(session_id)):
        if type(name) is not str:
            raise AuditIntegrityError("pre-title disclosure lost its actual plugin name")
        match kind:
            case "source":
                result.append(("source", name))
            case "transform":
                result.append(("transform", name))
            case "sink":
                result.append(("sink", name))
            case _:
                raise AuditIntegrityError("pre-title disclosure has no admitted plugin kind")
    return tuple(result)


async def prepare_pretitle_bootstrap(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    required_work: RequiredWorkBinding,
) -> StoredPretitleBootstrap:
    if (
        type(service) is not ComposerServiceImpl
        or type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not StoredContinuationSnapshot
        or type(required_work) is not RequiredWorkBinding
    ):
        raise AuditIntegrityError("pre-title bootstrap requires nominal live owners")
    running = required_work.running
    custody = required_work.coordinator.authority
    if (
        type(running) is not ComposerOperationRunning
        or custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
        or _sessions(service)._engine is not authority._engine
    ):
        raise AuditIntegrityError("pre-title bootstrap changed its actual current claim/store")
    required_work.validate_context(running.session_operation_context)
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if len(root.frames) != 1 or type(frame) is not InitializeFrame or root.logical.kind != "compose_message":
        raise AuditIntegrityError("pre-title bootstrap has no implemented SendMessage Initialize owner")
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    clock = ContinuationActionClock(policy.actions.session_sql)
    clock.finish_preparation()
    if frame.ingress_phase in ("title_pending", "ready"):

        async def replay() -> StoredPretitleBootstrap:
            return await _registered(
                work=required_work,
                source=RequiredWorkSource.CONTINUATION_READ_SQL,
                operation=lambda: _read_published(service, running, selected),
                expected=StoredPretitleBootstrap,
            )

        return await clock.run("physical", replay)
    if frame.ingress_phase != "inserted":
        raise AuditIntegrityError("pre-title bootstrap requires completed inserted ingress")

    async def prepare() -> PreparedPretitleBootstrap:
        skill_identity = (service._composer_skill_name, service._composer_skill_hash, service._composer_skill_text)
        source = await _registered(
            work=required_work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=lambda: _read_inserted(service, running, selected),
            expected=ContinuationTitleSource,
        )
        if source.initial_state is None or source.recovery.context.malformed_head:
            raise AuditIntegrityError("pre-title bootstrap requires the existing graph recovery owner")
        current = await load_selected_continuation(authority=authority, running=running, selected=selected, required_work=required_work)
        first = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint,
            authority=authority,
            selected=current,
            required_work=required_work,
        )
        require_no_credential_material_in_state(
            source.initial_state, surface="continuation pre-title initial state", env_ref_names=first.names
        )
        retained = await _read_retained_operation_context(
            authority=authority,
            binding=required_work,
            session_id=running.claim.session_id,
            actor_user_id=source.actor_user_id,
        )
        await _prepare_skill_history(service, required_work)
        final = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint,
            authority=authority,
            selected=current,
            required_work=required_work,
        )
        if first.names != final.names:
            raise AuditIntegrityError("pre-title bootstrap names changed across preparation awaits")
        disclosure = _initial_disclosure(service, root.logical.session_id)
        snapshot = capture_continuation_bootstrap(
            logical=root.logical,
            actor_user_id=source.actor_user_id,
            request=source.request,
            records=source.records,
            initial_state=source.initial_state,
            recovery_context=source.recovery.context,
            completion_gates=source.recovery.effective_completion_gates,
            retained_context=retained,
            session_title=source.original_session_title,
            initial_schema_disclosure=disclosure,
        )
        return PreparedPretitleBootstrap(source, final, encode_continuation_bootstrap(snapshot, env_ref_names=final.names), skill_identity)

    prepared = await clock.run("physical", prepare)

    async def retain() -> StoredPretitleBootstrap:
        return await _registered(
            work=required_work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=lambda: _publish(service, running, prepared),
            expected=StoredPretitleBootstrap,
        )

    return await clock.run("retention", retain)
