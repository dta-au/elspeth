"""Real PostgreSQL admission contention: custody, live fences and quota policies."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update
from tests.fixtures.resource_identity import UUID_SPELLINGS, uuid_spelling
from tests.testcontainer.web import test_quota_authority_postgres as quota_fixtures
from tests.testcontainer.web.test_quota_authority_postgres import _wait_for_database_lock
from tests.unit.web.auth.test_audit import _durable_recorder, _durable_rows, _metadata
from tests.unit.web.blobs.test_mutation_admission import _context, _empty_state, _resources

from elspeth.contracts.blobs import blob_record_snapshot_hash
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl, _blob_operation_path_token, content_hash
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tools.blobs import admit_blob_mutation
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_replacement_cleanups_table,
    chat_messages_table,
    output_storage_reservations_table,
    quota_policies_table,
    session_operation_fences_table,
)

pytestmark = pytest.mark.testcontainer
pg_fenced = quota_fixtures.pg_fenced


def _setup(fenced, tmp_path):
    recorder, url = _durable_recorder(tmp_path)
    repository = PostgresSessionOperationRepository(fenced.engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    operation = repository.acquire(
        session_id=UUID(fenced.session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="blob-admission",
        lease_seconds=120,
    )
    service = BlobServiceImpl(fenced.engine, tmp_path, session_operation_authority=repository)
    record = asyncio.run(service.create_blob(UUID(fenced.session_id), "admit.csv", b"old", "text/csv", session_operation_context=operation))
    message_id = str(uuid4())
    with fenced.engine.begin() as connection:
        connection.execute(
            chat_messages_table.insert().values(
                id=message_id,
                session_id=fenced.session_id,
                role="user",
                content="new larger",
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
            )
        )
    context = replace(_context(service, fenced.engine, operation, tmp_path), user_message_id=message_id)
    return repository, service, record, context, recorder, url


@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
def test_postgres_bound_selector_alias_refuses_without_resource_effect(pg_fenced, tmp_path, tool_name, spelling):
    repository, _service, record, context, _recorder, _url = _setup(pg_fenced, tmp_path)
    try:
        state = replace(
            _empty_state(),
            sources={
                "source": SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    on_validation_failure="discard",
                    options={"path": "elsewhere.csv", "blob_ref": str(record.id)},
                )
            },
        )
        arguments = {"blob_id": uuid_spelling(str(record.id), spelling), **({"content": "new"} if tool_name == "update_blob" else {})}
        before = _resources(pg_fenced.engine, tmp_path / "blobs")
        result = admit_blob_mutation(tool_name, arguments, state, context)
        assert result is not None and not result.success
        assert "current composition" in result.validation.errors[0].message
        assert result.updated_state is state
        assert _resources(pg_fenced.engine, tmp_path / "blobs") == before
    finally:
        repository.release(context.session_operation_context)


@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("spelling", ["canonical", "braced", "urn"])
def test_postgres_admission_waiter_observes_real_writer_debt_without_allocating(pg_fenced, tmp_path, tool_name, spelling):
    repository, service, record, context, recorder, _url = _setup(pg_fenced, tmp_path)
    operation = context.session_operation_context
    arguments = {"blob_id": uuid_spelling(str(record.id), spelling), **({"content": "new"} if tool_name == "update_blob" else {})}
    before = _resources(pg_fenced.engine, tmp_path / "blobs")
    assert admit_blob_mutation(tool_name, arguments, _empty_state(), context) is None
    assert _resources(pg_fenced.engine, tmp_path / "blobs") == before
    contender_engine = create_session_engine(pg_fenced.engine.url.render_as_string(hide_password=False))
    contender = PostgresSessionOperationRepository(contender_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    contender_context = replace(context, session_engine=contender_engine, session_operation_authority=contender)
    writer_entered, release_writer, waiter_started = Event(), Event(), Event()
    pid = []
    token = _blob_operation_path_token(
        operation_id=operation.fence.operation_id, operation_epoch=operation.fence.operation_epoch, operation_kind=operation.operation_kind
    )
    replacement_id = uuid4()
    storage = Path(record.storage_path)

    def hold_intent(transaction):
        if tool_name == "update_blob":
            plan = transaction.blobs.prepare_blob_replacement(
                replacement_id=replacement_id,
                expected=record,
                replacement=replace(record, content_hash=content_hash(b"new")),
                staging_path=str(storage.with_name(f".{record.id}.replace-{token}-{replacement_id}.stage")),
                backup_path=str(storage.with_name(f".{record.id}.replace-{token}-{replacement_id}.backup")),
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
        else:
            plan = transaction.blobs.prepare_blob_deletion(
                blob_id=record.id,
                tombstone_path=str(storage.with_name(f".{record.id}.delete-{token}")),
                blob_snapshot_hash=blob_record_snapshot_hash(record),
                expected_file_present=True,
                expected_file_size=record.size_bytes,
                expected_file_hash=record.content_hash,
                accepting_proposal_id=None,
            )
        writer_entered.set()
        assert release_writer.wait(10)
        return plan

    def observe_waiter(connection, _cursor, statement, _parameters, _execution, _many):
        if "pg_advisory_xact_lock" in statement and not pid:
            pid.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            waiter_started.set()

    event.listen(contender_engine, "before_cursor_execute", observe_waiter)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            writer = workers.submit(repository.mutate, operation, hold_intent)
            assert writer_entered.wait(5)
            waiter = workers.submit(admit_blob_mutation, tool_name, arguments, _empty_state(), contender_context)
            try:
                assert waiter_started.wait(5)
                _wait_for_database_lock(pg_fenced, pid[0])
                assert not waiter.done()
            finally:
                release_writer.set()
            plan = writer.result(timeout=5)
            result = waiter.result(timeout=5)
        assert result is not None
        assert not result.success
        assert any("recovery" in error.message.lower() for error in result.validation.errors)
        with pg_fenced.engine.connect() as connection:
            replacements = connection.execute(
                select(blob_replacement_cleanups_table).where(blob_replacement_cleanups_table.c.blob_id == str(record.id))
            ).all()
            deletions = connection.execute(
                select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            ).all()
            assert len(replacements) + len(deletions) == 1
            assert (replacements or deletions)[0].phase == plan.phase == "intent"
        assert storage.read_bytes() == b"old"
        if tool_name == "update_blob":
            BlobReplacementCoordinator(engine=pg_fenced.engine, data_dir=tmp_path, session_operation_authority=repository).reconcile(
                context=operation
            )
        else:
            asyncio.run(service.delete_blob(record.id, session_operation_context=operation))
    finally:
        release_writer.set()
        event.remove(contender_engine, "before_cursor_execute", observe_waiter)
        contender_engine.dispose()
        recorder.close()


@pytest.mark.parametrize("race", ["expiry", "metadata"])
def test_postgres_admission_releases_sessions_during_file_io_and_rechecks(pg_fenced, tmp_path, monkeypatch, race):
    repository, _service, record, context, recorder, _url = _setup(pg_fenced, tmp_path)
    entered, resume = Event(), Event()
    original = BlobReplacementCoordinator._read_bytes

    def pause(path, directory_fd):
        entered.set()
        assert resume.wait(10)
        return original(path, directory_fd)

    monkeypatch.setattr(BlobReplacementCoordinator, "_read_bytes", staticmethod(pause))
    from elspeth.web.sessions.models import blobs_table

    with ThreadPoolExecutor(max_workers=2) as workers:
        admission = workers.submit(
            admit_blob_mutation, "update_blob", {"blob_id": str(record.id), "content": "new"}, _empty_state(), context
        )
        try:
            assert entered.wait(5)
            # A real renew requires SESSIONS. It must finish while admission is
            # paused holding BLOB_CUSTODY, proving no DB transaction spans I/O.
            renewed = workers.submit(repository.renew, context.session_operation_context, lease_seconds=120)
            assert renewed.result(timeout=2).fence.operation_id == context.session_operation_context.fence.operation_id
            with pg_fenced.engine.begin() as connection:
                if race == "expiry":
                    connection.execute(
                        update(session_operation_fences_table)
                        .where(session_operation_fences_table.c.session_id == pg_fenced.session_id)
                        .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
                    )
                else:
                    connection.execute(
                        update(blobs_table).where(blobs_table.c.id == str(record.id)).values(source_description="changed during read")
                    )
        finally:
            resume.set()
        with pytest.raises(SessionOperationFenceLost if race == "expiry" else AuditIntegrityError):
            admission.result(timeout=5)
    assert Path(record.storage_path).read_bytes() == b"old"
    with pg_fenced.engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
        assert connection.execute(select(blob_deletion_cleanups_table)).all() == []
    recorder.close()


def test_postgres_admission_waiting_on_container_sees_committed_output_reservation(pg_fenced, tmp_path):
    _repository, _service, record, context, recorder, url = _setup(pg_fenced, tmp_path)
    from tests.fixtures.identities import ensure_test_identity

    from elspeth.web.sessions.models import composition_states_table, runs_table, sessions_table

    other_session, state_id, run_id = (str(uuid4()) for _ in range(3))
    now = datetime.now(UTC)
    with pg_fenced.engine.begin() as connection:
        ensure_test_identity(connection, identity_id="bob")
        connection.execute(
            sessions_table.insert().values(
                id=other_session, user_id="bob", auth_provider_type="local", title="other owner", created_at=now, updated_at=now
            )
        )
        connection.execute(
            composition_states_table.insert().values(
                id=state_id, session_id=other_session, version=1, is_valid=True, provenance="session_seed", created_at=now
            )
        )
        connection.execute(
            runs_table.insert().values(id=run_id, session_id=other_session, state_id=state_id, status="completed", started_at=now)
        )
        connection.execute(
            quota_policies_table.insert().values(
                policy_id="container-storage", identity_id=None, storage_bytes=8, set_by_actor="operator", set_at=now
            )
        )
    contender_engine = create_session_engine(pg_fenced.engine.url.render_as_string(hide_password=False))
    contender = PostgresSessionOperationRepository(contender_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    contender_context = replace(
        context, session_engine=contender_engine, session_operation_authority=contender, user_message_content="larger"
    )
    pid, started = [], Event()

    def observe_waiter(connection, _cursor, statement, _parameters, _execution, _many):
        if "quota_policies" in statement and "FOR UPDATE" in statement and not pid:
            pid.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            started.set()

    event.listen(contender_engine, "before_cursor_execute", observe_waiter)
    try:
        # A genuine accepting writer reserves six bytes for another identity
        # while holding the shared container policy, before admission measures.
        from elspeth.web.coordination.quota_authority import admit_storage_bytes_on_connection

        with ThreadPoolExecutor(max_workers=1) as workers, pg_fenced.engine.begin() as writer:
            admit_storage_bytes_on_connection(
                writer, session_id=other_session, additional_bytes=5, operation="run_output_finalize", record=recorder.record_quota_exceeded
            )
            writer.execute(
                output_storage_reservations_table.insert().values(
                    target_path=str(tmp_path / "other.csv"),
                    session_id=other_session,
                    run_id=run_id,
                    size_bytes=5,
                    custody_operation_id="execute-other",
                    custody_operation_epoch=1,
                )
            )
            admission = workers.submit(
                admit_blob_mutation, "update_blob", {"blob_id": str(record.id), "content": "larger"}, _empty_state(), contender_context
            )
            assert started.wait(5)
            _wait_for_database_lock(pg_fenced, pid[0])
            writer.commit()
            assert admission.result(timeout=5) is not None
        rows = _durable_rows(url)
        assert len(rows) == 1
        payload = _metadata(rows[0])
        assert (payload["usage"], payload["ceiling"], payload["scope"]) == (8, 8, "container")
        with pg_fenced.engine.connect() as connection:
            assert connection.execute(select(output_storage_reservations_table.c.size_bytes)).all() == [(5,)]
            assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
        assert Path(record.storage_path).read_bytes() == b"old"
    finally:
        event.remove(contender_engine, "before_cursor_execute", observe_waiter)
        contender_engine.dispose()
        recorder.close()


def test_postgres_real_writer_waits_for_read_only_admission_custody(pg_fenced, tmp_path, monkeypatch):
    _repository, _service, record, context, recorder, _url = _setup(pg_fenced, tmp_path)
    entered, resume, writer_started = Event(), Event(), Event()
    original_read = BlobReplacementCoordinator._read_bytes

    def pause(path, directory_fd):
        entered.set()
        assert resume.wait(10)
        return original_read(path, directory_fd)

    monkeypatch.setattr(BlobReplacementCoordinator, "_read_bytes", staticmethod(pause))
    writer_engine = create_session_engine(pg_fenced.engine.url.render_as_string(hide_password=False))
    writer_authority = PostgresSessionOperationRepository(writer_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    writer_service = BlobServiceImpl(writer_engine, tmp_path, session_operation_authority=writer_authority)
    pid = []

    def observe_writer(connection, _cursor, statement, _parameters, _execution, _many):
        if "pg_advisory_lock(" in statement and not pid:
            pid.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            writer_started.set()

    event.listen(writer_engine, "before_cursor_execute", observe_writer)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            admission = workers.submit(
                admit_blob_mutation, "update_blob", {"blob_id": str(record.id), "content": "new"}, _empty_state(), context
            )
            assert entered.wait(5)
            writer = workers.submit(
                asyncio.run, writer_service.delete_blob(record.id, session_operation_context=context.session_operation_context)
            )
            try:
                assert writer_started.wait(5)
                _wait_for_database_lock(pg_fenced, pid[0])
                assert not writer.done()
            finally:
                resume.set()
            assert admission.result(timeout=5) is None
            writer.result(timeout=5)
        assert not Path(record.storage_path).exists()
        with pg_fenced.engine.connect() as connection:
            assert connection.execute(select(blob_deletion_cleanups_table)).all() == []
            assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
    finally:
        resume.set()
        event.remove(writer_engine, "before_cursor_execute", observe_writer)
        writer_engine.dispose()
        recorder.close()


def test_postgres_admission_fence_waiter_cannot_read_after_expiry(pg_fenced, tmp_path, monkeypatch):
    _repository, _service, record, context, recorder, _url = _setup(pg_fenced, tmp_path)
    contender_engine = create_session_engine(pg_fenced.engine.url.render_as_string(hide_password=False))
    contender = PostgresSessionOperationRepository(contender_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    contender_context = replace(context, session_engine=contender_engine, session_operation_authority=contender)
    started, pid = Event(), []

    def observe_waiter(connection, _cursor, statement, _parameters, _execution, _many):
        if "session_operation_fences" in statement and "FOR UPDATE" in statement and not pid:
            pid.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            started.set()

    def forbidden_read(*args, **kwargs):
        pytest.fail("expired admission reached filesystem content")

    monkeypatch.setattr(BlobReplacementCoordinator, "_read_bytes", forbidden_read)
    event.listen(contender_engine, "before_cursor_execute", observe_waiter)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers, pg_fenced.engine.begin() as blocker:
            blocker.execute(
                select(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id == pg_fenced.session_id)
                .with_for_update()
            ).one()
            waiter = workers.submit(
                admit_blob_mutation, "update_blob", {"blob_id": str(record.id), "content": "new"}, _empty_state(), contender_context
            )
            assert started.wait(5)
            _wait_for_database_lock(pg_fenced, pid[0])
            blocker.execute(
                update(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id == pg_fenced.session_id)
                .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
            )
            blocker.commit()
            with pytest.raises(SessionOperationFenceLost):
                waiter.result(timeout=5)
        assert Path(record.storage_path).read_bytes() == b"old"
        with pg_fenced.engine.connect() as connection:
            assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
            assert connection.execute(select(blob_deletion_cleanups_table)).all() == []
    finally:
        event.remove(contender_engine, "before_cursor_execute", observe_waiter)
        contender_engine.dispose()
        recorder.close()
