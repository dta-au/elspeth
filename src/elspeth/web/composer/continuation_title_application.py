"""Apply one already paid/settled TITLE SUCCESS; no provider or Compose entry."""

from __future__ import annotations

import asyncio

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import _read_current_env_names
from elspeth.web.composer.continuation_initialization import _registered, _sessions
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_title_provider import _running
from elspeth.web.composer.continuation_title_values import decode_title_provider_result
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions._auto_title import _join_auto_title_custody_task, _record_auto_title_rejection
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_title_application import (
    CurrentTitleApplication,
    CurrentTitleApplicationNames,
    TitleApplicationRequest,
    read_title_application_on_connection,
    require_current_title_application,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.protocol import SessionRecord


def _read_selected(
    service: ComposerServiceImpl, running: ComposerOperationRunning, expected: StoredContinuationSnapshot | None
) -> StoredContinuationSnapshot:
    with locked_session_transaction(_sessions(service)._engine, str(running.claim.session_id)) as conn:
        selected = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
        require_current_title_application(conn, running, selected if expected is None else expected)
        return selected


async def read_current_title_application(
    *,
    service: ComposerServiceImpl,
    running: ComposerOperationRunning,
    required_work: RequiredWorkBinding,
    selected: StoredContinuationSnapshot | None = None,
) -> CurrentTitleApplication:
    if type(service) is not ComposerServiceImpl or _running(required_work) is not running:
        raise AuditIntegrityError("TITLE application requires its actual current service/work owner")
    before = await _registered(
        work=required_work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _read_selected(service, running, selected),
        expected=StoredContinuationSnapshot,
    )
    names = await _read_current_env_names(service._advisor_checkpoint, str(running.claim.session_id))
    after = await _registered(
        work=required_work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _read_selected(service, running, before),
        expected=StoredContinuationSnapshot,
    )
    if before != after:
        raise AuditIntegrityError("TITLE application names changed its complete current root")
    inventory = CurrentTitleApplicationNames(running, after, names)

    def read() -> CurrentTitleApplication:
        with locked_session_transaction(_sessions(service)._engine, str(running.claim.session_id)) as conn:
            return read_title_application_on_connection(conn, running, inventory=inventory)

    return await _registered(
        work=required_work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=CurrentTitleApplication
    )


async def apply_current_title_success(
    *, service: ComposerServiceImpl, selected: StoredContinuationSnapshot, required_work: RequiredWorkBinding
) -> CurrentTitleApplication:
    """One retained session-SQL action, independently of the completed TITLE clock."""
    running = _running(required_work)
    if type(service) is not ComposerServiceImpl or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("TITLE application lost its actual nominal current source")
    root = decode_continuation_snapshot(selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    clock = ContinuationActionClock(policy.actions.session_sql)
    current = await read_current_title_application(service=service, running=running, required_work=required_work, selected=selected)
    clock.finish_preparation()
    if current.applied:
        return current
    request = TitleApplicationRequest(running, current.selected, current.inventory, current.result, current.decision)

    async def write() -> None:
        sql, projection = required_work.reserve_pair(
            RequiredWorkSource.TITLE_ACCOUNTING_SQL, RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION
        )
        coroutine = _sessions(service).apply_continuation_title_application(request, required_work=sql)
        try:
            child = asyncio.create_task(coroutine, name="continuation-title-application")
        except BaseException:
            coroutine.close()
            sql.complete_without_submission()
            projection.complete_without_submission()
            raise
        try:
            record, cancellation = await _join_auto_title_custody_task(
                child, child_cancel_message="Retained TITLE application was cancelled before its actual durable completion"
            )
        except BaseException:
            if sql.complete:
                projection.complete_without_submission()
            raise
        projection.begin_projection()
        try:
            if type(record) is not SessionRecord or not sql.complete:
                raise AuditIntegrityError("TITLE application returned another or unfinished actual title owner")
            candidate = current.decision.candidate
            if candidate is not None and record.title != candidate:
                raise AuditIntegrityError("TITLE application returned another actual accepted title")
        except BaseException as error:
            projection.complete_owned(error)
            if cancellation is not None:
                raise error from cancellation
            raise
        projection.complete_owned()
        if cancellation is not None:
            raise cancellation

    await clock.run("physical", write)

    async def observe() -> CurrentTitleApplication:
        return await read_current_title_application(service=service, running=running, required_work=required_work)

    published = await clock.run("retention", observe)
    if not published.applied or published.result != current.result or published.decision != current.decision:
        raise AuditIntegrityError("TITLE application lost its committed complete ready result")
    if published.decision.disposition == "rejected":
        rejection = published.decision.rejection_class
        if rejection is None:
            raise AuditIntegrityError("TITLE rejection lost its actual gate classification")
        result = decode_title_provider_result(published.result, env_ref_names=published.inventory.names)
        _record_auto_title_rejection(rejection, result.completion.provider_metadata.finish_reason)
    return published
