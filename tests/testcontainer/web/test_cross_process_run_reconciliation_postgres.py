"""Process-death proofs through the real PostgreSQL recovery coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import structlog
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url
from tests.fixtures.landscape import leader_coordination_token, register_test_node
from tests.testcontainer.web.test_cross_process_run_control_postgres import _envelope
from tests.testcontainer.web.test_global_run_recovery_postgres import _expire_fence, _expire_instance, _register_live_instance
from tests.unit.web.blobs.test_service_fencing import _reserve_output_blob
from tests.unit.web.test_app import _settings

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.declaration_contracts import freeze_declaration_registry
from elspeth.contracts.enums import NodeType, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.tier_registry import freeze_tier_registry
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_coordination_table, run_workers_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer import yaml_generator
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.recovery import RunRecoveryCoordinator
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, run_events_table, runs_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer
_OUTPUT = b"value\n"


@pytest.fixture
def recovery_databases(external_deployment_postgres_url):
    base = make_url(external_deployment_postgres_url).set(drivername="postgresql")
    names = (f"reconcile_sessions_{uuid4().hex}", f"reconcile_landscape_{uuid4().hex}")
    with psycopg.connect(base.render_as_string(hide_password=False), autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield tuple(base.set(drivername="postgresql+psycopg", database=name).render_as_string(hide_password=False) for name in names)
    finally:
        with psycopg.connect(base.render_as_string(hide_password=False), autocommit=True) as admin:
            for name in names:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _service(engine, owner):
    _register_live_instance(engine, owner)
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.process-reconciliation"),
        owner_instance_id=owner,
    )


def _die_after_engine_result(session_url, landscape_url, data_dir, terminal, connection):
    async def seed():
        engine = create_session_engine(session_url)
        initialize_session_schema(engine)
        owner = f"crashed-admitter-{uuid4()}"
        sessions = _service(engine, owner)
        session = await sessions.create_session("alice", "crash handoff", "local")
        authority = sessions.session_operation_authority
        compose = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id=owner, lease_seconds=300
        )
        state = await sessions.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose,
        )
        authority.release(compose)
        execute = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id=owner, lease_seconds=300
        )
        run = await sessions.create_run(session.id, state.id, session_operation_context=execute, execution_input=_envelope())
        authority.mutate(execute, lambda tx: tx.runs.issue_start_permit(run_id=run.id))
        blobs = BlobServiceImpl(engine, Path(data_dir))
        blob = _reserve_output_blob(blobs, execute, run.id)
        Path(blob.storage_path).write_bytes(_OUTPUT)
        await sessions.update_run_status(run.id, "running", landscape_run_id=str(run.id), session_operation_context=execute)
        freeze_declaration_registry()
        freeze_tier_registry()
        with LandscapeDB.from_url(landscape_url) as db:
            repositories = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(data_dir) / "payloads"))
            repositories.run_lifecycle.begin_run(
                config={},
                canonical_version="v1",
                run_id=str(run.id),
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
            if terminal is True:
                leader = leader_coordination_token(repositories, str(run.id))
                source_id = register_test_node(repositories.data_flow, str(run.id), "source", node_type=NodeType.SOURCE)
                _, token = repositories.data_flow.create_row_with_token(
                    source_id, 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=leader
                )
                repositories.data_flow.record_token_outcome_leader(
                    TokenRef(token.token_id, str(run.id)),
                    TerminalOutcome.SUCCESS,
                    TerminalPath.FILTER_DROPPED,
                    coordination_token=leader,
                )
                repositories.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader)
            elif terminal:
                leader = leader_coordination_token(repositories, str(run.id))
                repositories.run_lifecycle.complete_run(RunStatus(terminal), coordination_token=leader)
                repositories.run_coordination.release_seat(token=leader)
        connection.send((str(run.id), str(session.id), owner, str(blob.id), blob.storage_path))
        # No lease release, service shutdown or destructor can run at this seam.
        os._exit(73)

    asyncio.run(seed())


def _run_reconciler(session_url, landscape_url, data_dir, crash_before_outputs, connection):
    async def recover():
        engine = create_session_engine(session_url)
        owner = f"reconciler-{uuid4()}"
        sessions = _service(engine, owner)
        telemetry = build_sessions_telemetry()
        blobs = BlobServiceImpl(engine, Path(data_dir))
        execution = ExecutionServiceImpl.for_trained_operator(
            loop=asyncio.get_running_loop(),
            broadcaster=ProgressBroadcaster(asyncio.get_running_loop(), telemetry=telemetry),
            settings=_settings(Path(data_dir)),
            session_service=sessions,
            yaml_generator=yaml_generator,
            telemetry=telemetry,
            blob_service=blobs,
        )
        coordinator = RunRecoveryCoordinator(sessions, execution, blobs, landscape_url=landscape_url, create_tables=False)
        if crash_before_outputs == "pause_after_observe":
            from unittest.mock import patch

            from elspeth.web.execution import recovery as recovery_module

            original_observe = recovery_module.observe_run
            paused = False

            def observe_then_pause(*args, **kwargs):
                nonlocal paused
                observation = original_observe(*args, **kwargs)
                if not paused:
                    paused = True
                    connection.send(("observed", observation.status.value))
                    assert connection.recv() == "continue"
                return observation

            with patch.object(recovery_module, "observe_run", observe_then_pause):
                await coordinator.recover()
        elif crash_before_outputs:
            from unittest.mock import patch

            async def die_before_outputs(*args, **kwargs):
                connection.send(("projection_committed", owner))
                os._exit(74)

            with patch.object(blobs, "finalize_run_output_blobs", die_before_outputs):
                await coordinator.recover()
            raise AssertionError("expected the process to die at output finalization")
        else:
            await coordinator.recover()
        await coordinator.recover()
        await execution.shutdown()
        engine.dispose()
        connection.send(("recovered", owner))

    asyncio.run(recover())


def _process(target, *args, expected_exit):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(*args, sender))
    try:
        process.start()
        sender.close()
        assert receiver.poll(90), "recovery subprocess did not report its durable seam"
        result = receiver.recv()
        process.join(timeout=30)
        assert process.exitcode == expected_exit
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        receiver.close()


def _expire_dead_owner(session_url, session_id, owner):
    engine = create_session_engine(session_url)
    try:
        _expire_fence(engine, UUID(session_id))
        _expire_instance(engine, owner)
    finally:
        engine.dispose()


def _web_snapshot(session_url, run_id, blob_id):
    engine = create_session_engine(session_url)
    try:
        with engine.connect() as conn:
            run = conn.execute(select(runs_table).where(runs_table.c.id == run_id)).one()
            blob = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
            events = conn.execute(select(run_events_table).where(run_events_table.c.run_id == run_id)).all()
            return run, blob, events
    finally:
        engine.dispose()


def test_dead_owner_terminal_truth_and_output_finalization_survive_second_process_death(recovery_databases, tmp_path):
    session_url, landscape_url = recovery_databases
    run_id, session_id, owner, blob_id, storage_path = _process(
        _die_after_engine_result,
        session_url,
        landscape_url,
        str(tmp_path),
        True,
        expected_exit=73,
    )
    run, blob, events = _web_snapshot(session_url, run_id, blob_id)
    assert (run.status, blob.status, len(events)) == ("running", "pending", 0)
    _expire_dead_owner(session_url, session_id, owner)
    seam, recovery_owner = _process(_run_reconciler, session_url, landscape_url, str(tmp_path), True, expected_exit=74)
    assert seam == "projection_committed"
    run, blob, events = _web_snapshot(session_url, run_id, blob_id)
    assert (run.status, run.saga_state, blob.status, len(events)) == ("completed", "running", "pending", 1)
    assert events[0].event_type == "completed"
    _expire_dead_owner(session_url, session_id, recovery_owner)
    _process(_run_reconciler, session_url, landscape_url, str(tmp_path), False, expected_exit=0)
    run, blob, events = _web_snapshot(session_url, run_id, blob_id)
    assert (run.status, run.saga_state, blob.status, len(events)) == ("completed", "terminal", "ready", 1)
    assert blob.content_hash == hashlib.sha256(_OUTPUT).hexdigest()
    assert blob.size_bytes == len(_OUTPUT)
    assert Path(storage_path).read_bytes() == _OUTPUT
    assert run.rows_processed == run.rows_succeeded == 1
    assert run.rows_failed == 0
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        assert RecorderFactory(db).run_lifecycle.get_run(run_id).status == RunStatus.COMPLETED


def test_dead_web_owner_with_live_landscape_seat_defers_without_terminal_projection_or_landscape_mutation(recovery_databases, tmp_path):
    session_url, landscape_url = recovery_databases
    run_id, session_id, owner, blob_id, storage_path = _process(
        _die_after_engine_result,
        session_url,
        landscape_url,
        str(tmp_path),
        False,
        expected_exit=73,
    )
    original_run, original_blob, _ = _web_snapshot(session_url, run_id, blob_id)
    _expire_dead_owner(session_url, session_id, owner)
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db, db.engine.connect() as conn:
        before = tuple(
            conn.execute(select(table).where(table.c.run_id == run_id)).all() for table in (run_coordination_table, run_workers_table)
        )
    _process(_run_reconciler, session_url, landscape_url, str(tmp_path), False, expected_exit=0)
    run, blob, events = _web_snapshot(session_url, run_id, blob_id)
    assert (run.status, blob.status, len(events)) == ("running", "pending", 0)
    assert run.finished_at is None
    assert (run.owner_instance_id, run.owner_epoch) == (original_run.owner_instance_id, original_run.owner_epoch)
    assert (blob.custody_operation_id, blob.custody_operation_epoch) == (
        original_blob.custody_operation_id,
        original_blob.custody_operation_epoch,
    )
    assert Path(storage_path).read_bytes() == _OUTPUT
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        assert RecorderFactory(db).run_lifecycle.get_run(run_id).status == RunStatus.RUNNING
        with db.engine.connect() as conn:
            after = tuple(
                conn.execute(select(table).where(table.c.run_id == run_id)).all() for table in (run_coordination_table, run_workers_table)
            )
    assert after == before


@pytest.mark.parametrize("terminal_status", ["failed", "interrupted"])
def test_cli_takeover_after_resumable_snapshot_defers_all_web_projection(recovery_databases, tmp_path, terminal_status):
    from elspeth.contracts.coordination import mint_worker_id

    session_url, landscape_url = recovery_databases
    run_id, session_id, owner, blob_id, _ = _process(
        _die_after_engine_result,
        session_url,
        landscape_url,
        str(tmp_path),
        terminal_status,
        expected_exit=73,
    )
    _expire_dead_owner(session_url, session_id, owner)
    original_run, original_blob, original_events = _web_snapshot(session_url, run_id, blob_id)
    spawn = multiprocessing.get_context("spawn")
    controller, participant = spawn.Pipe()
    process = spawn.Process(
        target=_run_reconciler,
        args=(session_url, landscape_url, str(tmp_path), "pause_after_observe", participant),
    )
    try:
        process.start()
        participant.close()
        assert controller.poll(60)
        assert controller.recv() == ("observed", terminal_status)
        with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
            repositories = RecorderFactory(db)
            cli_token = repositories.run_coordination.acquire_run_leadership(
                run_id=run_id,
                worker_id=mint_worker_id(run_id),
                window_seconds=80,
                entry_point="cli-resume-test",
            )
            with db.engine.connect() as conn:
                before = tuple(
                    conn.execute(select(table).where(table.c.run_id == run_id)).all()
                    for table in (run_coordination_table, run_workers_table)
                )
            controller.send("continue")
            assert controller.poll(60)
            assert controller.recv()[0] == "recovered"
            process.join(timeout=30)
            assert process.exitcode == 0
            run, blob, events = _web_snapshot(session_url, run_id, blob_id)
            assert (run.status, run.owner_instance_id, run.owner_epoch) == (
                original_run.status,
                original_run.owner_instance_id,
                original_run.owner_epoch,
            )
            assert run.finished_at is None
            assert events == original_events == []
            assert (blob.status, blob.custody_operation_id, blob.custody_operation_epoch) == (
                original_blob.status,
                original_blob.custody_operation_id,
                original_blob.custody_operation_epoch,
            )
            with db.engine.connect() as conn:
                after = tuple(
                    conn.execute(select(table).where(table.c.run_id == run_id)).all()
                    for table in (run_coordination_table, run_workers_table)
                )
            assert after == before
            assert repositories.run_lifecycle.get_run(run_id).status == RunStatus.RUNNING
            repositories.run_lifecycle.complete_run(RunStatus.EMPTY, coordination_token=cli_token)
            repositories.run_coordination.release_seat(token=cli_token)
        _process(_run_reconciler, session_url, landscape_url, str(tmp_path), False, expected_exit=0)
        recovered_run, recovered_blob, recovered_events = _web_snapshot(session_url, run_id, blob_id)
        assert (recovered_run.status, recovered_blob.status, len(recovered_events)) == ("empty", "ready", 1)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        controller.close()


@pytest.mark.parametrize(
    "terminal_status,web_status,blob_status",
    [("failed", "failed", "ready"), ("interrupted", "cancelled", "error")],
)
def test_resumable_terminal_projection_preserves_original_landscape_result(
    recovery_databases, tmp_path, terminal_status, web_status, blob_status
):
    session_url, landscape_url = recovery_databases
    run_id, session_id, owner, blob_id, _ = _process(
        _die_after_engine_result,
        session_url,
        landscape_url,
        str(tmp_path),
        terminal_status,
        expected_exit=73,
    )
    _expire_dead_owner(session_url, session_id, owner)
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        original = RecorderFactory(db).run_lifecycle.get_run(run_id)
    _process(_run_reconciler, session_url, landscape_url, str(tmp_path), False, expected_exit=0)
    run, blob, events = _web_snapshot(session_url, run_id, blob_id)
    assert (run.status, run.saga_state, blob.status, len(events)) == (web_status, "terminal", blob_status, 1)
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        current = RecorderFactory(db).run_lifecycle.get_run(run_id)
    assert current.status == original.status
    assert current.completed_at == original.completed_at
