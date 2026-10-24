"""Concurrent-writer proofs for P4 fenced content publication on real PostgreSQL.

``test_content_publication_postgres.py`` re-runs the P4 storage behaviours one
writer at a time, which proves dialect parity but not contention. Here two
real writers on separate threads and connections race the same session's
``composer_meta`` publication through the production write path
(``mutate`` -> ``append_state``). Each proof has a mutation control that
removes the serialisation or fence it relies on and must go red.

Multi-replica relevance: Azure Container Apps runs 2-4 replicas against one
PostgreSQL store (``workload.bicep:70,73``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_freeze, deep_thaw
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.content_publication import CONTENT_PUBLICATION_KEY, read_content_publication, storage_content_hash
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_states_table, session_operation_fences_table, web_instances_table
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord, SessionCompositionStateCreation
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

_BARRIER_TIMEOUT_SECONDS = 20


@pytest.fixture(scope="module")
def postgres_engine(external_deployment_postgres_url: str) -> Engine:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    try:
        yield engine
    finally:
        engine.dispose()


def _register_instance(engine: Engine, *, instance_id: str) -> None:
    with engine.begin() as conn:
        database_now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            insert(web_instances_table).values(
                instance_id=instance_id,
                deployment_target="testcontainer",
                deployment_generation="generation-1",
                session_epoch=37,
                landscape_epoch=29,
                coordination_protocol=1,
                image_digest="sha256:test",
                revision_label="test-revision",
                state="active",
                started_at=database_now,
                last_heartbeat_at=database_now,
                lease_expires_at=database_now + timedelta(minutes=5),
            )
        )


def _session(repository: PostgresSessionOperationRepository, *, owner: str) -> UUID:
    created = repository.create_session_with_initial_fence(
        user_id="alice",
        title="P4 contention",
        auth_provider_type="local",
        owner_instance_id=owner,
        lease_seconds=30,
    )
    return created.id


def _compose(repository: PostgresSessionOperationRepository, session_id: UUID, *, owner: str) -> SessionOperationContext:
    return repository.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=owner,
        lease_seconds=30,
    )


def _creation(description: str) -> SessionCompositionStateCreation:
    raw = CompositionState(nodes=(), edges=(), outputs=(), version=1, metadata=PipelineMetadata(description=description)).to_dict()
    data = CompositionStateData(
        sources=raw["sources"],
        nodes=raw["nodes"],
        edges=raw["edges"],
        outputs=raw["outputs"],
        metadata_=raw["metadata"],
        composer_meta={"opaque": {"writer": description}},
    )
    return SessionCompositionStateCreation(id=uuid4(), data=data, provenance="session_seed", created_at=datetime.now(UTC))


def _append(
    repository: PostgresSessionOperationRepository, context: SessionOperationContext, creation: SessionCompositionStateCreation
) -> CompositionStateRecord:
    return repository.mutate(context, lambda transaction: transaction.composition_states.append_state(creation))


def _race(barrier: Barrier, *calls: Callable[[], Any]) -> list[Any]:
    """Run each call on its own thread, released together; return value or exception."""

    def run(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        try:
            return call()
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def _stored_rows(engine: Engine, session_id: UUID) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                composition_states_table.c.id,
                composition_states_table.c.version,
                composition_states_table.c.composer_meta,
                composition_states_table.c.creator_operation_id,
            )
            .where(composition_states_table.c.session_id == str(session_id))
            .order_by(composition_states_table.c.version)
        ).all()
    return [dict(row._mapping) for row in rows]


def _stored_publication(row: dict[str, Any]) -> dict[str, Any]:
    # The column is an envelope; locate the publication object wherever the
    # envelope nests it, then compare its binding to the row's own identity.
    def find(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if CONTENT_PUBLICATION_KEY in value and isinstance(value[CONTENT_PUBLICATION_KEY], dict):
                return value[CONTENT_PUBLICATION_KEY]
            for nested in value.values():
                found = find(nested)
                if found is not None:
                    return found
        if isinstance(value, str) and CONTENT_PUBLICATION_KEY in value:
            return find(json.loads(value))
        return None

    publication = find(row["composer_meta"])
    assert publication is not None, row
    return publication


def _assert_publications_bind_their_own_rows(records: list[CompositionStateRecord], rows: list[dict[str, Any]]) -> None:
    for record in records:
        # The production reader refuses any publication not bound to this
        # exact (session, state, version, content) identity.
        read_content_publication(record)
        assert storage_content_hash(record)
    for row in rows:
        publication = _stored_publication(row)
        assert str(publication["state_id"]) == str(row["id"]), row
        assert int(publication["state_version"]) == int(row["version"]), row


# --------------------------------------------------------------------------- #
# Proof 1: same fence, concurrent appends serialise; nothing is torn.
# --------------------------------------------------------------------------- #


def test_concurrent_appends_under_one_fence_both_publish_distinct_versions(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"replica-{uuid4()}"
    session_id = _session(repository, owner=owner)
    context = _compose(repository, session_id, owner=owner)

    outcomes = _race(
        Barrier(2),
        lambda: _append(repository, context, _creation("writer-a")),
        lambda: _append(repository, context, _creation("writer-b")),
    )

    assert all(isinstance(outcome, CompositionStateRecord) for outcome in outcomes), outcomes
    rows = _stored_rows(postgres_engine, session_id)
    assert [row["version"] for row in rows] == [1, 2]
    assert {row["id"] for row in rows} == {str(outcome.id) for outcome in outcomes}
    _assert_publications_bind_their_own_rows(outcomes, rows)


def _ungranted_advisory_waiters(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted").scalar_one())


def _a_holds_while_b_arrives(
    engine: Engine, repository: PostgresSessionOperationRepository, context: SessionOperationContext
) -> tuple[bool, list[CompositionStateRecord]]:
    """Writer A holds its mutate transaction open until PostgreSQL reports a new
    ungranted advisory lock (writer B waiting), or a deadline passes; then both
    publish. Returns whether B was observed waiting on the advisory lock."""
    baseline_waiters = _ungranted_advisory_waiters(engine)
    a_inside = Event()
    observed_waiter = Event()

    def writer_a() -> CompositionStateRecord:
        creation = _creation("writer-a")

        def append_after_b_arrives(transaction: Any) -> CompositionStateRecord:
            a_inside.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if _ungranted_advisory_waiters(engine) > baseline_waiters:
                    observed_waiter.set()
                    break
                time.sleep(0.05)
            return transaction.composition_states.append_state(creation)

        return repository.mutate(context, append_after_b_arrives)

    def writer_b() -> CompositionStateRecord:
        assert a_inside.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        return _append(repository, context, _creation("writer-b"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer_a), pool.submit(writer_b)]
        outcomes = [future.result(timeout=_BARRIER_TIMEOUT_SECONDS * 2) for future in futures]
    return observed_waiter.is_set(), outcomes


def test_second_writer_blocks_on_the_session_lock_while_the_first_publishes(postgres_engine: Engine) -> None:
    """Non-vacuous concurrency: writer A holds its locked transaction open until
    PostgreSQL itself reports writer B waiting on the session advisory lock,
    and only then publishes. A sequential interleaving cannot satisfy the wait
    observation. Both must still publish distinct, whole versions."""
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"replica-{uuid4()}"
    session_id = _session(repository, owner=owner)
    observed, outcomes = _a_holds_while_b_arrives(postgres_engine, repository, _compose(repository, session_id, owner=owner))

    assert observed, "writer B never waited on the session advisory lock: the race was not concurrent"
    rows = _stored_rows(postgres_engine, session_id)
    assert [row["version"] for row in rows] == [1, 2]
    assert rows[0]["id"] == str(outcomes[0].id)  # A held the lock, so A allocated version 1
    _assert_publications_bind_their_own_rows(outcomes, rows)


def test_control_without_advisory_lock_b_never_waits_on_it_but_row_locks_still_serialise(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control for the wait proof: remove only the advisory lock. B can
    no longer be observed waiting on it, so the proof's wait assertion goes red.
    The session-row FOR UPDATE and fence-row CAS still serialise the two
    writers (measured defence in depth), so versions remain distinct."""
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"replica-{uuid4()}"
    session_id = _session(repository, owner=owner)
    context = _compose(repository, session_id, owner=owner)

    @contextmanager
    def unlocked_transaction(_session_id: str):
        with postgres_engine.begin() as conn:
            yield conn

    monkeypatch.setattr(repository, "_locked_transaction", unlocked_transaction)
    observed, outcomes = _a_holds_while_b_arrives(postgres_engine, repository, context)

    assert not observed
    rows = _stored_rows(postgres_engine, session_id)
    assert [row["version"] for row in rows] == [1, 2]
    _assert_publications_bind_their_own_rows(outcomes, rows)


def test_control_without_session_serialisation_two_writers_collide_on_one_version(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control for proof 1: remove the advisory lock, the session-row
    lock and the fence-row CAS lock, then hold both writers between their
    version read and their insert. The unique version constraint is all that
    remains, so exactly one writer must fail."""
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"replica-{uuid4()}"
    session_id = _session(repository, owner=owner)
    context = _compose(repository, session_id, owner=owner)

    @contextmanager
    def unlocked_transaction(_session_id: str):
        with postgres_engine.begin() as conn:
            yield conn

    monkeypatch.setattr(repository, "_locked_transaction", unlocked_transaction)
    monkeypatch.setattr(repository, "_lock_fence_and_read_database_time", lambda conn, _context: repository._database_now(conn))
    monkeypatch.setattr(repository, "_compare_and_swap_on_connection", lambda conn, _context, *, database_now: None)

    between_read_and_insert = Barrier(2)

    def hold_before_insert(*listener_args: Any) -> None:
        statement = listener_args[2]  # (conn, cursor, statement, parameters, context, executemany)
        if statement.lstrip().upper().startswith("INSERT INTO COMPOSITION_STATES"):
            between_read_and_insert.wait(timeout=_BARRIER_TIMEOUT_SECONDS)

    event.listen(postgres_engine, "before_cursor_execute", hold_before_insert)
    try:
        outcomes = _race(
            Barrier(2),
            lambda: _append(repository, context, _creation("writer-a")),
            lambda: _append(repository, context, _creation("writer-b")),
        )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", hold_before_insert)

    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(failures) == 1, outcomes
    assert isinstance(failures[0], IntegrityError), failures
    assert "uq_composition_state_version" in str(failures[0])
    assert [row["version"] for row in _stored_rows(postgres_engine, session_id)] == [1]


# --------------------------------------------------------------------------- #
# Proof 2: a taken-over writer races its successor; exactly one wins.
# --------------------------------------------------------------------------- #


def _taken_over_pair(
    engine: Engine, repository: PostgresSessionOperationRepository
) -> tuple[UUID, SessionOperationContext, SessionOperationContext]:
    stale_owner = f"stalled-replica-{uuid4()}"
    successor = f"successor-replica-{uuid4()}"
    _register_instance(engine, instance_id=stale_owner)
    session_id = _session(repository, owner=stale_owner)
    stale = _compose(repository, session_id, owner=stale_owner)
    with engine.begin() as conn:
        database_now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == stale_owner)
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )
    current = _compose(repository, session_id, owner=successor)
    assert current.fence.operation_epoch == stale.fence.operation_epoch + 1
    return session_id, stale, current


def test_stale_writer_racing_its_successor_is_refused_and_publication_is_whole(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session_id, stale, current = _taken_over_pair(postgres_engine, repository)

    stale_outcome, current_outcome = _race(
        Barrier(2),
        lambda: _append(repository, stale, _creation("stalled-replica")),
        lambda: _append(repository, current, _creation("successor")),
    )

    assert isinstance(stale_outcome, SessionOperationFenceLost), stale_outcome
    assert isinstance(current_outcome, CompositionStateRecord), current_outcome
    rows = _stored_rows(postgres_engine, session_id)
    assert [row["version"] for row in rows] == [1]
    assert rows[0]["creator_operation_id"] == current.fence.operation_id
    _assert_publications_bind_their_own_rows([current_outcome], rows)


def test_control_without_fence_predicates_the_stale_writer_lands(postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation control for proof 2: reduce the fence CAS to a session match,
    dropping operation id, token, epoch, kind, release and lease expiry. The
    stalled replica's write must then land beside its successor's."""
    repository = PostgresSessionOperationRepository(postgres_engine)
    session_id, stale, current = _taken_over_pair(postgres_engine, repository)
    monkeypatch.setattr(
        repository,
        "_exact_active_predicates",
        lambda context, _database_now: (session_operation_fences_table.c.session_id == context.fence.session_id,),
    )

    outcomes = _race(
        Barrier(2),
        lambda: _append(repository, stale, _creation("stalled-replica")),
        lambda: _append(repository, current, _creation("successor")),
    )

    assert all(isinstance(outcome, CompositionStateRecord) for outcome in outcomes), outcomes
    creators = {row["creator_operation_id"] for row in _stored_rows(postgres_engine, session_id)}
    assert stale.fence.operation_id in creators


def test_stored_publication_tamper_is_refused_by_the_production_reader(postgres_engine: Engine) -> None:
    """Negative control for the torn-metadata check itself: a publication whose
    binding disagrees with its row must be refused, so the proofs' checks
    cannot pass on rows they fail to decode."""
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"replica-{uuid4()}"
    session_id = _session(repository, owner=owner)
    record = _append(repository, _compose(repository, session_id, owner=owner), _creation("writer"))
    meta = deep_thaw(record.composer_meta)
    meta[CONTENT_PUBLICATION_KEY] = {**meta[CONTENT_PUBLICATION_KEY], "state_version": record.version + 1}
    with pytest.raises(AuditIntegrityError):
        read_content_publication(replace(record, composer_meta=deep_freeze(meta)))
    row = _stored_rows(postgres_engine, session_id)[0]
    assert int(_stored_publication(row)["state_version"]) == row["version"]
