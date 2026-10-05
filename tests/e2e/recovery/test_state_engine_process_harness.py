"""Independent-process crash controls for the state-engine recovery harness."""

from __future__ import annotations

import faulthandler
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts import RunStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import token_work_items_table
from elspeth.engine.clock import MockClock
from tests.e2e.recovery.harness import (
    _DEFAULT_LEASE_SECONDS,
    _T0,
    SpawnedProcessAtSeam,
    _craft_crashed_lease,
    _resume,
    _run_to_interrupted_checkpoint,
    spawn_database_process_at_seam,
    spawn_database_process_with_pause,
)
from tests.fixtures.landscape import expire_lease, member_token_for
from tests.helpers.process_diagnostics import ProcessDiagnostics, trace_child_process

_PROCESS_TIMEOUT_SECONDS = 20.0


def _heartbeat_crashed_lease(
    db: LandscapeDB,
    run_id: str,
    work_item_id: str,
    worker_id: str,
    now_iso: str,
) -> None:
    """Child action: cross the production heartbeat boundary, then pause."""
    RecorderFactory(db).scheduler.heartbeat_lease(
        work_item_id=work_item_id,
        lease_owner=worker_id,
        lease_seconds=2 * _DEFAULT_LEASE_SECONDS,
        member_token=member_token_for(db.engine, run_id=run_id, worker_id=worker_id),
    )


def _fail_before_process_seam(_db: LandscapeDB) -> None:
    """Module-level spawn target used to prove early-failure cleanup."""
    raise RuntimeError("deliberate child failure before readiness")


def _reach_process_seam(_db: LandscapeDB) -> None:
    """Module-level no-op proving a cooperative child can exit cleanly."""


def _block_before_process_seam(_db: LandscapeDB, *args: Any) -> None:
    args[-1].set()
    while True:
        time.sleep(1)


def _fail_before_durable_seam(_connection: Any) -> None:
    raise RuntimeError("controlled durable-seam failure")


def _exit_at_durable_seam(connection: Any) -> None:
    connection.send("controlled durable seam")
    os._exit(75)


def _signal_during_trace_retirement(path: str) -> None:
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait)
    thread.start()
    original_unregister = faulthandler.unregister

    def unregister_then_signal(signum: int) -> bool:
        result = original_unregister(signum)
        # This is exactly the boundary that used to restore SIG_DFL while a
        # secondary thread was still eligible to receive the process signal.
        os.kill(os.getpid(), signum)
        return result

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(faulthandler, "unregister", unregister_then_signal)
            with trace_child_process(path):
                pass
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="trace retirement control uses a POSIX signal")
def test_trace_retirement_ignores_late_signal_with_live_secondary_thread() -> None:
    diagnostics = ProcessDiagnostics()
    process = multiprocessing.get_context("spawn").Process(target=_signal_during_trace_retirement, args=(diagnostics.path,))
    try:
        process.start()
        process.join(timeout=20)
        assert process.exitcode == 0, diagnostics.snapshot(process)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()
        diagnostics.close()


def test_durable_seam_process_reports_early_failure_without_postgres() -> None:
    from tests.testcontainer.web.test_cross_process_run_reconciliation_postgres import _process

    with pytest.raises(AssertionError, match="controlled durable-seam failure"):
        _process(_fail_before_durable_seam, expected_exit=75)


def test_durable_seam_process_preserves_deliberate_exit_without_postgres() -> None:
    from tests.testcontainer.web.test_cross_process_run_reconciliation_postgres import _process

    assert _process(_exit_at_durable_seam, expected_exit=75) == "controlled durable seam"


class _PollClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_follower_readiness_preserves_database_error_and_child_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.e2e.recovery import test_sink_effect_deployment_profiles as profiles

    clock = _PollClock()
    monkeypatch.setattr(profiles, "time", clock)
    monkeypatch.setattr(profiles, "_PROCESS_TIMEOUT_SECONDS", 0.02)
    error = OperationalError("SELECT", {}, RuntimeError("controlled database read failure"))
    monkeypatch.setattr(profiles.LandscapeDB, "from_url", Mock(spec=LandscapeDB.from_url, side_effect=error))
    child = Mock(spec=SpawnedProcessAtSeam)
    child.is_alive = True
    child.diagnostic_snapshot.return_value = "controlled child stack"
    with pytest.raises(AssertionError, match="successful_observations=0") as failure:
        profiles._wait_for_fork_ready("sqlite:///unused.db", "run:blocked", child)
    assert failure.value.__cause__ is error
    assert "controlled database read failure" in str(failure.value)
    assert "controlled child stack" in str(failure.value)


def test_follower_readiness_distinguishes_absent_work_from_database_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.e2e.recovery import test_sink_effect_deployment_profiles as profiles

    database_url = f"sqlite:///{tmp_path / 'no-work.db'}"
    with LandscapeDB(database_url):
        pass
    monkeypatch.setattr(profiles, "time", _PollClock())
    monkeypatch.setattr(profiles, "_PROCESS_TIMEOUT_SECONDS", 0.02)
    child = Mock(spec=SpawnedProcessAtSeam)
    child.is_alive = True
    child.diagnostic_snapshot.return_value = "controlled child stack"
    with pytest.raises(AssertionError, match="successful_observations=2") as failure:
        profiles._wait_for_fork_ready(database_url, "run:absent", child)
    message = str(failure.value)
    assert "last_ready=False" in message
    assert "last_active_leader=False" in message
    assert "last_database_error=None" in message
    assert failure.value.__cause__ is None


@pytest.mark.skipif(os.name != "posix", reason="child stack capture uses a POSIX signal")
@pytest.mark.parametrize("spawn", [spawn_database_process_at_seam, spawn_database_process_with_pause])
def test_readiness_timeout_captures_live_action_stack_before_cleanup(tmp_path: Path, spawn: Any) -> None:
    database_url = f"sqlite:///{tmp_path / 'blocked.db'}"
    with LandscapeDB(database_url):
        pass
    entered = multiprocessing.get_context("spawn").Event()
    with spawn(database_url=database_url, seam="blocked-action", action=_block_before_process_seam, action_args=(entered,)) as child:
        assert entered.wait(20), "child did not enter controlled blocking action"
        with pytest.raises(AssertionError) as failure:
            child.wait_until_ready(timeout=0.05)
        message = str(failure.value)
        assert "alive=True" in message
        assert "phase=running action" in message
        assert "_block_before_process_seam" in message
        assert "opening landscape" in message
        assert child.closed
        assert not child.is_alive


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
def test_spawned_child_is_killed_at_named_seam_then_fresh_objects_resume(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    crashed_worker = "worker:process-crash"
    crashed_token_id = _craft_crashed_lease(
        crashed,
        ingest_sequence=3,
        lease_owner=crashed_worker,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
    )
    with crashed.db.engine.connect() as conn:
        work_item_id, initial_lease_expires_at = conn.execute(
            select(token_work_items_table.c.work_item_id, token_work_items_table.c.lease_expires_at).where(
                token_work_items_table.c.token_id == crashed_token_id
            )
        ).one()

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="after-lease-heartbeat",
        action=_heartbeat_crashed_lease,
        action_args=(crashed.run_id, str(work_item_id), crashed_worker, clock.now_utc().isoformat()),
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.seam == "after-lease-heartbeat"
        assert ready.pid != os.getpid()
        assert ready.database_dialect == "sqlite"
        with crashed.db.engine.connect() as conn:
            child_lease_expires_at = conn.execute(
                select(token_work_items_table.c.lease_expires_at).where(token_work_items_table.c.token_id == crashed_token_id)
            ).scalar_one()
        assert child_lease_expires_at > initial_lease_expires_at

        child.kill()
        exit_status = child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS)

    assert exit_status.exitcode == -signal.SIGKILL
    assert exit_status.was_killed is True

    # The killed child renewed the lease on the database clock; age it into
    # that clock's past (ADR-047) so the resume's sweep can reap it — the
    # process MockClock cannot expire a database-time lease.
    clock.advance(2 * _DEFAULT_LEASE_SECONDS + 60)
    expire_lease(crashed.db.engine, str(work_item_id))
    result, resume_sink, resume_source = _resume(crashed)

    assert result.status == RunStatus.COMPLETED
    assert result.rows_processed == 4
    assert resume_sink.results == [{"id": 3, "value": 30}]
    assert resume_source.load_invocations == 0


def test_readiness_failure_is_bounded_and_cleans_up_child(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'child-failure.db'}"
    db = LandscapeDB(database_url)
    db.close()

    child = spawn_database_process_at_seam(
        database_url=database_url,
        seam="never-reached",
        action=_fail_before_process_seam,
    )

    with pytest.raises(AssertionError, match="deliberate child failure before readiness"):
        child.wait_until_ready(timeout=5.0)

    assert child.closed is True
    assert child.is_alive is False


def test_spawned_child_release_has_bounded_clean_exit(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'child-release.db'}"
    db = LandscapeDB(database_url)
    db.close()

    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="cooperative-release",
        action=_reach_process_seam,
    ) as child:
        child.wait_until_ready(timeout=5.0)
        child.release()
        exit_status = child.wait_for_exit(timeout=5.0)

    assert exit_status.exitcode == 0
    assert exit_status.was_killed is False
