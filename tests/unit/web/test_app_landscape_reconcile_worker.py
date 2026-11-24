"""The pending Landscape reconciliation finalizes runs through the shared worker pool.

``_reconcile_pending_landscape_runs`` hands ``_finalize_orphaned_landscape_runs``
to ``run_sync_in_worker``. These tests pin that the finalizer runs on a worker
thread while the event loop keeps serving other tasks, that an exception it
raises reaches the caller unchanged (the periodic sweep retries only
``OperationalError``, so the exception class decides what the sweep does next),
and that a cancelled caller sees ``CancelledError`` while the finalizer
completes and no reconciliation outcome is recorded.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.web import app as app_module
from elspeth.web.sessions.protocol import RunRecord
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.worker_probe import WorkerProbe, run_with_probe

_LANDSCAPE_URL = "sqlite:///unused-landscape.db"


def _session_service(candidates: list[Any]) -> Mock:
    service = Mock(spec=SessionServiceImpl)
    service.list_pending_landscape_reconciliations = AsyncMock(
        spec=SessionServiceImpl.list_pending_landscape_reconciliations, return_value=candidates
    )
    service.mark_landscape_reconciliation_outcomes = AsyncMock(
        spec=SessionServiceImpl.mark_landscape_reconciliation_outcomes, return_value=None
    )
    return service


def _candidate() -> Mock:
    run = Mock(spec=RunRecord)
    run.id = uuid4()
    return run


@pytest.mark.asyncio
async def test_landscape_finalizer_runs_in_a_worker_while_the_loop_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _candidate()
    service = _session_service([candidate])
    complete: frozenset[UUID] = frozenset({candidate.id})
    absent: frozenset[UUID] = frozenset()
    received: list[tuple[str, list[Any], bool]] = []

    def finalize(url: str, runs: list[Any], *, create_tables: bool) -> tuple[frozenset[UUID], frozenset[UUID]]:
        received.append((url, runs, create_tables))
        return complete, absent

    probe = WorkerProbe()
    monkeypatch.setattr(app_module, "_finalize_orphaned_landscape_runs", probe.wrap(finalize))
    loop_thread = threading.get_ident()

    await run_with_probe(probe, app_module._reconcile_pending_landscape_runs(service, _LANDSCAPE_URL, create_tables=False))

    assert probe.entered.is_set()
    # True only if a loop task released the finalizer while it was running.
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads
    assert received == [(_LANDSCAPE_URL, [candidate], False)]
    service.mark_landscape_reconciliation_outcomes.assert_awaited_once_with(complete_run_ids=complete, absent_run_ids=absent)


@pytest.mark.asyncio
async def test_landscape_finalizer_operational_error_reaches_the_caller_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _session_service([_candidate()])
    failure = OperationalError("UPDATE runs", {}, RuntimeError("database is locked"))
    loop_thread = threading.get_ident()
    raising_threads: list[int] = []

    def finalize(_url: str, _runs: list[Any], *, create_tables: bool) -> tuple[frozenset[UUID], frozenset[UUID]]:
        del create_tables
        raising_threads.append(threading.get_ident())
        raise failure

    monkeypatch.setattr(app_module, "_finalize_orphaned_landscape_runs", finalize)

    with pytest.raises(OperationalError) as raised:
        await app_module._reconcile_pending_landscape_runs(service, _LANDSCAPE_URL, create_tables=False)

    assert raised.value is failure
    assert len(raising_threads) == 1
    assert loop_thread not in raising_threads
    service.mark_landscape_reconciliation_outcomes.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_landscape_reconcile_raises_cancelled_error_and_the_finalizer_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _session_service([_candidate()])

    def finalize(_url: str, _runs: list[Any], *, create_tables: bool) -> tuple[frozenset[UUID], frozenset[UUID]]:
        del create_tables
        return frozenset(), frozenset()

    probe = WorkerProbe()
    monkeypatch.setattr(app_module, "_finalize_orphaned_landscape_runs", probe.wrap(finalize))

    call = asyncio.ensure_future(app_module._reconcile_pending_landscape_runs(service, _LANDSCAPE_URL, create_tables=False))
    try:
        await probe.wait_entered()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        probe.released.set()
    await probe.wait_finished()

    assert probe.outcomes == [True]
    service.mark_landscape_reconciliation_outcomes.assert_not_awaited()
