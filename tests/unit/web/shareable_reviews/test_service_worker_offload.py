"""Shareable-review sessions-DB work runs through the shared worker pool.

``mark_ready_for_review`` hands its fenced ``mutate`` call, and
``get_shareable_link`` hands ``_latest_mark_ready_event``, to
``run_sync_in_worker``. For each call these tests pin that it runs on a worker
thread while the event loop keeps serving other tasks, that an exception
raised by the worker call reaches the caller unchanged, that a cancelled caller
sees ``CancelledError`` while the worker call runs to completion, and that a
draining instance refuses the call with ``RequiredGenerationUnavailable``.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web import async_workers
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.models import composer_completion_events_table
from elspeth.web.sessions.telemetry import observed_value
from elspeth.web.shareable_reviews.service import CompositionNotRunnableError, ShareableReviewService
from tests.helpers.worker_probe import WorkerProbe, run_with_probe
from tests.unit.web.shareable_reviews.test_service import (
    _OWNER_USERNAME,
    _build_service,
    _ok_validation,
    _readiness_snapshot,
    _SessionRecord,
)
from tests.unit.web.shareable_reviews.test_service import engine as engine
from tests.unit.web.shareable_reviews.test_service import payload_store as payload_store
from tests.unit.web.shareable_reviews.test_service import session_engine_with_row as session_engine_with_row
from tests.unit.web.shareable_reviews.test_service import session_id as session_id
from tests.unit.web.shareable_reviews.test_service import session_operation_context as session_operation_context
from tests.unit.web.shareable_reviews.test_service import session_record as session_record
from tests.unit.web.shareable_reviews.test_service import signer as signer
from tests.unit.web.shareable_reviews.test_service import state_id as state_id
from tests.unit.web.shareable_reviews.test_service import state_record as state_record


@dataclass(frozen=True, slots=True)
class _Harness:
    """A shareable-review service over a sessions database holding one session and its state."""

    service: ShareableReviewService
    db: Any
    store: FilesystemPayloadStore
    session: _SessionRecord
    context: SessionOperationContext


@pytest.fixture
def harness(request: pytest.FixtureRequest) -> _Harness:
    db = request.getfixturevalue("session_engine_with_row")
    store = request.getfixturevalue("payload_store")
    session = request.getfixturevalue("session_record")
    service, *_ = _build_service(
        engine=db,
        payload_store=store,
        signer=request.getfixturevalue("signer"),
        session_record=session,
        state_record=request.getfixturevalue("state_record"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session.id),
    )
    return _Harness(service=service, db=db, store=store, session=session, context=request.getfixturevalue("session_operation_context"))


def _completion_rows(h: _Harness) -> list[Any]:
    with h.db.connect() as conn:
        return list(
            conn.execute(
                select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(h.session.id))
            ).all()
        )


# ── mark_ready_for_review: the fenced mutate ────────────────────────────


@pytest.mark.asyncio
async def test_mark_ready_audit_write_runs_in_a_worker_while_the_loop_runs(harness: _Harness) -> None:
    service = harness.service
    probe = WorkerProbe()
    loop_thread = threading.get_ident()

    with patch.object(SQLiteLocalSessionOperationAuthority, "mutate", probe.wrap(SQLiteLocalSessionOperationAuthority.mutate)):
        response = await run_with_probe(
            probe,
            service.mark_ready_for_review(
                session_id=harness.session.id,
                user_id=harness.session.user_id,
                username=_OWNER_USERNAME,
                session_operation_context=harness.context,
            ),
        )

    assert probe.entered.is_set()
    # True only if a loop task released the call while it was running.
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads
    assert response.token
    assert [row.event_type for row in _completion_rows(harness)] == ["mark_ready_for_review"]


@pytest.mark.asyncio
async def test_mark_ready_custody_error_raised_in_the_worker_still_maps_to_readiness_state_drift(harness: _Harness) -> None:
    """The route maps this ``CompositionNotRunnableError`` reason to 409 ``readiness_state_drift``."""
    service = harness.service
    loop_thread = threading.get_ident()
    raising_threads: list[int] = []

    def refuse_custody(self: SQLiteLocalSessionOperationAuthority, context: SessionOperationContext, mutation: Any) -> None:
        del self, context, mutation
        raising_threads.append(threading.get_ident())
        raise SessionDerivedCustodyError()

    with (
        patch.object(SQLiteLocalSessionOperationAuthority, "mutate", refuse_custody),
        pytest.raises(CompositionNotRunnableError) as raised,
    ):
        await service.mark_ready_for_review(
            session_id=harness.session.id,
            user_id=harness.session.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=harness.context,
        )

    assert raised.value.reason == "readiness_state_drift"
    assert len(raising_threads) == 1
    assert loop_thread not in raising_threads
    assert _completion_rows(harness) == []
    assert observed_value(service._telemetry.session_completed_total) == 0


@pytest.mark.asyncio
async def test_cancelled_mark_ready_raises_cancelled_error_and_the_worker_write_commits(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller sees ``CancelledError``; the running worker call still commits its audit row.

    The context is still live, so the fenced write succeeds after the caller
    has gone. Nothing after the cancelled await runs: no snapshot blob is
    stored and no completion telemetry is recorded.
    """
    service = harness.service
    probe = WorkerProbe()
    stored: list[bytes] = []
    monkeypatch.setattr(harness.store, "store", lambda content: stored.append(content))

    with patch.object(SQLiteLocalSessionOperationAuthority, "mutate", probe.wrap(SQLiteLocalSessionOperationAuthority.mutate)):
        call = asyncio.ensure_future(
            service.mark_ready_for_review(
                session_id=harness.session.id,
                user_id=harness.session.user_id,
                username=_OWNER_USERNAME,
                session_operation_context=harness.context,
            )
        )
        try:
            await probe.wait_entered()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            probe.released.set()
        await probe.wait_finished()

    assert probe.outcomes == [True]
    assert [row.event_type for row in _completion_rows(harness)] == ["mark_ready_for_review"]
    assert stored == []
    assert observed_value(service._telemetry.session_completed_total) == 0


@pytest.mark.asyncio
async def test_mark_ready_audit_write_is_refused_while_the_instance_drains(harness: _Harness) -> None:
    """The app maps ``RequiredGenerationUnavailable`` to 503 ``database_unavailable``."""
    service = harness.service
    async_workers._INSTANCE_DRAINING.set()

    with pytest.raises(RequiredGenerationUnavailable):
        await service.mark_ready_for_review(
            session_id=harness.session.id,
            user_id=harness.session.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=harness.context,
        )

    assert _completion_rows(harness) == []
    assert observed_value(service._telemetry.session_completed_total) == 0


# ── get_shareable_link: the latest mark-ready read ──────────────────────


@pytest.mark.asyncio
async def test_shareable_link_event_read_runs_in_a_worker_while_the_loop_runs(harness: _Harness) -> None:
    service = harness.service
    probe = WorkerProbe()
    loop_thread = threading.get_ident()

    with (
        patch.object(ShareableReviewService, "_latest_mark_ready_event", probe.wrap(ShareableReviewService._latest_mark_ready_event)),
        pytest.raises(CompositionNotRunnableError) as raised,
    ):
        await run_with_probe(probe, service.get_shareable_link(session_id=harness.session.id, user_id=harness.session.user_id))

    # No mark-ready event exists, so the read returns None and the link is refused.
    assert raised.value.reason == "completion_event_missing"
    assert probe.entered.is_set()
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads


@pytest.mark.asyncio
async def test_shareable_link_event_read_error_raised_in_the_worker_reaches_the_caller_unchanged(harness: _Harness) -> None:
    service = harness.service
    loop_thread = threading.get_ident()
    raising_threads: list[int] = []
    failure = OperationalError("SELECT composer_completion_events", {}, RuntimeError("database is locked"))

    def failing_read(self: ShareableReviewService, **_keywords: object) -> None:
        del self
        raising_threads.append(threading.get_ident())
        raise failure

    with (
        patch.object(ShareableReviewService, "_latest_mark_ready_event", failing_read),
        pytest.raises(OperationalError) as raised,
    ):
        await service.get_shareable_link(session_id=harness.session.id, user_id=harness.session.user_id)

    assert raised.value is failure
    assert len(raising_threads) == 1
    assert loop_thread not in raising_threads


@pytest.mark.asyncio
async def test_cancelled_shareable_link_raises_cancelled_error_and_the_worker_read_completes(harness: _Harness) -> None:
    service = harness.service
    probe = WorkerProbe()

    with patch.object(ShareableReviewService, "_latest_mark_ready_event", probe.wrap(ShareableReviewService._latest_mark_ready_event)):
        call = asyncio.ensure_future(service.get_shareable_link(session_id=harness.session.id, user_id=harness.session.user_id))
        try:
            await probe.wait_entered()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            probe.released.set()
        await probe.wait_finished()

    assert probe.outcomes == [True]


@pytest.mark.asyncio
async def test_shareable_link_event_read_is_refused_while_the_instance_drains(harness: _Harness) -> None:
    """The app maps ``RequiredGenerationUnavailable`` to 503 ``database_unavailable``."""
    service = harness.service
    async_workers._INSTANCE_DRAINING.set()

    with pytest.raises(RequiredGenerationUnavailable):
        await service.get_shareable_link(session_id=harness.session.id, user_id=harness.session.user_id)
