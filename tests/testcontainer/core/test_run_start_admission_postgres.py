"""Independent-process PostgreSQL permit admission and crash proofs."""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Iterator
from multiprocessing.connection import Connection
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.fixtures.landscape import expire_leader_seat
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import RunStatus
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.core.landscape import RecorderFactory
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import calls_table, nodes_table, run_coordination_events_table, run_start_admissions_table, runs_table
from elspeth.engine.orchestrator.bootstrap import prepare_for_run

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


def _create_then_die(url: str, binding: RunStartPermitBinding, ready: Connection) -> None:
    prepare_for_run()
    with LandscapeDB.from_url(url, create_tables=False) as db:
        RecorderFactory(db).run_lifecycle.begin_run(
            {},
            "v1",
            run_id=binding.run_id,
            run_start_permit=binding,
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        ready.send("committed")
        os._exit(19)


def test_killed_after_baseline_exact_retry_then_cancel_uses_new_authority(postgres_url: str) -> None:
    binding = RunStartPermitBinding(str(uuid4()), str(uuid4()), 1, "a" * 64)
    with LandscapeDB.from_url(postgres_url) as db:
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        child = context.Process(target=_create_then_die, args=(postgres_url, binding, send))
        child.start()
        send.close()
        try:
            assert receive.poll(30), "creator did not commit its prepared baseline"
            assert receive.recv() == "committed"
            child.join(timeout=30)
            assert child.exitcode == 19
        finally:
            receive.close()
            if child.is_alive():
                child.kill()
                child.join(timeout=10)
        factory = RecorderFactory(db)
        with db.engine.connect() as conn:
            before = conn.execute(
                select(run_coordination_events_table).where(run_coordination_events_table.c.run_id == binding.run_id)
            ).all()
        run = factory.run_lifecycle.begin_run(
            {},
            "v1",
            run_id=binding.run_id,
            run_start_permit=binding,
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        assert run.run_id == binding.run_id
        with db.engine.connect() as conn:
            assert (
                conn.execute(select(run_coordination_events_table).where(run_coordination_events_table.c.run_id == binding.run_id)).all()
                == before
            )
        expire_leader_seat(db, binding.run_id)
        result = factory.run_lifecycle.materialize_cancelled_permit(
            binding,
            {},
            "v1",
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        assert result.status is RunStatus.INTERRUPTED
        with db.engine.connect() as conn:
            assert len(conn.execute(select(runs_table).where(runs_table.c.run_id == binding.run_id)).all()) == 1
            assert (
                len(conn.execute(select(run_start_admissions_table).where(run_start_admissions_table.c.run_id == binding.run_id)).all())
                == 1
            )
            assert conn.execute(select(nodes_table).where(nodes_table.c.run_id == binding.run_id)).all() == []
            assert conn.execute(select(calls_table)).all() == []
            epochs = (
                conn.execute(
                    select(run_coordination_events_table.c.leader_epoch).where(run_coordination_events_table.c.run_id == binding.run_id)
                )
                .scalars()
                .all()
            )
            assert max(epoch for epoch in epochs if epoch is not None) == 2


def _race_create(url: str, binding: RunStartPermitBinding, control: Connection) -> None:
    prepare_for_run()
    with LandscapeDB.from_url(url, create_tables=False) as db:
        control.send("ready")
        assert control.recv() == "go"
        result = RecorderFactory(db).run_lifecycle.begin_run(
            {},
            "v1",
            run_id=binding.run_id,
            run_start_permit=binding,
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        control.send(result.run_id)


def test_two_processes_materialize_one_exact_permit(postgres_url: str) -> None:
    from elspeth.core.landscape.schema import run_workers_table

    binding = RunStartPermitBinding(str(uuid4()), str(uuid4()), 1, "b" * 64)
    with LandscapeDB.from_url(postgres_url) as db:
        context = multiprocessing.get_context("spawn")
        controls = [context.Pipe(), context.Pipe()]
        children = [context.Process(target=_race_create, args=(postgres_url, binding, pair[1])) for pair in controls]
        try:
            for child in children:
                child.start()
            for parent, remote in controls:
                remote.close()
                assert parent.poll(30)
                assert parent.recv() == "ready"
            for parent, _remote in controls:
                parent.send("go")
            for parent, _remote in controls:
                assert parent.poll(30)
                assert parent.recv() == binding.run_id
            for child in children:
                child.join(timeout=30)
                assert child.exitcode == 0
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join(timeout=10)
            for parent, _remote in controls:
                parent.close()
        with db.engine.connect() as conn:
            assert (
                len(conn.execute(select(run_start_admissions_table).where(run_start_admissions_table.c.run_id == binding.run_id)).all())
                == 1
            )
            assert len(conn.execute(select(run_workers_table).where(run_workers_table.c.run_id == binding.run_id)).all()) == 1


def _hold_projection_fence(url: str, token: CoordinationToken, control: Connection) -> None:
    from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction

    with LandscapeDB.from_url(url, create_tables=False) as db:
        with fenced_leader_transaction(db.engine, token=token, window_seconds=0.01, verb="test-terminal-projection"):
            control.send("locked")
            assert control.recv() == "release"
        control.send("released")


def _resume_during_projection(url: str, run_id: str, control: Connection) -> None:
    from elspeth.contracts.coordination import mint_worker_id
    from elspeth.core.checkpoint.recovery import NonResumableRunError

    with LandscapeDB.from_url(url, create_tables=False) as db:
        control.send("attempting")
        try:
            RecorderFactory(db).run_coordination.acquire_run_leadership(
                run_id=run_id,
                worker_id=mint_worker_id(run_id),
                window_seconds=30,
            )
        except NonResumableRunError:
            control.send("refused")
        else:
            control.send("acquired")


def test_terminal_projection_lock_blocks_cli_resume_after_lease_expiry(postgres_url: str) -> None:
    from tests.fixtures.landscape import leader_token_for

    from elspeth.contracts.coordination import mint_worker_id

    run_id = str(uuid4())
    with LandscapeDB.from_url(postgres_url) as db:
        factory = RecorderFactory(db)
        factory.run_lifecycle.begin_run({}, "v1", run_id=run_id, openrouter_catalog_sha256="a" * 64, openrouter_catalog_source="bundled")
        original = leader_token_for(db, run_id)
        factory.run_lifecycle.complete_run(RunStatus.FAILED, coordination_token=original)
        factory.run_coordination.release_seat(token=original)
        token = factory.run_coordination.acquire_reconciliation_leadership(
            run_id=run_id,
            worker_id=mint_worker_id(run_id),
            window_seconds=30,
            expected_status=RunStatus.FAILED,
        )
        context = multiprocessing.get_context("spawn")
        parent_hold, child_hold = context.Pipe()
        parent_resume, child_resume = context.Pipe()
        holder = context.Process(target=_hold_projection_fence, args=(postgres_url, token, child_hold))
        contender = context.Process(target=_resume_during_projection, args=(postgres_url, run_id, child_resume))
        try:
            holder.start()
            child_hold.close()
            assert parent_hold.poll(30)
            assert parent_hold.recv() == "locked"
            contender.start()
            child_resume.close()
            assert parent_resume.poll(30)
            assert parent_resume.recv() == "attempting"
            assert not parent_resume.poll(0.2), "CLI passed a projection transaction with an expired lease"
            with db.engine.connect() as conn:
                assert conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one() == "failed"
            parent_hold.send("release")
            assert parent_hold.poll(30)
            assert parent_hold.recv() == "released"
            assert parent_resume.poll(30)
            assert parent_resume.recv() in {"refused", "acquired"}
            for process in (holder, contender):
                process.join(timeout=30)
                assert process.exitcode == 0
        finally:
            for process in (holder, contender):
                if process.is_alive():
                    process.kill()
                    process.join(timeout=10)
            parent_hold.close()
            parent_resume.close()
