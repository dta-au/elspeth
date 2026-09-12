"""Kill real web dispatch and recover retained CSV input in a fresh process."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.testcontainer.web.test_cross_process_run_reconciliation_postgres import (
    _expire_dead_owner,
    _process,
    _service,
)
from tests.testcontainer.web.test_cross_process_run_reconciliation_postgres import recovery_databases as recovery_databases
from tests.unit.web.test_app import _settings

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionDecision
from elspeth.contracts.enums import RunStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import rows_table, run_start_admissions_table, token_outcomes_table
from elspeth.core.landscape.schema import runs_table as landscape_runs_table
from elspeth.engine.orchestrator.bootstrap import prepare_for_run
from elspeth.plugins.llm.model_catalog import read_openrouter_catalog_snapshot_id
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.contracts import RecoveryRequiredReason, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.envelope import EnvelopeRecoveryReason, _EnvelopePayload
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.recovery import RunRecoveryCoordinator
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, run_events_table, run_execution_inputs_table, run_start_permits_table, runs_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer
_ADMITTED_CSV = "value\nadmitted-a\nadmitted-b\n"


def _execution(sessions, engine, session_url, landscape_url, data_dir):
    telemetry = build_sessions_telemetry()
    loop = asyncio.get_running_loop()
    blobs = BlobServiceImpl(engine, Path(data_dir))
    execution = ExecutionServiceImpl.for_trained_operator(
        loop=loop,
        broadcaster=ProgressBroadcaster(loop, telemetry=telemetry),
        settings=_settings(
            Path(data_dir),
            session_db_url=session_url,
            landscape_url=landscape_url,
            payload_store_path=Path(data_dir) / "payloads",
        ),
        session_service=sessions,
        yaml_generator=yaml_generator,
        telemetry=telemetry,
        blob_service=blobs,
    )
    catalog_hash, catalog_source = read_openrouter_catalog_snapshot_id()
    execution.set_openrouter_catalog_snapshot(sha256=catalog_hash, source=catalog_source)
    prepare_for_run()
    return execution, blobs


def _die_during_web_dispatch(session_url, landscape_url, data_dir, seam, connection):
    async def admit():
        root = Path(data_dir)
        (root / "payloads").mkdir(mode=0o700, exist_ok=True)
        engine = create_session_engine(session_url)
        initialize_session_schema(engine)
        owner = f"dead-execution-{uuid4()}"
        sessions = _service(engine, owner)
        execution, _blobs = _execution(sessions, engine, session_url, landscape_url, data_dir)
        with LandscapeDB.from_url(landscape_url):
            pass
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
        session = await sessions.create_session("alice", "durable CSV admission", "local")
        source_path = root / "blobs" / str(session.id) / "input.csv"
        output_path = root / "outputs" / str(session.id) / "result.csv"
        source_path.parent.mkdir(parents=True)
        output_path.parent.mkdir(parents=True)
        source_path.write_text(_ADMITTED_CSV)
        schema = {"mode": "fixed", "fields": ["value: str"]}
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="primary",
                options={"path": str(source_path), "schema": schema},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="primary",
                    plugin="csv",
                    options={"path": str(output_path), "schema": schema},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(name="Crash seam CSV pipeline", description="Retained-input process death proof"),
            version=1,
        ).to_dict()
        compose = await SessionOperationLease.acquire(
            sessions.session_operation_authority,
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=owner,
            lease_seconds=300,
        )
        await sessions.save_composition_state(
            session.id,
            CompositionStateData(
                sources=state["sources"],
                nodes=state["nodes"],
                edges=state["edges"],
                outputs=state["outputs"],
                metadata_=state["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
            session_operation_context=compose.context,
        )
        await compose.close()
        lease = await SessionOperationLease.acquire(
            sessions.session_operation_authority,
            session_id=session.id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=owner,
            lease_seconds=300,
        )

        def die(run_id):
            connection.send((str(run_id), str(session.id), owner, str(source_path), str(output_path)))
            # Abrupt process death leaves neither executor cleanup nor lease
            # release an opportunity to manufacture a terminal projection.
            os._exit(75)

        original_issue = sessions.issue_run_start_permit
        original_update = sessions.update_run_status

        async def issue_then_die(run_id, **kwargs):
            result = await original_issue(run_id, **kwargs)
            if seam == "permit":
                die(run_id)
            return result

        async def update_then_die(run_id, *args, **kwargs):
            result = await original_update(run_id, *args, **kwargs)
            if seam == "sessions_link" and kwargs["status"] == "running":
                die(run_id)
            return result

        def die_before_submit(function, run_id, *args, **kwargs):
            die(run_id)

        with (
            patch.object(sessions, "issue_run_start_permit", new=issue_then_die),
            patch.object(sessions, "update_run_status", new=update_then_die),
        ):
            if seam == "admission":
                with patch.object(execution._executor, "submit", new=die_before_submit):
                    await execution.execute(session.id, session_operation_lease=lease, user_id="alice", auth_provider_type="local")
            else:
                run_id = await execution.execute(
                    session.id,
                    session_operation_lease=lease,
                    user_id="alice",
                    auth_provider_type="local",
                )
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    run = await sessions.get_run(run_id)
                    assert run.status not in ("failed", "cancelled", "completed"), (run.status, run.error)
                    await asyncio.sleep(0.05)
        raise AssertionError("dispatch did not reach the requested durable crash seam")

    asyncio.run(admit())


def _recover_full_web_dispatch(session_url, landscape_url, data_dir, run_id, connection):
    async def recover():
        engine = create_session_engine(session_url)
        sessions = _service(engine, f"fresh-execution-{uuid4()}")
        execution, blobs = _execution(sessions, engine, session_url, landscape_url, data_dir)
        coordinator = RunRecoveryCoordinator(sessions, execution, blobs, landscape_url=landscape_url, create_tables=False)
        await coordinator.recover()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = await sessions.get_run(UUID(run_id))
            assert run.status not in ("failed", "cancelled"), (run.status, run.error)
            assert run.saga_state != "recovery_required", run.recovery_required_reason
            if run.status == "completed" and not execution.get_live_run_ids():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"recovered pipeline did not finish: {run.status}, {run.saga_state}")
        # A second coordinator pass must not create another UUID or repeat the
        # successful CSV write / terminal audit outcome.
        await coordinator.recover()
        await execution.shutdown()
        engine.dispose()
        connection.send((str(run.id), run.status, run.rows_processed, run.rows_succeeded, run.rows_failed))

    asyncio.run(recover())


def _recover_unavailable_retained_source(session_url, landscape_url, data_dir, run_id, connection):
    async def recover():
        engine = create_session_engine(session_url)
        sessions = _service(engine, f"fresh-refusal-{uuid4()}")
        execution, blobs = _execution(sessions, engine, session_url, landscape_url, data_dir)
        coordinator = RunRecoveryCoordinator(sessions, execution, blobs, landscape_url=landscape_url, create_tables=False)
        refusals = []
        record_refusal = execution._record_recovery_refusal

        async def observe_refusal(refused_run_id, lease, error):
            refusals.append((refused_run_id, error.reason))
            await record_refusal(refused_run_id, lease, error)

        try:
            with (
                patch.object(execution, "_record_recovery_refusal", new=observe_refusal),
                patch.object(execution._executor, "submit", side_effect=AssertionError("Unavailable source must not dispatch")) as submit,
            ):
                await coordinator.recover()
                assert refusals == [(UUID(run_id), EnvelopeRecoveryReason.SOURCE_UNAVAILABLE)]
                submit.assert_not_called()
            run = await sessions.get_run(UUID(run_id))
            assert run.status == "pending"
            assert run.saga_state == "recovery_required"
            assert run.recovery_required_reason is RecoveryRequiredReason.INCOMPLETE_SOURCE
            assert not execution.get_live_run_ids()
            connection.send((str(run.id), run.saga_state, run.recovery_required_reason.value))
        finally:
            await execution.shutdown()
            engine.dispose()

    asyncio.run(recover())


def _cancel_refused_recovery(session_url, landscape_url, data_dir, run_id, connection):
    async def cancel():
        engine = create_session_engine(session_url)
        sessions = _service(engine, f"fresh-cancellation-{uuid4()}")
        execution, blobs = _execution(sessions, engine, session_url, landscape_url, data_dir)
        coordinator = RunRecoveryCoordinator(sessions, execution, blobs, landscape_url=landscape_url, create_tables=False)
        try:
            await execution.cancel(UUID(run_id), user=UserIdentity(user_id="alice", username="Alice"))
            await coordinator.recover()
            run = await sessions.get_run(UUID(run_id))
            assert run.status == "cancelled"
            assert run.saga_state == "terminal_cancelled"
            assert not execution.get_live_run_ids()
            connection.send((str(run.id), run.status, run.saga_state))
        finally:
            await execution.shutdown()
            engine.dispose()

    asyncio.run(cancel())


def _recover_disabled_owner(session_url, landscape_url, data_dir, run_id, connection):
    async def recover():
        engine = create_session_engine(session_url)
        sessions = _service(engine, f"disabled-recovery-{uuid4()}")
        execution, blobs = _execution(sessions, engine, session_url, landscape_url, data_dir)
        coordinator = RunRecoveryCoordinator(sessions, execution, blobs, landscape_url=landscape_url, create_tables=False)
        try:
            with (
                patch.object(
                    execution, "_restore_admitted_run", side_effect=AssertionError("Disabled owner must not restore inputs")
                ) as restore,
                patch.object(execution._executor, "submit", side_effect=AssertionError("Disabled owner must not dispatch")) as submit,
            ):
                await coordinator.recover()
                restore.assert_not_called()
                submit.assert_not_called()
            run = await sessions.get_run(UUID(run_id))
            assert run.status == "failed"
            assert not execution.get_live_run_ids()
            connection.send((str(run.id), run.status))
        finally:
            await execution.shutdown()
            engine.dispose()

    asyncio.run(recover())


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_fresh_process_refuses_unavailable_retained_source(request, tmp_path, damage):
    session_url, landscape_url = request.getfixturevalue("recovery_databases")
    run_id, session_id, owner, source_path, output_path = _process(
        _die_during_web_dispatch,
        session_url,
        landscape_url,
        str(tmp_path),
        "admission",
        expected_exit=75,
    )
    engine = create_session_engine(session_url)
    try:
        with engine.connect() as conn:
            envelope = conn.execute(
                select(run_execution_inputs_table.c.envelope).where(run_execution_inputs_table.c.run_id == run_id)
            ).scalar_one()
        retained_inputs = _EnvelopePayload.model_validate(envelope).retained_inputs
        assert len(retained_inputs) == 1
        retained_path = Path(retained_inputs[0].retained_path)
        assert retained_path != Path(source_path)
        assert retained_path.read_text() == _ADMITTED_CSV
        if damage == "missing":
            retained_path.unlink()
        else:
            retained_path.write_text(_ADMITTED_CSV.replace("admitted", "modified"))
        # The original remains readable; recovery must refuse rather than
        # substitute mutable session storage for the admitted retained bytes.
        assert Path(source_path).read_text() == _ADMITTED_CSV
        _expire_dead_owner(session_url, session_id, owner)
        assert _process(
            _recover_unavailable_retained_source,
            session_url,
            landscape_url,
            str(tmp_path),
            run_id,
            expected_exit=0,
        ) == (run_id, "recovery_required", RecoveryRequiredReason.INCOMPLETE_SOURCE.value)
        assert not Path(output_path).exists()
        with engine.connect() as conn:
            permit = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
            assert permit.start_state == "pending"
            events = conn.execute(select(run_events_table.c.event_type).where(run_events_table.c.run_id == run_id)).scalars().all()
            assert not {"failed", "completed", "cancelled"}.intersection(events)
        with LandscapeDB.from_url(landscape_url, create_tables=False) as landscape, landscape.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(landscape_runs_table)).scalar_one() == 0
        assert _process(
            _cancel_refused_recovery,
            session_url,
            landscape_url,
            str(tmp_path),
            run_id,
            expected_exit=0,
        ) == (run_id, "cancelled", "terminal_cancelled")
        with engine.connect() as conn:
            permit = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
            assert permit.start_state == "cancelled_before_permit"
            assert permit.permit_subject_hash is None
            assert permit.issued_at is None
            events = conn.execute(select(run_events_table.c.event_type).where(run_events_table.c.run_id == run_id)).scalars().all()
            assert events.count("cancelled") == 1
            assert not {"failed", "completed"}.intersection(events)
        assert not Path(output_path).exists()
        with LandscapeDB.from_url(landscape_url, create_tables=False) as landscape, landscape.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(landscape_runs_table)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(run_start_admissions_table)).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("seam", ["admission", "permit"])
def test_fresh_process_refuses_disabled_owner_before_restore_or_dispatch(request, tmp_path, seam):
    session_url, landscape_url = request.getfixturevalue("recovery_databases")
    run_id, session_id, owner, source_path, output_path = _process(
        _die_during_web_dispatch, session_url, landscape_url, str(tmp_path), seam, expected_exit=75
    )
    engine = create_session_engine(session_url)
    try:
        with engine.begin() as conn:
            original = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
            conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
        _expire_dead_owner(session_url, session_id, owner)
        assert _process(_recover_disabled_owner, session_url, landscape_url, str(tmp_path), run_id, expected_exit=0) == (run_id, "failed")
        with engine.connect() as conn:
            permit = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
            if seam == "admission":
                assert permit.start_state == "refused"
                assert permit.permit_subject_hash is None
                assert permit.issued_at is None
                decision = ChargeableAdmissionDecision.model_validate_json(json.dumps(permit.admission_decision))
            else:
                assert permit.start_state == "start_permitted"
                assert permit.permit_subject_hash == original.permit_subject_hash
                assert permit.issued_at == original.issued_at
                assert permit.admission_decision == original.admission_decision
                decision = ChargeableAdmissionDecision.model_validate_json(json.dumps(permit.execution_refusal))
            assert decision.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
        assert Path(source_path).read_text() == _ADMITTED_CSV
        assert not Path(output_path).exists()
        with LandscapeDB.from_url(landscape_url, create_tables=False) as landscape, landscape.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(landscape_runs_table)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(run_start_admissions_table)).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("seam", ["admission", "permit", "sessions_link"])
def test_fresh_process_recovers_full_web_pipeline_from_durable_admission(request, tmp_path, seam):
    session_url, landscape_url = request.getfixturevalue("recovery_databases")
    run_id, session_id, owner, source_path, output_path = _process(
        _die_during_web_dispatch,
        session_url,
        landscape_url,
        str(tmp_path),
        seam,
        expected_exit=75,
    )
    engine = create_session_engine(session_url)
    try:
        with engine.connect() as conn:
            run = conn.execute(select(runs_table).where(runs_table.c.id == run_id)).one()
            permit = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
            envelope = conn.execute(select(run_execution_inputs_table).where(run_execution_inputs_table.c.run_id == run_id)).one()
            assert run.status == ("running" if seam == "sessions_link" else "pending")
            assert permit.start_state == ("pending" if seam == "admission" else "start_permitted")
            assert envelope.automatic_recovery_eligible
        with LandscapeDB.from_url(landscape_url, create_tables=False) as landscape, landscape.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(landscape_runs_table)).scalar_one() == 0
        assert not Path(output_path).exists()
        Path(source_path).write_text("value\nreplacement-must-not-be-read\n")
        _expire_dead_owner(session_url, session_id, owner)
        recovered = _process(
            _recover_full_web_dispatch,
            session_url,
            landscape_url,
            str(tmp_path),
            run_id,
            expected_exit=0,
        )
        assert recovered == (run_id, "completed", 2, 2, 0)
        with Path(output_path).open(newline="") as output:
            assert list(csv.DictReader(output)) == [{"value": "admitted-a"}, {"value": "admitted-b"}]
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(runs_table)).scalar_one() == 1
            assert conn.execute(select(func.count()).select_from(run_start_permits_table)).scalar_one() == 1
            events = conn.execute(select(run_events_table.c.event_type).where(run_events_table.c.run_id == run_id)).scalars().all()
            assert events.count("completed") == 1
            assert "failed" not in events
        with LandscapeDB.from_url(landscape_url, create_tables=False) as landscape, landscape.engine.connect() as conn:
            run = conn.execute(select(landscape_runs_table).where(landscape_runs_table.c.run_id == run_id)).one()
            assert run.status == RunStatus.COMPLETED.value
            assert conn.execute(select(func.count()).select_from(landscape_runs_table)).scalar_one() == 1
            assert conn.execute(select(func.count()).select_from(run_start_admissions_table)).scalar_one() == 1
            assert conn.execute(select(func.count()).select_from(rows_table).where(rows_table.c.run_id == run_id)).scalar_one() == 2
            outcomes = conn.execute(select(token_outcomes_table.c.outcome).where(token_outcomes_table.c.run_id == run_id)).scalars().all()
            assert outcomes == ["success", "success"]
    finally:
        engine.dispose()
