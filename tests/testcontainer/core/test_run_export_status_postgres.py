"""Seat admission observes status transitions committed by its prior holder."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import event, func, insert, select, update
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import expire_leader_seat
from tests.helpers.postgres_target import postgres_test_target
from tests.helpers.run_coordination import register_run_leader
from tests.helpers.state_engine import StateEngineImage, _capture_on_connection, capture_state_engine_image

from elspeth.contracts.coordination import CoordinationToken, mint_worker_id
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import run_coordination_table, runs_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.mark.timeout(90)
@pytest.mark.parametrize("transition", ["resume-before-export", "complete-before-resume"])
def test_admission_waiting_for_seat_reads_committed_run_status(
    postgres_url: str, transition: Literal["resume-before-export", "complete-before-resume"]
) -> None:
    run_id = transition
    resume_id = mint_worker_id(run_id)
    export_id = mint_worker_id(run_id)
    resume_ready = threading.Event()
    allow_resume_commit = threading.Event()
    export_connected = threading.Event()
    backend_pids: dict[str, int] = {}
    outcomes: dict[str, object] = {}
    resumed_images: list[StateEngineImage] = []

    with ExitStack() as resources:
        observer = resources.enter_context(closing(LandscapeDB.from_url(postgres_url)))
        resume_db = resources.enter_context(closing(LandscapeDB.from_url(postgres_url)))
        export_db = resources.enter_context(closing(LandscapeDB.from_url(postgres_url)))
        with observer.engine.begin() as conn:
            conn.execute(
                insert(runs_table).values(
                    run_id=run_id,
                    started_at=datetime.now(UTC),
                    config_hash="config",
                    settings_json="{}",
                    canonical_version="v1",
                    status="failed" if transition == "resume-before-export" else "running",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
        incumbent = register_run_leader(
            RunCoordinationRepository(observer.engine), run_id=run_id, worker_id=mint_worker_id(run_id), window_seconds=30
        )
        expire_leader_seat(observer, run_id)

        def hold_resume_commit(conn: Connection) -> None:
            if resume_ready.is_set():
                return
            # Model the holder dying after its status transition commits.
            # Lease expiry must not override the committed status gate.
            conn.execute(
                update(run_coordination_table)
                .where(run_coordination_table.c.run_id == run_id)
                .values(leader_heartbeat_expires_at=func.current_timestamp() - timedelta(seconds=1))
            )
            backend_pids["resume"] = conn.execute(select(func.pg_backend_pid())).scalar_one()
            resumed_images.append(_capture_on_connection(conn, run_id=run_id))
            resume_ready.set()
            if not allow_resume_commit.wait(timeout=20):
                raise TimeoutError("resume transaction was not released")

        def record_export_connection(conn: Connection) -> None:
            conn.exec_driver_sql("SET LOCAL statement_timeout = '20000ms'")
            backend_pids["export"] = conn.execute(select(func.pg_backend_pid())).scalar_one()
            export_connected.set()

        def resume() -> None:
            try:
                if transition == "resume-before-export":
                    outcomes["resume"] = RunCoordinationRepository(resume_db.engine).acquire_run_leadership(
                        run_id=run_id, worker_id=resume_id, window_seconds=30
                    )
                else:
                    outcomes["resume"] = RecorderFactory(resume_db).run_lifecycle.complete_run(
                        RunStatus.COMPLETED, coordination_token=incumbent
                    )
            except BaseException as exc:
                outcomes["resume"] = exc

        def export() -> None:
            try:
                contender = RunCoordinationRepository(export_db.engine)
                if transition == "resume-before-export":
                    outcomes["export"] = contender.acquire_export_leadership(run_id=run_id, worker_id=export_id, window_seconds=30)
                else:
                    outcomes["export"] = contender.acquire_run_leadership(run_id=run_id, worker_id=export_id, window_seconds=30)
            except BaseException as exc:
                outcomes["export"] = exc

        event.listen(resume_db.engine, "commit", hold_resume_commit)
        event.listen(export_db.engine, "begin", record_export_connection)
        resume_thread = threading.Thread(target=resume, name="resume-before-export")
        export_thread = threading.Thread(target=export, name="export-after-resume")
        try:
            resume_thread.start()
            assert resume_ready.wait(timeout=15), outcomes
            export_thread.start()
            assert export_connected.wait(timeout=15), outcomes
            deadline = time.monotonic() + 10
            with observer.engine.connect() as conn:
                while True:
                    blockers = conn.execute(select(func.pg_blocking_pids(backend_pids["export"]))).scalar_one()
                    if backend_pids["resume"] in blockers:
                        break
                    assert "export" not in outcomes, outcomes
                    assert time.monotonic() < deadline, "PostgreSQL did not report export blocked behind resume"
                    time.sleep(0.01)
            allow_resume_commit.set()
            resume_thread.join(timeout=20)
            export_thread.join(timeout=20)
            assert not resume_thread.is_alive() and not export_thread.is_alive()
        finally:
            allow_resume_commit.set()
            for thread in (resume_thread, export_thread):
                if thread.ident is not None:
                    thread.join(timeout=25)
            event.remove(resume_db.engine, "commit", hold_resume_commit)
            event.remove(export_db.engine, "begin", record_export_connection)

        if transition == "resume-before-export":
            assert isinstance(outcomes["resume"], CoordinationToken), outcomes
            assert isinstance(outcomes["export"], NonResumableRunError), outcomes
            expected_status, expected_worker, expected_epoch = "running", resume_id, 2
        else:
            assert not isinstance(outcomes["resume"], BaseException), outcomes
            assert isinstance(outcomes["export"], AuditIntegrityError), outcomes
            expected_status, expected_worker, expected_epoch = "completed", incumbent.worker_id, 1
        with observer.engine.connect() as conn:
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
            seat = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).one()
        assert status == expected_status
        assert (seat.leader_worker_id, seat.leader_epoch) == (expected_worker, expected_epoch)
        # Refusal must leave the entire image produced by the holder unchanged.
        assert len(resumed_images) == 1
        assert capture_state_engine_image(observer, run_id=run_id) == resumed_images[0]
