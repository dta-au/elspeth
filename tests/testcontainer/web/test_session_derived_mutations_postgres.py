"""Strict PostgreSQL custody and serialization proofs for derived mutations."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, event, func, insert, select, update

from elspeth.contracts.blobs import blob_record_snapshot_hash
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import (
    PostgresSessionOperationRepository,
    SessionDerivedCustodyError,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    audit_access_log_table,
    blob_deletion_cleanups_table,
    blob_inline_resolutions_table,
    blob_replacement_cleanups_table,
    blob_run_links_table,
    blobs_table,
    composition_proposals_table,
    composition_states_table,
    proposal_blob_effect_receipts_table,
    proposal_events_table,
    run_events_table,
    runs_table,
    sessions_table,
)
from elspeth.web.sessions.proposal_blob_effects import blob_record_snapshot_payload
from elspeth.web.sessions.protocol import AuditAccessLogWriteError
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_engine(external_deployment_postgres_url: str) -> Engine:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _create(repository: PostgresSessionOperationRepository, *, title: str):
    owner = f"postgres-owner-{uuid4()}"
    return repository.create_session_with_initial_fence(
        user_id="alice",
        title=title,
        auth_provider_type="local",
        owner_instance_id=owner,
        lease_seconds=30,
    )


def _seed_run_and_blob(
    engine: Engine,
    *,
    session_id: UUID,
    content_hash: str,
    size_bytes: int,
    storage_path: str | None = None,
) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    state_id = uuid4()
    run_id = uuid4()
    blob_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id),
                session_id=str(session_id),
                version=1,
                source=None,
                sources=None,
                nodes=None,
                edges=None,
                outputs=None,
                metadata_=None,
                is_valid=True,
                validation_errors=None,
                composer_meta=None,
                created_at=now,
                derived_from_state_id=None,
                provenance="session_seed",
            )
        )
        conn.execute(
            insert(runs_table).values(
                id=str(run_id),
                session_id=str(session_id),
                state_id=str(state_id),
                status="pending",
                started_at=now,
                finished_at=None,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )
        )
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(session_id),
                filename=f"{blob_id}.txt",
                mime_type="text/plain",
                size_bytes=size_bytes,
                content_hash=content_hash,
                storage_path=storage_path or f"/tmp/{blob_id}.txt",
                created_at=now,
                created_by="user",
                source_description=None,
                status="ready",
                creation_modality="verbatim",
            )
        )
    return run_id, blob_id


def _acquire(repository: PostgresSessionOperationRepository, *, session_id: UUID):
    return repository.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=f"postgres-owner-{uuid4()}",
        lease_seconds=30,
    )


def test_postgres_blob_effect_receipt_commits_atomically_with_replacement_metadata(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session = _create(repository, title="atomic proposal blob receipt")
    run_id, blob_id = _seed_run_and_blob(postgres_engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    proposal_id = uuid4()
    creation_event_id = uuid4()
    arguments = {"blob_id": str(blob_id), "content": "approved"}
    now = datetime.now(UTC)
    with postgres_engine.begin() as conn:
        conn.execute(update(runs_table).where(runs_table.c.id == str(run_id)).values(status="completed"))
        conn.execute(
            insert(composition_proposals_table).values(
                id=str(proposal_id),
                session_id=str(session.id),
                tool_call_id=f"call-{proposal_id}",
                user_message_id=None,
                tool_name="update_blob",
                status="pending",
                summary="Update approved blob",
                rationale="PostgreSQL atomicity proof",
                affects=["blob"],
                arguments_json=arguments,
                arguments_redacted_json=arguments,
                base_state_id=None,
                committed_state_id=None,
                audit_event_id=str(creation_event_id),
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(proposal_events_table).values(
                id=str(creation_event_id),
                session_id=str(session.id),
                proposal_id=str(proposal_id),
                event_type="proposal.created",
                actor="composer-web:user:alice",
                payload={
                    "schema": "tool_proposal_created.v1",
                    "tool_call_id": f"call-{proposal_id}",
                    "tool_name": "update_blob",
                    "status": "pending",
                },
                created_at=now,
            )
        )

    context = repository.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=f"postgres-owner-{uuid4()}",
        lease_seconds=30,
    )
    expected = repository.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(expected, size_bytes=8, content_hash="b" * 64)
    replacement_id = uuid4()
    plan = repository.mutate(
        context,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=replacement_id,
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.{replacement_id}.stage",
            backup_path=f"{expected.storage_path}.{replacement_id}.backup",
            max_storage_per_session=100,
            accepting_proposal_id=proposal_id,
        ),
    )
    staged = repository.mutate(
        context,
        lambda transaction: transaction.blobs.mark_blob_replacement_staged(plan=plan),
    )
    false_result = blob_record_snapshot_payload(expected)
    with postgres_engine.begin() as conn:
        conn.execute(
            insert(proposal_blob_effect_receipts_table).values(
                proposal_id=str(proposal_id),
                session_id=str(session.id),
                tool_name="update_blob",
                blob_id=str(blob_id),
                arguments_hash=stable_hash(arguments),
                result_blob_snapshot=false_result,
                result_blob_snapshot_hash=stable_hash(false_result),
                accepted_event_id=None,
                created_at=now,
                accepted_at=None,
            )
        )

    with pytest.raises(AuditIntegrityError, match="already has a durable receipt"):
        repository.mutate(
            context,
            lambda transaction: transaction.blobs.commit_blob_replacement(
                plan=staged,
                max_storage_per_session=100,
                accepting_proposal_id=proposal_id,
            ),
        )

    assert repository.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id)) == expected
    assert repository.mutate(context, lambda transaction: transaction.blobs.read_blob_replacement(blob_id=blob_id)) == staged
    with postgres_engine.begin() as conn:
        conn.execute(
            delete(proposal_blob_effect_receipts_table).where(proposal_blob_effect_receipts_table.c.proposal_id == str(proposal_id))
        )

    committed = repository.mutate(
        context,
        lambda transaction: transaction.blobs.commit_blob_replacement(
            plan=staged,
            max_storage_per_session=100,
            accepting_proposal_id=proposal_id,
        ),
    )

    assert committed.phase == "purge_pending"
    assert repository.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id)) == replacement
    with postgres_engine.connect() as conn:
        receipt = conn.execute(
            select(proposal_blob_effect_receipts_table).where(proposal_blob_effect_receipts_table.c.proposal_id == str(proposal_id))
        ).one()
    expected_result = blob_record_snapshot_payload(replacement)
    assert receipt.result_blob_snapshot == expected_result
    assert receipt.result_blob_snapshot_hash == stable_hash(expected_result)
    assert receipt.accepted_event_id is None


def test_postgres_rejects_foreign_parents_and_rolls_back_mixed_inline_batch(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    owned = _create(repository, title="owned")
    foreign = _create(repository, title="foreign")
    owned_run, owned_blob = _seed_run_and_blob(postgres_engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    foreign_run, foreign_blob = _seed_run_and_blob(
        postgres_engine,
        session_id=foreign.id,
        content_hash="b" * 64,
        size_bytes=5,
    )
    fence = _acquire(repository, session_id=owned.id)

    with pytest.raises(SessionDerivedCustodyError):
        repository.mutate(
            fence,
            lambda transaction: transaction.runs.append_run_event(
                run_id=foreign_run,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={},
            ),
        )
    with pytest.raises(SessionDerivedCustodyError):
        repository.mutate(
            fence,
            lambda transaction: transaction.blobs.insert_blob_run_link(
                blob_id=foreign_blob,
                run_id=owned_run,
                direction="input",
            ),
        )
    resolutions = (
        ResolvedBlobContent(
            field_path="source.options.first",
            blob_id=owned_blob,
            content_hash="a" * 64,
            byte_length=3,
            mime_type="text/plain",
            encoding="utf-8",
        ),
        ResolvedBlobContent(
            field_path="source.options.second",
            blob_id=foreign_blob,
            content_hash="b" * 64,
            byte_length=5,
            mime_type="text/plain",
            encoding="utf-8",
        ),
    )
    with pytest.raises(SessionDerivedCustodyError):
        repository.mutate(
            fence,
            lambda transaction: transaction.blobs.insert_blob_inline_resolutions(
                run_id=owned_run,
                attempt=1,
                resolutions=resolutions,
                resolved_at=datetime.now(UTC),
            ),
        )

    with postgres_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(run_events_table).where(run_events_table.c.run_id == str(foreign_run))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count()).select_from(blob_run_links_table).where(blob_run_links_table.c.run_id == str(owned_run))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(blob_inline_resolutions_table)
                .where(blob_inline_resolutions_table.c.run_id == str(owned_run))
            ).scalar_one()
            == 0
        )


def test_postgres_stale_event_writer_consumes_no_sequence(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session = _create(repository, title="stale writer")
    run_id, _blob_id = _seed_run_and_blob(postgres_engine, session_id=session.id, content_hash="c" * 64, size_bytes=7)
    stale = _acquire(repository, session_id=session.id)
    first = repository.mutate(
        stale,
        lambda transaction: transaction.runs.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="progress",
            data={"writer": "first"},
        ),
    )
    repository.release(stale)
    current = _acquire(repository, session_id=session.id)

    with pytest.raises(SessionOperationFenceLost):
        repository.mutate(
            stale,
            lambda transaction: transaction.runs.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"writer": "stale"},
            ),
        )
    second = repository.mutate(
        current,
        lambda transaction: transaction.runs.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="completed",
            data={"writer": "current"},
        ),
    )

    assert (first.sequence, second.sequence) == (1, 2)


def test_postgres_plugin_crash_breadcrumb_rejects_stale_predecessor(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session = _create(repository, title="plugin crash breadcrumb")
    predecessor = repository.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"postgres-owner-{uuid4()}",
        lease_seconds=30,
    )
    repository.mutate(
        predecessor,
        lambda transaction: transaction.session.record_plugin_crash_breadcrumb(),
    )
    with postgres_engine.connect() as conn:
        after_predecessor = conn.execute(select(sessions_table.c.updated_at).where(sessions_table.c.id == str(session.id))).scalar_one()
    assert after_predecessor > session.updated_at

    repository.release(predecessor)
    successor = repository.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"postgres-owner-{uuid4()}",
        lease_seconds=30,
    )
    with pytest.raises(SessionOperationFenceLost):
        repository.mutate(
            predecessor,
            lambda transaction: transaction.session.record_plugin_crash_breadcrumb(),
        )
    with postgres_engine.connect() as conn:
        after_stale_attempt = conn.execute(select(sessions_table.c.updated_at).where(sessions_table.c.id == str(session.id))).scalar_one()
    assert after_stale_attempt == after_predecessor

    repository.mutate(
        successor,
        lambda transaction: transaction.session.record_plugin_crash_breadcrumb(),
    )
    with postgres_engine.connect() as conn:
        after_successor = conn.execute(select(sessions_table.c.updated_at).where(sessions_table.c.id == str(session.id))).scalar_one()
    assert after_successor > after_stale_attempt


@pytest.mark.parametrize("first_actor", ["audit", "archive"])
def test_postgres_audit_access_and_archive_delete_serialize_in_both_orders(
    postgres_engine: Engine,
    external_deployment_postgres_url: str,
    first_actor: str,
) -> None:
    audit_engine = create_session_engine(external_deployment_postgres_url)
    archive_engine = create_session_engine(external_deployment_postgres_url)
    audit_authority = RepositoryAuditAccessLogAuthority(audit_engine)
    archive_repository = PostgresSessionOperationRepository(archive_engine)
    session = _create(archive_repository, title=f"audit archive race {first_actor}")
    archive_context = archive_repository.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"postgres-archive-{uuid4()}",
        lease_seconds=30,
    )
    first_entered = Event()
    release_first = Event()
    contender_statement_entered = Event()

    def block_audit_insert(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into audit_access_log"):
            first_entered.set()
            assert release_first.wait(timeout=10)

    def observe_audit_subject_read(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if " from sessions " in f" {normalized} ":
            contender_statement_entered.set()

    def block_archive_delete(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("delete from sessions"):
            first_entered.set()
            assert release_first.wait(timeout=10)

    def observe_archive_delete(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("delete from sessions"):
            contender_statement_entered.set()

    def record_access():
        return audit_authority.record_audit_grade_view(
            session_id=str(session.id),
            requesting_principal="alice",
            auth_provider_type="local",
            request_path=f"/api/sessions/{session.id}/messages",
            query_args={"include_tool_rows": "true"},
            ip_address=None,
        )

    first_listener = block_audit_insert if first_actor == "audit" else block_archive_delete
    contender_listener = observe_archive_delete if first_actor == "audit" else observe_audit_subject_read
    first_engine = audit_engine if first_actor == "audit" else archive_engine
    contender_engine = archive_engine if first_actor == "audit" else audit_engine
    event.listen(first_engine, "before_cursor_execute", first_listener)
    event.listen(contender_engine, "before_cursor_execute", contender_listener)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            if first_actor == "audit":
                first_future = pool.submit(record_access)
                assert first_entered.wait(timeout=10)
                contender_future = pool.submit(archive_repository.archive_delete, archive_context)
            else:
                first_future = pool.submit(archive_repository.archive_delete, archive_context)
                assert first_entered.wait(timeout=10)
                contender_future = pool.submit(record_access)
            assert not contender_statement_entered.wait(timeout=0.25)
            release_first.set()
            first_future.result(timeout=10)
            if first_actor == "audit":
                contender_future.result(timeout=10)
            else:
                with pytest.raises(AuditAccessLogWriteError):
                    contender_future.result(timeout=10)
            assert contender_statement_entered.wait(timeout=10)
    finally:
        release_first.set()
        event.remove(first_engine, "before_cursor_execute", first_listener)
        event.remove(contender_engine, "before_cursor_execute", contender_listener)
        audit_engine.dispose()
        archive_engine.dispose()

    with postgres_engine.connect() as conn:
        assert (
            conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.id == str(session.id))).scalar_one() == 0
        )
        assert (
            conn.execute(
                select(func.count()).select_from(audit_access_log_table).where(audit_access_log_table.c.session_id == str(session.id))
            ).scalar_one()
            == 0
        )


def test_postgres_concurrent_event_writers_allocate_monotonic_sequences(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session = _create(repository, title="concurrent writers")
    run_id, _blob_id = _seed_run_and_blob(postgres_engine, session_id=session.id, content_hash="d" * 64, size_bytes=11)
    fence = _acquire(repository, session_id=session.id)

    def append(writer: int):
        contender = PostgresSessionOperationRepository(postgres_engine)
        return contender.mutate(
            fence,
            lambda transaction: transaction.runs.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"writer": writer},
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = tuple(pool.map(append, (1, 2)))

    assert sorted(record.sequence for record in records) == [1, 2]
    replay = repository.mutate(fence, lambda transaction: transaction.runs.list_run_events_after(run_id=run_id, after_sequence=0))
    assert tuple(event.sequence for event in replay) == (1, 2)


def test_postgres_replacement_and_deletion_race_admits_exactly_one_recoverable_ledger(
    postgres_engine: Engine,
    external_deployment_postgres_url: str,
    tmp_path: Path,
) -> None:
    seed_repository = PostgresSessionOperationRepository(postgres_engine)
    session = _create(seed_repository, title="replacement deletion race")
    old_content = b"old durable bytes"
    storage = tmp_path / "race.txt"
    storage.write_bytes(old_content)
    run_id, blob_id = _seed_run_and_blob(
        postgres_engine,
        session_id=session.id,
        content_hash=hashlib.sha256(old_content).hexdigest(),
        size_bytes=len(old_content),
        storage_path=str(storage),
    )
    with postgres_engine.begin() as conn:
        conn.execute(update(runs_table).where(runs_table.c.id == str(run_id)).values(status="completed"))
    context = seed_repository.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"postgres-owner-{uuid4()}",
        lease_seconds=30,
    )
    expected = seed_repository.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(
        expected,
        size_bytes=len(b"new durable bytes"),
        content_hash=hashlib.sha256(b"new durable bytes").hexdigest(),
    )
    replacement_id = uuid4()
    staging = storage.with_name(f".{storage.name}.{replacement_id}.stage")
    backup = storage.with_name(f".{storage.name}.{replacement_id}.backup")
    tombstone = storage.with_name(f".{storage.name}.delete")
    replacement_engine = create_session_engine(external_deployment_postgres_url)
    deletion_engine = create_session_engine(external_deployment_postgres_url)
    replacement_repository = PostgresSessionOperationRepository(replacement_engine)
    deletion_repository = PostgresSessionOperationRepository(deletion_engine)
    start = Barrier(2)

    def prepare_replacement() -> str:
        start.wait()
        try:
            replacement_repository.mutate(
                replace(context),
                lambda transaction: transaction.blobs.prepare_blob_replacement(
                    replacement_id=replacement_id,
                    expected=expected,
                    replacement=replacement,
                    staging_path=str(staging),
                    backup_path=str(backup),
                    max_storage_per_session=1024,
                    accepting_proposal_id=None,
                ),
            )
        except AuditIntegrityError:
            return "rejected"
        return "replacement"

    def prepare_deletion() -> str:
        start.wait()
        try:
            deletion_repository.mutate(
                replace(context),
                lambda transaction: transaction.blobs.prepare_blob_deletion(
                    blob_id=blob_id,
                    tombstone_path=str(tombstone),
                    blob_snapshot_hash=blob_record_snapshot_hash(expected),
                    expected_file_present=True,
                    expected_file_size=expected.size_bytes,
                    expected_file_hash=expected.content_hash,
                    accepting_proposal_id=None,
                ),
            )
        except AuditIntegrityError:
            return "rejected"
        return "deletion"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replacement_future = pool.submit(prepare_replacement)
            deletion_future = pool.submit(prepare_deletion)
            outcomes = {replacement_future.result(), deletion_future.result()}
    finally:
        replacement_engine.dispose()
        deletion_engine.dispose()

    assert outcomes in ({"replacement", "rejected"}, {"deletion", "rejected"})
    with postgres_engine.connect() as conn:
        replacement_row = conn.execute(
            select(blob_replacement_cleanups_table).where(blob_replacement_cleanups_table.c.blob_id == str(blob_id))
        ).one_or_none()
        deletion_row = conn.execute(
            select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(blob_id))
        ).one_or_none()
        durable_blob = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).one()
    assert (replacement_row is None) is not (deletion_row is None)
    assert durable_blob.size_bytes == expected.size_bytes
    assert durable_blob.content_hash == expected.content_hash
    assert storage.read_bytes() == old_content
    assert tuple(storage.parent.iterdir()) == (storage,)
    if replacement_row is not None:
        assert replacement_row.phase == "intent"
        assert replacement_row.old_blob_snapshot_hash == blob_record_snapshot_hash(expected)
    else:
        assert deletion_row is not None
        assert deletion_row.phase == "intent"
        assert deletion_row.blob_snapshot_hash == blob_record_snapshot_hash(expected)


def test_postgres_output_read_checks_blob_custody(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    owned = _create(repository, title="owned output")
    foreign = _create(repository, title="foreign output")
    owned_run, owned_blob = _seed_run_and_blob(postgres_engine, session_id=owned.id, content_hash="e" * 64, size_bytes=13)
    _foreign_run, foreign_blob = _seed_run_and_blob(
        postgres_engine,
        session_id=foreign.id,
        content_hash="f" * 64,
        size_bytes=17,
    )
    fence = _acquire(repository, session_id=owned.id)
    repository.mutate(
        fence,
        lambda transaction: transaction.blobs.insert_blob_run_link(blob_id=owned_blob, run_id=owned_run, direction="output"),
    )
    with postgres_engine.begin() as conn:
        conn.execute(insert(blob_run_links_table).values(blob_id=str(foreign_blob), run_id=str(owned_run), direction="output"))

    with pytest.raises(SessionDerivedCustodyError):
        repository.mutate(fence, lambda transaction: transaction.blobs.list_run_output_blobs(run_id=owned_run))
