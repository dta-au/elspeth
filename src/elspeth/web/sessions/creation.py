"""Keep session creation and cancellation cleanup in one async lifetime."""

from __future__ import annotations

import asyncio

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.coordination.lifecycle import _join_shielded_task_after_cancellation
from elspeth.web.sessions.protocol import SessionRecord, SessionServiceProtocol


async def _archive_created_session_after_cancellation(
    service: SessionServiceProtocol, creation_task: asyncio.Task[SessionRecord]
) -> BaseException | None:
    await asyncio.wait({creation_task})
    # A failed creation has no session to archive; the caller still sees cancellation.
    if creation_task.cancelled() or creation_task.exception() is not None:
        return None
    session = creation_task.result()
    try:
        await service.archive_session(session.id)
    except BaseException as error:
        return error
    return None


async def create_session_with_cancellation_cleanup(
    service: SessionServiceProtocol, user_id: str, title: str, auth_provider_type: AuthProviderType
) -> SessionRecord:
    """Archive a session created after its caller was cancelled."""
    creation_task = asyncio.create_task(service.create_session(user_id, title, auth_provider_type), name="session-create")
    try:
        await asyncio.wait({creation_task})
        return creation_task.result()
    except asyncio.CancelledError as cancellation:
        cleanup_task = asyncio.create_task(
            _archive_created_session_after_cancellation(service, creation_task), name="session-cancelled-create-cleanup"
        )
        cleanup_error = await _join_shielded_task_after_cancellation(cleanup_task)
        del cleanup_task, creation_task
        if cleanup_error is None:
            raise
        raise cleanup_error from (cleanup_error.__cause__ if cleanup_error.__cause__ is not None else cancellation)
