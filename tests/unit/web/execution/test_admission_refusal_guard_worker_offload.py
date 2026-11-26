"""Admission-refusal cleanup reproves its EXECUTE lease through the shared worker pool.

``_settle_admission_refusal.finish_cleanup`` hands ``lease.guard_external_effect``
to ``run_sync_in_worker`` before finalizing run output blobs. The Landscape
database in these tests holds no row for the run, so ``reconcile_landscape``
schedules ``finish_cleanup`` directly and that guard is the only
compare-and-swap after the lease is adopted. These tests pin that the guard runs
on a worker thread while the event loop keeps serving other tasks, that
``SessionOperationFenceLost`` raised by it reaches the caller unchanged before
any cleanup runs, and that cancelling the caller raises ``CancelledError`` while
the shielded reconciliation, guard included, runs to completion.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any
from unittest.mock import create_autospec, patch
from uuid import uuid4

import pytest

from elspeth.contracts.blobs import BlobFinalizationResult
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.landscape.database import LandscapeDB
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from tests.helpers.session_fences import RecordingSessionOperationAuthority, adopt_execute_lease, close_adopted_lease
from tests.helpers.worker_probe import WorkerProbe, run_with_probe
from tests.unit.web.execution.test_service import broadcaster as broadcaster
from tests.unit.web.execution.test_service import execution_fixture as execution_fixture
from tests.unit.web.execution.test_service import mock_loop as mock_loop
from tests.unit.web.execution.test_service import mock_session_service as mock_session_service
from tests.unit.web.execution.test_service import mock_settings as mock_settings
from tests.unit.web.execution.test_service import real_loop as real_loop
from tests.unit.web.execution.test_service import service as service

_OPEN_LANDSCAPE = "elspeth.web.execution.service.open_landscape_db"


class _ObservedRecordingAuthority(RecordingSessionOperationAuthority):
    """Recording authority that, once armed, records each compare-and-swap thread and can run a probe first."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.probe: WorkerProbe | None = None
        self.cas_threads: list[int] = []

    def compare_and_swap(self, context: SessionOperationContext) -> SessionOperationContext:
        recorded: Callable[[SessionOperationContext], SessionOperationContext] = super().compare_and_swap
        if not self.armed:
            return recorded(context)
        self.cas_threads.append(threading.get_ident())
        if self.probe is None:
            return recorded(context)
        return self.probe.wrap(recorded)(context)


def _blob_service(execution_service: Any) -> Any:
    blobs = create_autospec(BlobServiceImpl, instance=True)
    blobs.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
    execution_service._blob_service = blobs
    return blobs


def test_admission_refusal_cleanup_guard_runs_in_a_worker_while_the_loop_runs(request: pytest.FixtureRequest) -> None:
    execution_service = request.getfixturevalue("service")
    sessions = request.getfixturevalue("mock_session_service")
    loop = request.getfixturevalue("real_loop")
    authority = _ObservedRecordingAuthority()
    lease = adopt_execute_lease(loop, uuid4(), authority)
    probe = WorkerProbe()
    loop_thread = threading.get_ident()  # run_until_complete runs the loop on this thread
    try:
        authority.probe = probe
        authority.armed = True
        with LandscapeDB.in_memory() as db, patch(_OPEN_LANDSCAPE, return_value=nullcontext(db)):
            blobs = _blob_service(execution_service)
            loop.run_until_complete(run_with_probe(probe, execution_service._settle_admission_refusal(uuid4(), lease)))
    finally:
        authority.armed = False
        authority.probe = None
        close_adopted_lease(loop, lease)

    assert probe.entered.is_set()
    # True only if a loop task released the guard while it was running.
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads
    blobs.finalize_run_output_blobs.assert_awaited_once()
    sessions.session_operation_authority.mutate.assert_called_once()


def test_admission_refusal_cleanup_guard_refusal_reaches_the_caller_before_any_cleanup(request: pytest.FixtureRequest) -> None:
    execution_service = request.getfixturevalue("service")
    sessions = request.getfixturevalue("mock_session_service")
    loop = request.getfixturevalue("real_loop")
    authority = _ObservedRecordingAuthority()
    lease = adopt_execute_lease(loop, uuid4(), authority)
    guard_error = SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
    loop_thread = threading.get_ident()
    try:
        authority.compare_and_swap_error = guard_error
        authority.armed = True
        with LandscapeDB.in_memory() as db, patch(_OPEN_LANDSCAPE, return_value=nullcontext(db)):
            blobs = _blob_service(execution_service)
            with pytest.raises(SessionOperationFenceLost) as raised:
                loop.run_until_complete(execution_service._settle_admission_refusal(uuid4(), lease))
    finally:
        authority.armed = False
        authority.compare_and_swap_error = None
        try:
            close_adopted_lease(loop, lease)
        except SessionOperationFenceLost as closing:
            # The lease reports the failure of the child task it owns when it closes.
            assert closing is guard_error

    assert raised.value is guard_error
    assert len(authority.cas_threads) == 1
    assert loop_thread not in authority.cas_threads
    blobs.finalize_run_output_blobs.assert_not_awaited()
    sessions.session_operation_authority.mutate.assert_not_called()


def test_cancelled_admission_refusal_raises_cancelled_error_and_the_shielded_cleanup_finishes(request: pytest.FixtureRequest) -> None:
    execution_service = request.getfixturevalue("service")
    sessions = request.getfixturevalue("mock_session_service")
    loop = request.getfixturevalue("real_loop")
    authority = _ObservedRecordingAuthority()
    lease = adopt_execute_lease(loop, uuid4(), authority)
    probe = WorkerProbe()

    async def cancel_while_the_guard_runs() -> None:
        settle = asyncio.ensure_future(execution_service._settle_admission_refusal(uuid4(), lease))
        try:
            await probe.wait_entered()
            settle.cancel()
            with pytest.raises(asyncio.CancelledError):
                await settle
        finally:
            probe.released.set()

    try:
        authority.probe = probe
        authority.armed = True
        with LandscapeDB.in_memory() as db, patch(_OPEN_LANDSCAPE, return_value=nullcontext(db)):
            blobs = _blob_service(execution_service)
            loop.run_until_complete(cancel_while_the_guard_runs())
            # Closing joins the shielded reconciliation task the lease owns,
            # so the database stays open until that task has finished.
            close_adopted_lease(loop, lease)
    finally:
        authority.armed = False
        authority.probe = None
        if not lease.closed:
            close_adopted_lease(loop, lease)

    assert probe.outcomes == [True]
    blobs.finalize_run_output_blobs.assert_awaited_once()
    sessions.session_operation_authority.mutate.assert_called_once()
