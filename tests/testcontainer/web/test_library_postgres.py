"""A real PostgreSQL row lock admits one curator decision for an entry."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, get_ident
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.library_authority import (
    CuratorAuthorityRequired,
    LibraryEntryAlreadyCurated,
    LibraryEntryNotForkable,
    RepositoryLibraryAuthority,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identity_roles_table, library_entries_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def library_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"library_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        yield engine
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="src_out",
            options={"path": "/data/in.csv", "schema": {"fields": ["a"]}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="pass",
                node_type="transform",
                plugin="passthrough",
                input="src_out",
                on_success="out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="csv", options={"path": "/data/out.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(name="race", description="concurrent accept"),
        version=1,
    )


def _grant_role(engine: Engine, identity_id: str, role: str) -> str:
    role_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=role_id,
                identity_id=identity_id,
                role=role,
                expires_at=None,
                note=None,
                scope=None,
                granted_by_identity_id=identity_id,
                granted_at=datetime.now(UTC),
                revoked_at=None,
            )
        )
    return role_id


def test_concurrent_accept_has_exactly_one_winner(library_engine: Engine, tmp_path: Path) -> None:
    with library_engine.begin() as conn:
        for identity_id in ("alice", "carol", "dave"):
            ensure_test_identity(conn, identity_id=identity_id)
    _grant_role(library_engine, "alice", "user")
    _grant_role(library_engine, "carol", "curator")
    _grant_role(library_engine, "dave", "curator")
    authority = RepositoryLibraryAuthority(library_engine, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    entry = authority.publish(
        session_id=str(uuid4()),
        state=_state(),
        title="race",
        published_by="alice",
        compartment_id="alpha",
        record=lambda event: None,
    )
    gate = Barrier(2)

    def accept(curator: str) -> str | BaseException:
        gate.wait(timeout=30)
        try:
            accepted = authority.accept(entry_id=entry.entry_id, curator=curator, note=None, record=lambda event: None)
            if accepted.curated_by_identity_id is None:
                raise AssertionError("accepted entry has no curator")
            return accepted.curated_by_identity_id
        except LibraryEntryAlreadyCurated as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, ("carol", "dave")))

    winners = [outcome for outcome in outcomes if isinstance(outcome, str)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, LibraryEntryAlreadyCurated)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert losers[0].current_state == "accepted"
    with library_engine.connect() as conn:
        row = conn.execute(select(library_entries_table).where(library_entries_table.c.entry_id == entry.entry_id)).one()
    assert row.curated_by_identity_id == winners[0] and row.accepted_at is not None


def test_revocation_locked_first_prevents_later_curator_decision(library_engine: Engine, tmp_path: Path) -> None:
    with library_engine.begin() as conn:
        for identity_id in ("alice", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
    _grant_role(library_engine, "alice", "user")
    role_id = _grant_role(library_engine, "carol", "curator")
    authority = RepositoryLibraryAuthority(library_engine, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    entry = authority.publish(
        session_id=str(uuid4()),
        state=_state(),
        title="revoke race",
        published_by="alice",
        compartment_id="alpha",
        record=lambda event: None,
    )
    reached_grant_read = Event()
    release_grant_read = Event()
    worker_ident: int | None = None

    def pause_before_grant_read(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if get_ident() == worker_ident and statement.lstrip().upper().startswith("SELECT") and "identity_roles" in statement:
            reached_grant_read.set()
            assert release_grant_read.wait(timeout=15)

    def accept() -> object:
        nonlocal worker_ident
        worker_ident = get_ident()
        return authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)

    event.listen(library_engine, "before_cursor_execute", pause_before_grant_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(accept)
            assert reached_grant_read.wait(timeout=10)
            with library_engine.begin() as revoke_conn:
                revoke_conn.execute(
                    update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=datetime.now(UTC))
                )
                release_grant_read.set()
                # The grant read must wait on the revoker's row lock. An
                # unlocked MVCC read admits stale access and finishes here.
                with pytest.raises(FutureTimeoutError):
                    future.result(timeout=1)
            with pytest.raises(CuratorAuthorityRequired):
                future.result(timeout=10)
    finally:
        release_grant_read.set()
        event.remove(library_engine, "before_cursor_execute", pause_before_grant_read)
    assert authority.read(entry_id=entry.entry_id).state == "pending"


def test_recall_locked_first_refuses_later_fork_authorization(library_engine: Engine, tmp_path: Path) -> None:
    with library_engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
    _grant_role(library_engine, "alice", "user")
    _grant_role(library_engine, "bob", "user")
    _grant_role(library_engine, "carol", "curator")
    authority = RepositoryLibraryAuthority(library_engine, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    entry = authority.publish(
        session_id=str(uuid4()),
        state=_state(),
        title="recall race",
        published_by="alice",
        compartment_id="alpha",
        record=lambda event: None,
    )
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with library_engine.begin() as recall_conn:
            recall_conn.execute(
                update(library_entries_table)
                .where(library_entries_table.c.entry_id == entry.entry_id)
                .values(recalled_at=datetime.now(UTC))
            )
            future = pool.submit(authority.authorize_fork_source, entry_id=entry.entry_id, forker_identity_id="bob", provider="local")
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=1)
        with pytest.raises(LibraryEntryNotForkable) as refused:
            future.result(timeout=10)
    assert refused.value.current_state == "recalled"
