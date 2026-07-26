"""Strict PostgreSQL custody and serialization proofs for derived mutations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, insert, select

from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionDerivedCustodyError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blob_inline_resolutions_table,
    blob_run_links_table,
    blobs_table,
    composition_states_table,
    run_events_table,
    runs_table,
)
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
                storage_path=f"/tmp/{blob_id}.txt",
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
            lambda transaction: transaction.append_run_event(
                run_id=foreign_run,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={},
            ),
        )
    with pytest.raises(SessionDerivedCustodyError):
        repository.mutate(
            fence,
            lambda transaction: transaction.insert_blob_run_link(
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
            lambda transaction: transaction.insert_blob_inline_resolutions(
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
        lambda transaction: transaction.append_run_event(
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
            lambda transaction: transaction.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"writer": "stale"},
            ),
        )
    second = repository.mutate(
        current,
        lambda transaction: transaction.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="completed",
            data={"writer": "current"},
        ),
    )

    assert (first.sequence, second.sequence) == (1, 2)


def test_postgres_concurrent_event_writers_allocate_monotonic_sequences(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session = _create(repository, title="concurrent writers")
    run_id, _blob_id = _seed_run_and_blob(postgres_engine, session_id=session.id, content_hash="d" * 64, size_bytes=11)
    fence = _acquire(repository, session_id=session.id)

    def append(writer: int):
        contender = PostgresSessionOperationRepository(postgres_engine)
        return contender.mutate(
            fence,
            lambda transaction: transaction.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"writer": writer},
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = tuple(pool.map(append, (1, 2)))

    assert sorted(record.sequence for record in records) == [1, 2]
    replay = repository.mutate(fence, lambda transaction: transaction.list_run_events_after(run_id=run_id, after_sequence=0))
    assert tuple(event.sequence for event in replay) == (1, 2)


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
        lambda transaction: transaction.insert_blob_run_link(blob_id=owned_blob, run_id=owned_run, direction="output"),
    )
    with postgres_engine.begin() as conn:
        conn.execute(insert(blob_run_links_table).values(blob_id=str(foreign_blob), run_id=str(owned_run), direction="output"))

    with pytest.raises(SessionDerivedCustodyError):
        repository.mutate(fence, lambda transaction: transaction.list_run_output_blobs(run_id=owned_run))
