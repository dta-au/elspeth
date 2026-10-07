"""Actual app handler503 and no-subset cancellation at the manual boundary."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from starlette.requests import Request
from structlog.testing import capture_logs

from elspeth.web import app as app_module
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.pipeline_commit import PipelineCommitError
from elspeth.web.sessions.manual_proposal_failure import ComposerManualProposalFailure, consume_manual_proposal_failure
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.routes.composer import pipeline_settlement, proposals
from elspeth.web.sessions.schemas import AcceptProposalRequest
from tests.unit.web.sessions.test_pipeline_rejection_callers import mounted_app, originals
from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
async def test_actual_app_manual_sql503_keeps_originals_correlation_and_redaction(operation_store, tmp_path, monkeypatch):
    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("controlled candidate validation failure", code="VALIDATION_FAILED")
    sql_error = OperationalError("SECRET_SQL", {"value": "SECRET_PARAMETER"}, RuntimeError("SECRET_DRIVER"))

    async def fail_candidate(**kwargs):
        raise body

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", fail_candidate)

    def fail_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise sql_error

    captured = []
    actual_consume = app_module.consume_manual_proposal_failure

    def observe(carrier):
        observation = actual_consume(carrier)
        captured.append(observation)
        return observation

    monkeypatch.setattr(app_module, "consume_manual_proposal_failure", observe)
    event.listen(engine, "before_cursor_execute", fail_sql)
    correlation = str(uuid4())
    try:
        with capture_logs() as logs:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    f"/api/sessions/{sid}/proposals/{expected.authority.row.id}/accept",
                    json={"draft_hash": expected.authority.proposal.draft_hash},
                    headers={"X-Request-ID": correlation},
                )
        assert response.status_code == 503
        assert response.json() == {
            "detail": "Database is currently unavailable. Please retry in a moment.",
            "error_type": "database_unavailable",
            "request_id": correlation,
        }
        assert len(captured) == 1 and captured[0].selected_original is sql_error
        assert captured[0].lease.closed
        assert any(leaf is body for leaf in originals(captured[0].original_root))
        assert any(leaf is sql_error for leaf in originals(captured[0].original_root))
        assert all(
            secret not in response.text and secret not in repr(logs) for secret in ("SECRET_SQL", "SECRET_PARAMETER", "SECRET_DRIVER")
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_failure", [False, True])
@pytest.mark.parametrize("arrival", ["before_release", "after_release_entered"])
async def test_actual_manual_close_outer_original_is_never_filtered_by_old_receipts(
    operation_store, tmp_path, monkeypatch, sql_failure, arrival
):
    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("actual body before manual rejection", code="VALIDATION_FAILED")
    sql_error = OperationalError("actual rejection CAS", {}, RuntimeError("actual driver"))

    async def fail_candidate(**kwargs):
        raise body

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", fail_candidate)
    entered, release = threading.Event(), threading.Event()
    close_entered = asyncio.Event()
    actual_close = proposals.close_required_proposal_lease
    close_originals = []

    async def observe_close(lease, *, coordinator):
        close_entered.set()
        errors = await actual_close(lease, coordinator=coordinator)
        close_originals.extend(errors)
        return errors

    monkeypatch.setattr(proposals, "close_required_proposal_lease", observe_close)

    def actual_fault_and_release_gate(_conn, _cursor, statement, _parameters, _context, _many):
        if sql_failure and statement.startswith("UPDATE composition_proposals"):
            raise sql_error
        if close_entered.is_set() and statement.startswith("UPDATE session_operation_fences"):
            assignments = tuple(
                part.strip().partition("=")[0].strip() for part in statement.partition(" WHERE ")[0].partition(" SET ")[2].split(",")
            )
            if assignments == ("lease_expires_at", "released_at"):
                entered.set()
                assert release.wait(5)

    event.listen(engine, "before_cursor_execute", actual_fault_and_release_gate)
    request = Request({"type": "http", "app": app, "state": {"request_id": str(uuid4())}})
    task = asyncio.create_task(
        proposals.accept_composition_proposal(
            session_id=sid,
            proposal_id=expected.authority.row.id,
            request=request,
            user=UserIdentity(user_id="alice", username="alice"),
            body=AcceptProposalRequest(draft_hash=expected.authority.proposal.draft_hash),
        )
    )
    try:
        async with asyncio.timeout(5):
            while not close_entered.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        if arrival == "after_release_entered":
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
        task.cancel("actual late manual cancellation")
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        if sql_failure:
            with pytest.raises(ComposerManualProposalFailure) as caught:
                await task
            observation = consume_manual_proposal_failure(caught.value)
            assert observation.selected_original is sql_error
            retained = originals(observation.original_root)
            assert observation.project(request_id=None, timeout_seconds=5).http_status == 503
        else:
            with pytest.raises(BaseExceptionGroup) as caught:
                await task
            retained = originals(caught.value)
        assert any(leaf is body for leaf in retained)
        assert len(close_originals) == 1
        assert isinstance(close_originals[0], asyncio.CancelledError)
        assert any(leaf is close_originals[0] for leaf in retained)
        with engine.connect() as conn:
            released_at = conn.execute(
                select(session_operation_fences_table.c.released_at).where(session_operation_fences_table.c.session_id == str(sid))
            ).scalar_one()
        assert released_at is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", actual_fault_and_release_gate)
