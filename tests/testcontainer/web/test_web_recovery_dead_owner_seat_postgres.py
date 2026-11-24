"""Web recovery of a real admitted run whose owner process died mid-run (PostgreSQL).

A production app admits a real run through ``/execute``: a saved composition
state, the production execution envelope, and an engine ``begin_run`` that
registers the graph and takes the run's Landscape leader seat. The run is held
after its source is exhausted and before its sink write, and the owning process
is then killed with SIGKILL, so no lease release, shutdown or destructor runs.

The parent then puts the dead owner's Landscape seat in one of two states:

- ``released``: the seat is released, as a clean leader exit leaves it;
- ``lapsed``: the seat's deadline is put in the database clock's past, the
  deterministic equivalent of waiting out the liveness window.

It expires the dead owner's web fence and instance, then starts fresh
production apps on the same databases. Each app's lifespan runs the production
recovery sweep; some apps also ask for a new run on the session through the
real routes.

Both seat states must recover the same way: a lapsed seat's incumbent is not
evidence that its owner is alive.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import sys
import threading
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select
from tests.fixtures.identities import grant_test_pipeline_user
from tests.fixtures.landscape import expire_leader_seat, leader_coordination_token
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_pg_http import pg_http_settings
from tests.helpers.process_diagnostics import ProcessDiagnostics, trace_child_process
from tests.integration.web.conftest import _save_composition_state_with_compose_authority
from tests.testcontainer.web.test_cross_process_run_reconciliation_postgres import (
    _close_owned_recovery_process,
    _expire_dead_owner,
    recovery_databases,
)

from elspeth.contracts.enums import RunStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import checkpoints_table, nodes_table, rows_table, run_coordination_table, run_sources_table
from elspeth.engine.orchestrator.sink_flush import SinkFlushCoordinator
from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.run_recovery_authority import RepositoryGlobalRunRecoveryAuthority
from elspeth.web.schema_probe import init_landscape_schema, init_session_schema
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import run_events_table, runs_table
from elspeth.web.sessions.protocol import CompositionStateData

pytestmark = [pytest.mark.testcontainer, pytest.mark.timing_sensitive]
__all__ = ["recovery_databases"]

_USERNAME = "seat-recovery-owner"
_PASSWORD = "seat recovery test password"
_SCHEMA = {"mode": "fixed", "fields": ["id: int", "name: str", "value: int"]}
_INPUT_CSV = b"id,name,value\n1,ada,42\n2,grace,7\n"
_CHILD_BOUND_SECONDS = 120.0
_SETTLE_BOUND_SECONDS = 90.0
_FAKE_PROVIDER_KEY = "offline-pg-http-test-only-not-a-provider-credential"


def _bootstrap_schemas(session_url: str, landscape_url: str) -> None:
    """Initialise both stores as a deployment's bootstrap does; the apps start in external-state mode."""
    session_owner = create_session_engine(session_url)
    landscape_owner = create_engine(landscape_url)
    try:
        init_session_schema(session_owner)
        init_landscape_schema(landscape_owner)
    finally:
        session_owner.dispose()
        landscape_owner.dispose()


def _prepare_data_dir(data_dir: Path) -> None:
    for directory in (data_dir, data_dir / "blobs", data_dir / "outputs", data_dir / "payloads"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)


def _composition(input_path: Path, output_path: Path) -> CompositionStateData:
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": str(input_path), "schema": _SCHEMA},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(name="primary", plugin="csv", options={"path": str(output_path), "schema": _SCHEMA}, on_write_failure="discard"),
        ),
        metadata=PipelineMetadata(name="Dead owner recovery", description="CSV passthrough"),
        version=1,
    ).to_dict()
    return CompositionStateData(
        sources=state["sources"],
        nodes=state["nodes"],
        edges=state["edges"],
        outputs=state["outputs"],
        metadata_=state["metadata"],
        is_valid=True,
        validation_errors=None,
    )


def _owner_process(session_url: str, landscape_url: str, data_dir: str, sender: Connection, diagnostic_path: str) -> None:
    """Admit a real run, hold it before its sink write, report, and wait to be killed."""
    with trace_child_process(diagnostic_path) as phase:
        asyncio.run(_owner(session_url, landscape_url, Path(data_dir), sender, phase))


async def _owner(session_url: str, landscape_url: str, data_dir: Path, sender: Connection, phase: Any) -> None:
    reached_sink_write = threading.Event()
    never = threading.Event()

    def held_flush_and_write_sinks(self: SinkFlushCoordinator, *args: Any, **kwargs: Any) -> Any:
        # The source is exhausted and every row is processed; nothing is written to the sink yet.
        reached_sink_write.set()
        never.wait()
        raise AssertionError("the owner process was expected to be killed while its run was held")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OPENAI_API_KEY", _FAKE_PROVIDER_KEY)
        patch.setattr(SinkFlushCoordinator, "flush_and_write_sinks", held_flush_and_write_sinks)
        app = create_app(settings=pg_http_settings(session_url, landscape_url, data_dir), process_watchdog_factory=OwnedTestProcessWatchdog)
        async with app.router.lifespan_context(app):
            phase("app started")
            provider = app.state.auth_provider
            assert isinstance(provider, LocalAuthProvider)
            provider.create_user(_USERNAME, _PASSWORD, "Seat Recovery Owner")
            token = await provider.login(_USERNAME, _PASSWORD)
            principal = await provider.authenticate(token)
            with app.state.session_engine.begin() as connection:
                grant_test_pipeline_user(connection, identity_id=principal.user_id)
            headers = {"Authorization": f"Bearer {token}"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
                created = await client.post("/api/sessions", headers=headers, json={"title": "dead owner recovery"})
                assert created.status_code == 201, created.text
                session_id = created.json()["id"]
                input_dir = data_dir / "blobs" / session_id
                input_dir.mkdir(parents=True, mode=0o700)
                input_path = input_dir / "input.csv"
                input_path.write_bytes(_INPUT_CSV)
                output_path = data_dir / "outputs" / session_id / "result.csv"
                await _save_composition_state_with_compose_authority(
                    app.state.session_service,
                    UUID(session_id),
                    _composition(input_path, output_path),
                    provenance="session_seed",
                )
                started = await client.post(f"/api/sessions/{session_id}/execute", headers=headers)
                assert started.status_code == 202, started.text
                run_id = started.json()["run_id"]
                phase(f"run {run_id} admitted")
                assert await asyncio.to_thread(reached_sink_write.wait, _CHILD_BOUND_SECONDS), "the run never reached its sink write"
                phase("run held before its sink write")
                sender.send(
                    {
                        "run_id": run_id,
                        "session_id": session_id,
                        "owner": app.state.session_service.session_operation_owner_instance_id,
                        "output_path": str(output_path),
                    }
                )
                # Held here until the parent kills this process.
                await asyncio.Event().wait()


def _admit_and_kill_owner(session_url: str, landscape_url: str, data_dir: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Spawn the owner, observe its held run from outside, then SIGKILL it."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    diagnostics = ProcessDiagnostics()
    process = context.Process(target=_owner_process, args=(session_url, landscape_url, str(data_dir), sender, diagnostics.path))
    try:
        process.start()
        sender.close()
        if not receiver.poll(_CHILD_BOUND_SECONDS):
            raise AssertionError(f"the owner process did not report its held run\n{diagnostics.snapshot(process)}")
        try:
            held: dict[str, str] = receiver.recv()
        except EOFError as exc:
            raise AssertionError(f"the owner process exited before its run was held\n{diagnostics.snapshot(process)}") from exc
        assert process.is_alive(), diagnostics.snapshot(process)
        at_kill = _landscape_snapshot(landscape_url, held["run_id"])
        pid = process.pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        process.join(timeout=30)
        assert process.exitcode == -signal.SIGKILL, diagnostics.snapshot(process)
        return held, at_kill
    finally:
        _close_owned_recovery_process(process, (receiver, sender), diagnostics, sys.exc_info()[1])


def _landscape_snapshot(landscape_url: str, run_id: str) -> dict[str, Any]:
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        repositories = RecorderFactory(db)
        run = repositories.run_lifecycle.get_run(run_id)
        leader = repositories.run_coordination.seat_holder(run_id=run_id)
        with db.engine.connect() as conn:
            nodes = conn.execute(select(func.count()).select_from(nodes_table).where(nodes_table.c.run_id == run_id)).scalar_one()
            rows = conn.execute(select(func.count()).select_from(rows_table).where(rows_table.c.run_id == run_id)).scalar_one()
            checkpoints = conn.execute(
                select(checkpoints_table.c.sequence_number)
                .where(checkpoints_table.c.run_id == run_id)
                .order_by(checkpoints_table.c.sequence_number)
            ).all()
            sources = conn.execute(select(run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)).all()
            seat = conn.execute(
                select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_epoch).where(
                    run_coordination_table.c.run_id == run_id
                )
            ).one_or_none()
    return {
        "landscape_status": None if run is None else run.status.value,
        "seat_holder": repr(leader),
        "nodes": nodes,
        "source_rows": rows,
        "checkpoint_sequences": [row.sequence_number for row in checkpoints],
        "source_lifecycle": [row.lifecycle_state for row in sources],
        "seat_row": None if seat is None else {"leader_worker_id": seat.leader_worker_id, "leader_epoch": seat.leader_epoch},
    }


def _web_snapshot(session_url: str, run_id: str) -> dict[str, Any]:
    engine = create_session_engine(session_url)
    try:
        with engine.connect() as conn:
            run = conn.execute(select(runs_table).where(runs_table.c.id == run_id)).one()
            events = conn.execute(
                select(run_events_table.c.event_type).where(run_events_table.c.run_id == run_id).order_by(run_events_table.c.sequence)
            ).all()
    finally:
        engine.dispose()
    return {
        "status": run.status,
        "saga_state": run.saga_state,
        "recovery_required_reason": run.recovery_required_reason,
        "cancel_requested": run.cancel_requested_at is not None,
        "rows_processed": run.rows_processed,
        "rows_succeeded": run.rows_succeeded,
        "error": run.error,
        "events": [row.event_type for row in events],
    }


def _offered_to_recovery(session_url: str, run_id: str) -> bool:
    """Whether a recovery sweep started now would be offered the run (candidate discovery only)."""
    engine = create_session_engine(session_url)
    try:
        return run_id in [str(record.id) for record in RepositoryGlobalRunRecoveryAuthority(engine).list_recoverable_run_records()]
    finally:
        engine.dispose()


async def _settle(app: Any, session_url: str, run_ids: list[str] | None = None) -> dict[str, list[str]]:
    """Wait, with a bound, until no run thread is live in this app and each run's saga is closed.

    A run leaves the live set before its worker closes the saga (a fenced write
    scheduled back onto this app's loop), so an empty live set alone does not
    mean the run has finished; leaving the lifespan in that window would refuse
    the close. ``run_ids`` defaults to the runs live when the wait starts. The
    saga reads run in a thread so this loop stays free to serve the close.
    """
    seen = sorted(app.state.execution_service.get_live_run_ids())
    awaited = seen if run_ids is None else sorted(run_ids)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_BOUND_SECONDS
    while app.state.execution_service.get_live_run_ids() and loop.time() < deadline:
        await asyncio.sleep(0.1)
    open_sagas = list(awaited)
    while open_sagas and loop.time() < deadline:
        snapshots = [await asyncio.to_thread(_web_snapshot, session_url, run) for run in open_sagas]
        open_sagas = [run for run, snapshot in zip(open_sagas, snapshots, strict=True) if snapshot["saga_state"] != "terminal"]
        if open_sagas:
            await asyncio.sleep(0.1)
    return {
        "live": seen,
        "still_live_at_bound": sorted(app.state.execution_service.get_live_run_ids()),
        "saga_open_at_bound": open_sagas,
    }


async def _app_cycle(
    session_url: str, landscape_url: str, data_dir: Path, *, run_id: str, session_id: str, execute: bool, cancel_on_conflict: bool
) -> dict[str, Any]:
    """One fresh production app: its lifespan runs the recovery sweep; optionally ask for a new run."""
    observed: dict[str, Any] = {}
    with pytest.MonkeyPatch.context() as credential_patch:
        credential_patch.setenv("OPENAI_API_KEY", _FAKE_PROVIDER_KEY)
        app = create_app(settings=pg_http_settings(session_url, landscape_url, data_dir), process_watchdog_factory=OwnedTestProcessWatchdog)
        async with app.router.lifespan_context(app):
            observed["live_after_startup"] = sorted(app.state.execution_service.get_live_run_ids())
            observed["settled_after_startup"] = await _settle(app, session_url)
            if execute:
                provider = app.state.auth_provider
                assert isinstance(provider, LocalAuthProvider)
                token = await provider.login(_USERNAME, _PASSWORD)
                headers = {"Authorization": f"Bearer {token}"}
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
                    response = await client.post(f"/api/sessions/{session_id}/execute", headers=headers)
                    observed["execute"] = [response.status_code, response.json()]
                    if response.status_code == 409 and cancel_on_conflict:
                        cancel = await client.post(f"/api/runs/{run_id}/cancel", headers=headers)
                        observed["cancel"] = [cancel.status_code, cancel.json()]
                        retry = await client.post(f"/api/sessions/{session_id}/execute", headers=headers)
                        observed["execute_after_cancel"] = [retry.status_code, retry.json()]
                    observed["settled_after_execute"] = await _settle(app, session_url, _new_run_ids(observed))
    return observed


def _new_run_ids(observed: dict[str, Any]) -> list[str]:
    return [observed[key][1]["run_id"] for key in ("execute", "execute_after_cancel") if key in observed and observed[key][0] == 202]


@pytest.mark.parametrize("seat", ["released", "lapsed"])
def test_dead_owner_run_resumes_whether_its_seat_was_released_or_lapsed(recovery_databases, tmp_path, seat):
    session_url, landscape_url = recovery_databases
    _bootstrap_schemas(session_url, landscape_url)
    _prepare_data_dir(tmp_path)
    held, at_kill = _admit_and_kill_owner(session_url, landscape_url, tmp_path)
    run_id, session_id = held["run_id"], held["session_id"]
    output_path = Path(held["output_path"])
    record: dict[str, Any] = {"seat": seat, "at_kill": at_kill, "web_at_kill": _web_snapshot(session_url, run_id)}

    # The owner was held mid-run: the run is RUNNING in Landscape with its graph and seat.
    assert at_kill["landscape_status"] == RunStatus.RUNNING.value
    assert at_kill["nodes"] > 0
    # Held after the source was read to the end and before any sink write, with the run-start checkpoint.
    assert at_kill["source_lifecycle"] == ["exhausted"]
    assert at_kill["checkpoint_sequences"] == [0]
    assert at_kill["source_rows"] == 2
    assert "seat_live=True" in at_kill["seat_holder"]
    assert record["web_at_kill"]["status"] == "running"

    _expire_dead_owner(session_url, session_id, held["owner"])
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        repositories = RecorderFactory(db)
        if seat == "lapsed":
            expire_leader_seat(db, run_id)
        else:
            repositories.run_coordination.release_seat(token=leader_coordination_token(repositories, run_id))
    record["before_app1"] = _landscape_snapshot(landscape_url, run_id)
    record["before_app1"]["offered_to_recovery"] = _offered_to_recovery(session_url, run_id)

    cycles = (
        ("app1", False, False),
        ("app2", True, True),
        ("app3", False, False),
        ("app4", True, False),
    )
    for name, execute, cancel_on_conflict in cycles:
        observed = asyncio.run(
            _app_cycle(
                session_url,
                landscape_url,
                tmp_path,
                run_id=run_id,
                session_id=session_id,
                execute=execute,
                cancel_on_conflict=cancel_on_conflict,
            )
        )
        observed["web"] = _web_snapshot(session_url, run_id)
        observed["offered_to_recovery_after"] = _offered_to_recovery(session_url, run_id)
        observed["landscape"] = _landscape_snapshot(landscape_url, run_id)
        observed["new_runs"] = {new_run: _web_snapshot(session_url, new_run) for new_run in _new_run_ids(observed)}
        observed["output_bytes"] = output_path.read_bytes().decode() if output_path.exists() else None
        record[name] = observed

    if seat == "released":
        assert record["before_app1"]["seat_holder"] == "None"
    else:
        assert "seat_live=False" in record["before_app1"]["seat_holder"]
    assert record["before_app1"]["offered_to_recovery"] is True
    # App 1's sweep resumes the run in that app (recover_run -> engine resume; the
    # seat epoch moves from 1 to 2) to completion, writing the held sink output once.
    assert record["app1"]["live_after_startup"] == [run_id]
    assert record["app1"]["settled_after_startup"]["still_live_at_bound"] == []
    assert record["app1"]["settled_after_startup"]["saga_open_at_bound"] == []
    assert record["app1"]["web"]["status"] == "completed"
    assert record["app1"]["web"]["saga_state"] == "terminal"
    assert record["app1"]["web"]["recovery_required_reason"] is None
    assert record["app1"]["web"]["events"] == [*record["web_at_kill"]["events"], "completed"]
    assert (record["app1"]["web"]["rows_processed"], record["app1"]["web"]["rows_succeeded"]) == (2, 2)
    assert record["app1"]["landscape"]["landscape_status"] == RunStatus.COMPLETED.value
    assert record["app1"]["landscape"]["seat_row"] == {"leader_worker_id": None, "leader_epoch": 2}
    # Resumed, not re-run: no source row was ingested again.
    assert record["app1"]["landscape"]["source_rows"] == 2
    assert record["app1"]["output_bytes"] == _INPUT_CSV.decode().replace("\n", "\r\n")
    for name in ("app2", "app4"):
        assert record[name]["execute"][0] == 202
        assert record[name]["settled_after_execute"]["saga_open_at_bound"] == []
        (new_run,) = record[name]["new_runs"].values()
        assert (new_run["status"], new_run["saga_state"]) == ("completed", "terminal")
