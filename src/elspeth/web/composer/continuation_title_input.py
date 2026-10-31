"""Registered current inserted-source read plus original ephemeral title input.

This adapter grants no TITLE dispatch, durable phase transition or bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions._auto_title import PreparedAutoTitleRequest, prepare_auto_title_request
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_title_source import ContinuationTitleSource, read_title_source_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class CurrentContinuationTitleInput:
    source: ContinuationTitleSource
    request: PreparedAutoTitleRequest | None


def _read_current_title_input(
    service: ComposerServiceImpl,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
) -> CurrentContinuationTitleInput:
    sessions = service._require_sessions_service()
    if type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("current title input requires the actual sessions SQL owner")
    with locked_session_transaction(sessions._engine, str(running.claim.session_id)) as conn:
        source = read_title_source_on_connection(conn, sessions, running, selected=selected)
        settings = service._settings
        request = None
        if source.eligible:
            request = prepare_auto_title_request(
                user_message=source.request.content,
                model=settings.composer_model,
                temperature=settings.composer_temperature,
                seed=settings.composer_seed,
                api_base=settings.composer_endpoint_base_url,
                api_key=settings.composer_endpoint_api_key.get_secret_value() if settings.composer_endpoint_api_key is not None else None,
            )
        return CurrentContinuationTitleInput(source, request)


async def load_current_title_input(
    *,
    service: ComposerServiceImpl,
    selected: StoredContinuationSnapshot,
    required_work: RequiredWorkBinding,
) -> CurrentContinuationTitleInput:
    if type(service) is not ComposerServiceImpl or type(required_work) is not RequiredWorkBinding:
        raise AuditIntegrityError("current title input requires nominal live service/work owners")
    running = required_work.running
    authority = required_work.coordinator.authority
    if (
        type(running) is not ComposerOperationRunning
        or type(selected) is not StoredContinuationSnapshot
        or authority.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or authority.durable_operation_id != running.claim.operation_id
        or authority.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("current title input changed its actual durable current claim")
    required_work.validate_context(running.session_operation_context)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, _read_current_title_input, service, running, selected)
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
    result: CurrentContinuationTitleInput | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not CurrentContinuationTitleInput:
                raise AuditIntegrityError("current title SQL lost its actual complete source/input")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("current title read returned no owned finish-once outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not error for error in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Current title source original outcomes", errors)
    if result is None:
        raise AuditIntegrityError("current title source projection lost its result")
    return result
