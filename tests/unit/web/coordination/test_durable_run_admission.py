"""Admission, start and peer cancellation share the durable session lock."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update

from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind, StartPermitState
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.run_cancellation_authority import RepositoryRunCancellationAuthority
from elspeth.web.coordination.run_recovery_authority import RepositoryGlobalRunRecoveryAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.envelope import RunExecutionInput
from elspeth.web.sessions.models import (
    composition_states_table,
    run_events_table,
    run_execution_inputs_table,
    run_start_permits_table,
    runs_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import RunAlreadyActiveError


def _admission(engine):
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="admission", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    fence = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="owner", lease_seconds=30
    )
    context = fence
    state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id), session_id=str(session.id), version=1, provenance="session_seed", created_at=datetime.now(UTC)
            )
        )
    envelope = RunExecutionInput(
        schema_version=1,
        envelope_json="{}",
        canonical_input_digest="a" * 64,
        topology_digest="b" * 64,
        source_manifest_digest="c" * 64,
        application_fingerprint="d" * 64,
        plugin_registry_fingerprint="e" * 64,
        configuration_fingerprint="f" * 64,
        graph_fingerprint="a" * 64,
        runtime_fingerprint="b" * 64,
        implementation_fingerprint="c" * 64,
        deployment_generation="test",
        session_epoch=1,
        landscape_epoch=1,
        coordination_protocol=1,
        automatic_recovery_eligible=True,
    )
    run_id = uuid4()

    def admit(tx):
        return tx.runs.create_pending_run(
            run_id=run_id, state_id=state_id, pipeline_yaml=None, started_at=datetime.now(UTC), execution_input=envelope
        )

    run = authority.mutate(context, admit)
    return authority, context, run, envelope


def test_atomic_admission_has_one_envelope_permit_and_owner(engine):
    authority, context, run, envelope = _admission(engine)
    with pytest.raises(RunAlreadyActiveError):
        authority.mutate(
            context,
            lambda tx: tx.runs.create_pending_run(
                run_id=uuid4(), state_id=run.state_id, pipeline_yaml=None, started_at=datetime.now(UTC), execution_input=envelope
            ),
        )
    with engine.connect() as conn:
        assert [
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (runs_table, run_execution_inputs_table, run_start_permits_table)
        ] == [1, 1, 1]
        row = conn.execute(select(runs_table)).one()
        assert row.owner_epoch == context.fence.operation_epoch
        assert row.saga_state == "start_intent"


def test_cancel_before_permit_forbids_dispatch_and_is_durable(engine):
    authority, context, run, _ = _admission(engine)
    cancel = RepositoryRunCancellationAuthority(engine)
    cancelled = cancel.request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local")
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested_at is not None
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id))
    assert permit.state is StartPermitState.CANCELLED_BEFORE_PERMIT
    assert permit.permit_id is None
    assert cancel.request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local") == cancelled
    with engine.connect() as conn:
        event = conn.execute(select(run_events_table).where(run_events_table.c.run_id == str(run.id))).one()
        assert event.event_type == "cancelled"
        assert event.sequence == 1
        assert event.timestamp.replace(tzinfo=UTC) == cancelled.finished_at
        assert event.data == {
            "status": "cancelled",
            "source_rows_processed": 0,
            "tokens_succeeded": 0,
            "tokens_failed": 0,
            "tokens_quarantined": 0,
            "tokens_routed_success": 0,
            "tokens_routed_failure": 0,
        }


def test_issued_permit_is_immutable_and_cancel_after_permit_is_cooperative(engine):
    authority, context, run, _ = _admission(engine)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id))
    assert permit.state is StartPermitState.START_PERMITTED
    cancelled = RepositoryRunCancellationAuthority(engine).request(
        run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local"
    )
    assert cancelled.status == "pending"
    assert cancelled.cancel_requested_at is not None
    assert authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id)) == permit
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


@pytest.mark.parametrize(
    ("user_id", "provider", "wrong_run"), [("mallory", "local", False), ("alice", "oidc", False), ("alice", "local", True)]
)
def test_peer_cancellation_rejects_confused_deputy_without_writes(engine, user_id, provider, wrong_run):
    _, _, run, _ = _admission(engine)
    with pytest.raises(SessionDerivedCustodyError):
        RepositoryRunCancellationAuthority(engine).request(
            uuid4() if wrong_run else run.id, session_id=run.session_id, user_id=user_id, auth_provider_type=provider
        )
    with engine.connect() as conn:
        assert conn.execute(select(runs_table.c.cancel_requested_at)).scalar_one() is None
        assert conn.execute(select(run_start_permits_table.c.start_state)).scalar_one() == "pending"


def test_expired_owner_cannot_issue_but_peer_can_cancel(engine):
    authority, context, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(run.session_id))
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id))
    assert RepositoryGlobalRunRecoveryAuthority(engine).list_recoverable_run_records()[0].id == run.id
    cancelled = RepositoryRunCancellationAuthority(engine).request(
        run.id, session_id=UUID(context.fence.session_id), user_id="alice", auth_provider_type="local"
    )
    assert cancelled.status == "cancelled"


def test_fresh_owner_adopts_only_linked_pending_output_custody(engine):
    from elspeth.web.sessions.models import blob_run_links_table, blobs_table

    authority, context, run, _ = _admission(engine)
    blob_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(run.session_id),
                filename="output.csv",
                mime_type="text/csv",
                size_bytes=0,
                storage_path="outputs/output.csv",
                created_at=datetime.now(UTC),
                created_by="pipeline",
                status="pending",
                custody_operation_id=context.fence.operation_id,
                custody_operation_epoch=context.fence.operation_epoch,
                custody_operation_kind="execute",
            )
        )
        conn.execute(insert(blob_run_links_table).values(blob_id=str(blob_id), run_id=str(run.id), direction="output"))
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(run.session_id))
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    fresh = authority.acquire(
        session_id=run.session_id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="replacement", lease_seconds=30
    )
    authority.mutate(fresh, lambda tx: tx.runs.rebind_run_ownership(run_id=run.id))
    outputs = authority.mutate(fresh, lambda tx: tx.blobs.list_pending_run_output_blobs(run_id=run.id))
    assert [blob.id for blob in outputs] == [blob_id]
    with engine.connect() as conn:
        blob = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).one()
        assert blob.custody_operation_id == fresh.fence.operation_id
        assert blob.custody_operation_epoch == fresh.fence.operation_epoch


def test_terminal_event_reconciliation_is_idempotent(engine):
    from elspeth.web.sessions.models import run_events_table

    authority, context, run, _ = _admission(engine)

    def append(tx):
        return tx.runs.append_terminal_run_event_once(
            run_id=run.id, timestamp=datetime.now(UTC), event_type="cancelled", data={"reason": "requested"}
        )

    first = authority.mutate(context, append)
    second = authority.mutate(context, append)
    assert first.id == second.id
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 1


def test_permit_creation_failure_rolls_back_admission_envelope_and_run(engine, monkeypatch):
    from elspeth.web.coordination.run_start_permit_authority import RepositoryRunStartPermitAuthority

    def fail_creation(connection_token, *, run_id):
        raise RuntimeError("simulated admission crash")

    monkeypatch.setattr(RepositoryRunStartPermitAuthority, "create_pending", fail_creation)
    with pytest.raises(RuntimeError, match="simulated admission crash"):
        _admission(engine)
    with engine.connect() as conn:
        for table in (runs_table, run_execution_inputs_table, run_start_permits_table):
            assert conn.execute(select(func.count()).select_from(table)).scalar_one() == 0


def test_archived_session_cannot_receive_durable_cancellation(engine):
    _, _, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(run.session_id)).values(archived_at=datetime.now(UTC)))
    with pytest.raises(SessionDerivedCustodyError):
        RepositoryRunCancellationAuthority(engine).request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local")
    with engine.connect() as conn:
        row = conn.execute(select(runs_table).where(runs_table.c.id == str(run.id))).one()
        assert row.cancel_requested_at is None
        assert row.status == "pending"
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0
