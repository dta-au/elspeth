"""Custody proofs for fenced mutations over Sessions-derived tables."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, insert, select

from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import (
    blob_inline_resolutions_table,
    blob_run_links_table,
    blobs_table,
    composition_states_table,
    run_events_table,
    runs_table,
)


def _create(authority: SQLiteLocalSessionOperationAuthority, *, title: str):
    return authority.create_session_with_initial_fence(
        user_id="alice",
        title=title,
        auth_provider_type="local",
        owner_instance_id="sqlite-owner",
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


def _acquire(authority: SQLiteLocalSessionOperationAuthority, *, session_id: UUID):
    return authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )


def test_derived_mutations_reject_foreign_parents_and_raw_execute(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    owned = _create(authority, title="owned")
    foreign = _create(authority, title="foreign")
    _owned_run, owned_blob = _seed_run_and_blob(engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    foreign_run, foreign_blob = _seed_run_and_blob(engine, session_id=foreign.id, content_hash="b" * 64, size_bytes=5)
    fence = _acquire(authority, session_id=owned.id)

    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.append_run_event(
                run_id=foreign_run,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"phase": "forbidden"},
            ),
        )
    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.insert_blob_run_link(
                blob_id=foreign_blob,
                run_id=_owned_run,
                direction="input",
            ),
        )
    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.insert_blob_run_link(
                blob_id=owned_blob,
                run_id=foreign_run,
                direction="input",
            ),
        )
    with pytest.raises(ValueError, match="directly session-scoped"):
        authority.mutate(
            fence,
            lambda transaction: transaction.execute(
                insert(run_events_table).values(
                    id=str(uuid4()),
                    run_id=str(_owned_run),
                    sequence=1,
                    timestamp=datetime.now(UTC),
                    event_type="progress",
                    data={},
                )
            ),
        )

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(blob_run_links_table)).scalar_one() == 0


def test_inline_resolution_batch_rolls_back_when_one_blob_is_foreign(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    owned = _create(authority, title="owned")
    foreign = _create(authority, title="foreign")
    owned_run, owned_blob = _seed_run_and_blob(engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    _foreign_run, foreign_blob = _seed_run_and_blob(engine, session_id=foreign.id, content_hash="b" * 64, size_bytes=5)
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
    fence = _acquire(authority, session_id=owned.id)

    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.insert_blob_inline_resolutions(
                run_id=owned_run,
                attempt=1,
                resolutions=resolutions,
                resolved_at=datetime.now(UTC),
            ),
        )

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(blob_inline_resolutions_table)).scalar_one() == 0


def test_stale_event_writer_consumes_no_sequence(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="events")
    run_id, _blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    stale = _acquire(authority, session_id=session.id)
    first = authority.mutate(
        stale,
        lambda transaction: transaction.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="progress",
            data={"step": 1},
        ),
    )
    authority.release(stale)
    current = _acquire(authority, session_id=session.id)

    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(
            stale,
            lambda transaction: transaction.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"step": "stale"},
            ),
        )
    second = authority.mutate(
        current,
        lambda transaction: transaction.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="completed",
            data={"step": 2},
        ),
    )
    replay = authority.mutate(current, lambda transaction: transaction.list_run_events_after(run_id=run_id, after_sequence=0))

    assert (first.sequence, second.sequence) == (1, 2)
    assert tuple(event.sequence for event in replay) == (1, 2)
    assert tuple(event.data["step"] for event in replay) == (1, 2)


def test_output_reads_fail_closed_on_cross_session_link(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    owned = _create(authority, title="owned")
    foreign = _create(authority, title="foreign")
    owned_run, owned_blob = _seed_run_and_blob(engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    _foreign_run, foreign_blob = _seed_run_and_blob(engine, session_id=foreign.id, content_hash="b" * 64, size_bytes=5)
    fence = _acquire(authority, session_id=owned.id)
    inserted = authority.mutate(
        fence,
        lambda transaction: transaction.insert_blob_run_link(blob_id=owned_blob, run_id=owned_run, direction="output"),
    )
    duplicate = authority.mutate(
        fence,
        lambda transaction: transaction.insert_blob_run_link(blob_id=owned_blob, run_id=owned_run, direction="output"),
    )
    links = authority.mutate(fence, lambda transaction: transaction.list_blob_run_links(blob_id=owned_blob))
    with engine.begin() as conn:
        conn.execute(insert(blob_run_links_table).values(blob_id=str(foreign_blob), run_id=str(owned_run), direction="output"))

    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(fence, lambda transaction: transaction.list_run_output_blobs(run_id=owned_run))

    assert inserted is True
    assert duplicate is False
    assert [(link.blob_id, link.run_id, link.direction) for link in links] == [(owned_blob, owned_run, "output")]
