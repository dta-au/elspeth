"""Independent-process crash controls for the state-engine recovery harness."""

from __future__ import annotations

import os
import signal
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import token_work_items_table
from elspeth.engine.clock import MockClock
from tests.e2e.recovery.harness import (
    _DEFAULT_LEASE_SECONDS,
    _T0,
    _craft_crashed_lease,
    _resume,
    _run_to_interrupted_checkpoint,
    spawn_database_process_at_seam,
)

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
        run_id=run_id,
        work_item_id=work_item_id,
        lease_owner=worker_id,
        lease_seconds=2 * _DEFAULT_LEASE_SECONDS,
        now=datetime.fromisoformat(now_iso),
        membership_fenced=True,
    )


def _fail_before_process_seam(_db: LandscapeDB) -> None:
    """Module-level spawn target used to prove early-failure cleanup."""
    raise RuntimeError("deliberate child failure before readiness")


def _reach_process_seam(_db: LandscapeDB) -> None:
    """Module-level no-op proving a cooperative child can exit cleanly."""


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

    clock.advance(2 * _DEFAULT_LEASE_SECONDS + 60)
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
