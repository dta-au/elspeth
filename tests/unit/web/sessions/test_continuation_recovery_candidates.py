"""Recovery discovery against real admissions, starts and retained SQL rows."""

from dataclasses import FrozenInstanceError
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, update

from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation_recovery_candidates import ContinuationRecoveryCandidate, list_recoverable_segments
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table, web_instances_table
from tests.unit.web.coordination.test_composer_continuation_claim_start import queued, start_segment
from tests.unit.web.coordination.test_composer_operation_authority import _admission_rows, admit, failed
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store

operation_store = _operation_store


def _started(store):
    authority, record = queued(store)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(store[1], claim)
    return authority, record, claim, context


def test_released_exact_fence_returns_original_identity_without_writes(operation_store):
    engine, repository, _, _, sid = operation_store
    _, record, claim, context = _started(operation_store)
    assert list_recoverable_segments(engine, limit=1) == (), "live physical owner must exclude discovery"
    repository.release(context)
    with engine.begin() as conn:
        # Isolate release from expiry on SQLite as well as PostgreSQL.
        conn.execute(update(fences).values(lease_expires_at=database_now(conn) + timedelta(days=1)))
    before = _admission_rows(engine)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert type(candidate) is ContinuationRecoveryCandidate
    assert candidate.session_id == sid
    assert candidate.operation_id == record.operation_id
    assert candidate.expected_generation == claim.ownership_generation == 1
    assert candidate.segment_sequence == claim.segment_sequence == 1
    assert candidate.actor_user_id == "alice"
    assert candidate.auth_provider_type == "local"
    assert candidate.claim_attempt == claim.attempt
    assert candidate.session_operation_id == context.fence.operation_id
    assert candidate.session_operation_epoch == context.fence.operation_epoch
    assert candidate.session_operation_lease_token == context.fence.lease_token
    assert candidate.owner_instance_id == "test-owner"
    assert candidate.checkpoint_id == record.checkpoint_id
    assert candidate.cancel_requested_at is candidate.session_archived_at is None
    with pytest.raises(FrozenInstanceError):
        candidate.actor_user_id = "replacement"
    assert _admission_rows(engine) == before


def test_expired_physical_requires_expired_postgres_membership(operation_store):
    engine = operation_store[0]
    _started(operation_store)
    assert list_recoverable_segments(engine, limit=1) == ()
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                update(web_instances_table)
                .where(web_instances_table.c.instance_id == "test-owner")
                .values(lease_expires_at=database_now(conn) - timedelta(days=1))
            )
        before = _admission_rows(engine)
        assert list_recoverable_segments(engine, limit=1) == (), "membership expiry alone cannot replace a live physical fence"
        assert _admission_rows(engine) == before
        with engine.begin() as conn:
            conn.execute(
                update(web_instances_table)
                .where(web_instances_table.c.instance_id == "test-owner")
                .values(lease_expires_at=database_now(conn) + timedelta(days=1))
            )
    with engine.begin() as conn:
        conn.execute(update(fences).values(lease_expires_at=database_now(conn) - timedelta(days=1)))
    before = _admission_rows(engine)
    if engine.dialect.name == "postgresql":
        assert list_recoverable_segments(engine, limit=1) == (), "physical expiry alone cannot prove a live member dead"
        assert _admission_rows(engine) == before
        with engine.begin() as conn:
            changed = conn.execute(
                update(web_instances_table)
                .where(web_instances_table.c.instance_id == "test-owner")
                .values(lease_expires_at=database_now(conn) - timedelta(days=1))
            )
            assert changed.rowcount == 1
    before = _admission_rows(engine)
    assert len(list_recoverable_segments(engine, limit=1)) == 1
    assert _admission_rows(engine) == before
    if engine.dialect.name == "postgresql":
        # A reused identity with a new live membership does not prove death.
        with engine.begin() as conn:
            now = database_now(conn)
            conn.execute(
                update(web_instances_table)
                .where(web_instances_table.c.instance_id == "test-owner")
                .values(started_at=now, last_heartbeat_at=now, lease_expires_at=now + timedelta(days=1))
            )
        before = _admission_rows(engine)
        assert list_recoverable_segments(engine, limit=1) == ()
        assert _admission_rows(engine) == before


@pytest.mark.parametrize("released", [False, True])
def test_missing_postgres_member_never_proves_death_but_release_does(operation_store, released):
    engine, repository, _, _, _ = operation_store
    _, _, _, context = _started(operation_store)
    if released:
        repository.release(context)
    with engine.begin() as conn:
        conn.execute(update(fences).values(lease_expires_at=database_now(conn) - timedelta(days=1)))
        conn.execute(delete(web_instances_table).where(web_instances_table.c.instance_id == "test-owner"))
    before = _admission_rows(engine)
    candidates = list_recoverable_segments(engine, limit=1)
    assert len(candidates) == (1 if released or engine.dialect.name == "sqlite" else 0)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize(
    "changed",
    [
        "parent_sequence",
        "parent_generation",
        "parent_attempt",
        "parent_checkpoint",
        "segment_state",
        "attempt_generation",
        "attempt_physical_id",
        "attempt_epoch",
        "attempt_token",
        "attempt_owner",
        "fence_id",
        "fence_epoch",
        "fence_token",
        "fence_owner",
        "fence_kind",
        "revoked_attempt",
        "missing_attempt",
    ],
)
def test_discovery_requires_exact_current_segment_attempt_and_fence(operation_store, changed):
    engine, repository, _, _, _ = operation_store
    _, _, _, context = _started(operation_store)
    repository.release(context)
    assert len(list_recoverable_segments(engine, limit=1)) == 1, "exact original identity is the positive control"
    with engine.begin() as conn:
        if changed == "parent_sequence":
            conn.execute(update(jobs).values(current_segment_sequence=2))
        elif changed == "parent_generation":
            conn.execute(update(jobs).values(ownership_generation=2))
        elif changed == "parent_attempt":
            conn.execute(update(jobs).values(attempt=jobs.c.attempt + 1))
        elif changed == "parent_checkpoint":
            conn.execute(update(jobs).values(checkpoint_id=str(uuid4())))
        elif changed == "segment_state":
            conn.execute(update(segments).values(state="settled"))
        elif changed == "attempt_generation":
            conn.execute(update(attempts).values(generation=2))
        elif changed == "attempt_physical_id":
            conn.execute(update(attempts).values(session_operation_id=str(uuid4())))
        elif changed == "attempt_epoch":
            conn.execute(update(attempts).values(session_operation_epoch=attempts.c.session_operation_epoch + 1))
        elif changed == "attempt_token":
            conn.execute(update(attempts).values(session_operation_lease_token="foreign-token"))
        elif changed == "attempt_owner":
            conn.execute(update(attempts).values(owner_instance_id="foreign-owner"))
        elif changed == "fence_id":
            conn.execute(update(fences).values(operation_id=str(uuid4())))
        elif changed == "fence_epoch":
            conn.execute(update(fences).values(operation_epoch=fences.c.operation_epoch + 1))
        elif changed == "fence_token":
            conn.execute(update(fences).values(lease_token="foreign-token"))
        elif changed == "fence_owner":
            conn.execute(update(fences).values(owner_instance_id="foreign-owner"))
        elif changed == "fence_kind":
            conn.execute(update(fences).values(operation_kind="archive"))
        elif changed == "revoked_attempt":
            conn.execute(update(attempts).values(revoked_at=database_now(conn), outcome="interrupted"))
        else:
            assert changed == "missing_attempt"
            conn.execute(delete(attempts))
    before = _admission_rows(engine)
    assert list_recoverable_segments(engine, limit=1) == ()
    assert _admission_rows(engine) == before


def test_terminal_parent_excluded_even_if_segment_still_running(operation_store):
    engine, repository, _, _, _ = operation_store
    _, _, _, context = _started(operation_store)
    repository.release(context)
    assert len(list_recoverable_segments(engine, limit=1)) == 1
    # Isolate the parent predicate with the production terminal value producer;
    # deliberately retain the pre-terminal segment and unrevoked attempt.
    with engine.begin() as conn:
        values = _terminal_values(failed(), now=database_now(conn), settled_by="settle_lost")
        conn.execute(update(jobs).values(**values, phase="settled"))
    before = _admission_rows(engine)
    assert list_recoverable_segments(engine, limit=1) == ()
    assert _admission_rows(engine) == before


def test_v1_running_parent_excluded(operation_store):
    engine, repository, authority, _, sid = operation_store
    record, fresh = admit(authority, sid)
    assert fresh
    (claim,) = authority.claim_next(limit=1)
    context = repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    repository.release(context)
    before = _admission_rows(engine)
    assert authority.list_expired_running(limit=1)[0].operation_id == record.operation_id
    assert list_recoverable_segments(engine, limit=1) == ()
    assert _admission_rows(engine) == before


def test_stop_and_archive_are_observed_for_locked_writer_disposition(operation_store):
    engine, repository, _, _, _ = operation_store
    _, _, _, context = _started(operation_store)
    repository.release(context)
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(update(jobs).values(cancel_requested_at=now))
        conn.execute(update(sessions_table).values(archived_at=now))
    before = _admission_rows(engine)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert candidate.cancel_requested_at == candidate.session_archived_at == now
    assert _admission_rows(engine) == before


def test_filter_before_bound_with_stable_tie_order(operation_store, monkeypatch):
    from elspeth.web.coordination import composer_operation_authority as admission_module

    engine, repository, _, _, _ = operation_store
    with engine.connect() as conn:
        tied_at = database_now(conn) - timedelta(days=1)
    oldest_at = tied_at - timedelta(days=1)
    with monkeypatch.context() as admission_clock:
        admission_clock.setattr(admission_module, "database_now", lambda conn: oldest_at)
        authority, oldest = queued(operation_store)
    assert oldest.created_at == oldest_at
    (old_claim,) = authority.claim_segment(limit=1)
    start_segment(repository, old_claim)  # Keep the oldest physical owner live.
    sessions = []
    for _ in range(3):
        sessions.append(
            repository.create_session_with_initial_fence(
                user_id="alice", title="Recovery order", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
            )
        )
    expected = []
    for session in sorted(sessions, key=lambda item: str(item.id), reverse=True):
        with monkeypatch.context() as admission_clock:
            admission_clock.setattr(admission_module, "database_now", lambda conn: tied_at)
            record, fresh = admit(authority, session.id, lifecycle_version=2, continuation_enabled=True)
        assert fresh
        assert record.created_at == tied_at
        (claim,) = authority.claim_segment(limit=1)
        repository.release(start_segment(repository, claim))
        expected.append((str(session.id), record.operation_id, 1))
    assert expected == sorted(expected, reverse=True)
    expected.sort()
    before = _admission_rows(engine)
    for limit in (1, 2, 3, 10_000):
        actual = list_recoverable_segments(engine, limit=limit)
        assert [(str(row.session_id), row.operation_id, row.segment_sequence) for row in actual] == expected[:limit]
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("limit", [True, False, 0, -1, 10_001, 1.0, float("inf"), "1", None])
def test_discovery_rejects_nonfinite_or_non_nominal_bounds_before_sql(operation_store, limit):
    engine = operation_store[0]
    before = _admission_rows(engine)

    def refuse_sql(conn, cursor, statement, parameters, context, executemany):
        raise AssertionError("invalid discovery bound reached SQL")

    event.listen(engine, "before_cursor_execute", refuse_sql)
    try:
        with pytest.raises(ValueError, match="limit must be between 1 and 10000"):
            list_recoverable_segments(engine, limit=limit)
    finally:
        event.remove(engine, "before_cursor_execute", refuse_sql)
    assert _admission_rows(engine) == before
