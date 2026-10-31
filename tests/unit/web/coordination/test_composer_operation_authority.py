"""Durable operation admission, exact start and terminal negative controls."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.composition_state import composition_state_data

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, RepositoryRateLimitAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.composer_operations import (
    ComposerOperationActiveError,
    ComposerOperationAssistantWrite,
    ComposerOperationCancelledDuringTurn,
    ComposerOperationConflictError,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationPreconditionRefused,
    ComposerOperationRecord,
    ComposerOperationRunning,
    composer_operation_request_hash,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.schemas import ChatMessageResponse, MessageWithStateResponse, RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.fixture
def operation_store(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'operations.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    repository = SQLiteLocalSessionOperationAuthority(engine)
    session = repository.create_session_with_initial_fence(
        user_id="alice", title="Durable", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    service = SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.storage"),
        session_operation_authority=repository,
        owner_instance_id="test-owner",
    )
    yield engine, repository, authority, service, session.id
    engine.dispose()


async def _wait_for_thread_event(event: Event) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("SQL thread did not reach the controlled seam")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("segment_state", ["queued", "running"])
def test_known_historical_fence_cannot_fall_back_to_sync_compose(operation_store, segment_state):
    from datetime import timedelta

    from sqlalchemy import insert

    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    engine, _repository, _authority, _service, sid = operation_store
    record, running = start(operation_store)
    fence = running.session_operation_context.fence
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            insert(segments).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                sequence=1,
                state=segment_state,
                generation=1,
                enqueued_at=now,
                next_eligible_at=now,
                checkpoint_id=str(uuid4()),
                next_action_json="{}",
                policy_sha256="a" * 64,
                started_at=now if segment_state == "running" else None,
                cooperative_at=now + timedelta(seconds=10) if segment_state == "running" else None,
                deadline_at=now + timedelta(minutes=1) if segment_state == "running" else None,
                session_operation_id=fence.operation_id,
                session_operation_epoch=fence.operation_epoch,
                session_operation_lease_token=fence.lease_token,
            )
        )
        conn.execute(
            insert(attempts).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                sequence=1,
                generation=1,
                claim_attempt=running.claim.attempt,
                session_operation_id=fence.operation_id,
                session_operation_epoch=fence.operation_epoch,
                session_operation_lease_token=fence.lease_token,
                owner_instance_id="test-owner",
                started_at=now,
                claim_expires_at=now,
            )
        )
        before = _continuation_guard_rows(conn)
        with pytest.raises(ComposerOperationFenceLost):
            require_composer_operation_mutation_on_connection(conn, running.session_operation_context)
        assert _continuation_guard_rows(conn) == before


def _continuation_guard_rows(conn):
    from pathlib import Path

    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    rows = tuple(tuple(conn.execute(select(table)).all()) for table in (jobs, segments, attempts, fences, sessions_table))
    database = conn.engine.url.database
    database_bytes = Path(database).read_bytes() if conn.dialect.name == "sqlite" and database is not None else None
    return rows, database_bytes


def _seed_live_continuation_history(operation_store, *, parent_status="running"):
    from datetime import timedelta

    from sqlalchemy import insert

    from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    engine, repository, _authority, _service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=180
    )
    fence = context.fence
    request = SendMessageRequest(operation_id=str(uuid4()), content="Continuation SQL prerequisite")
    with engine.begin() as conn:
        now = database_now(conn)
        started = now - timedelta(minutes=1)
        conn.execute(
            insert(jobs).values(
                session_id=str(sid),
                operation_id=request.operation_id,
                kind="compose_message",
                status=parent_status,
                lifecycle_version=2,
                phase="executing" if parent_status == "running" else "queued",
                current_segment_sequence=1,
                ownership_generation=1,
                checkpoint_id=str(uuid4()),
                ingress_phase="pending",
                reserved_user_message_id=str(uuid4()),
                request_hash=composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request),
                actor_user_id="alice",
                request_json=request.model_dump_json(),
                attempt=1,
                created_at=started,
                updated_at=now,
                started_at=started,
            )
        )
        conn.execute(
            insert(segments).values(
                session_id=str(sid),
                operation_id=request.operation_id,
                sequence=1,
                state="running",
                generation=1,
                claim_token="segment-claim",
                claim_owner_instance_id="test-owner",
                claim_expires_at=now + timedelta(minutes=3),
                session_operation_id=fence.operation_id,
                session_operation_epoch=fence.operation_epoch,
                session_operation_lease_token=fence.lease_token,
                enqueued_at=started,
                next_eligible_at=started,
                started_at=started,
                cooperative_at=started + timedelta(seconds=10),
                deadline_at=now + timedelta(minutes=3),
                checkpoint_id=str(uuid4()),
                next_action_json="{}",
                policy_sha256="a" * 64,
            )
        )
        conn.execute(
            insert(attempts).values(
                session_id=str(sid),
                operation_id=request.operation_id,
                sequence=1,
                generation=1,
                claim_attempt=1,
                session_operation_id=fence.operation_id,
                session_operation_epoch=fence.operation_epoch,
                session_operation_lease_token=fence.lease_token,
                owner_instance_id="test-owner",
                started_at=started,
                claim_expires_at=now + timedelta(minutes=3),
            )
        )
    return context, request.operation_id


def test_continuation_history_live_sql_identity_allows_mutation_without_changes(operation_store):
    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.continuation import require_continuation_mutation

    engine, _repository, _authority, _service, _sid = operation_store
    context, _operation_id = _seed_live_continuation_history(operation_store)
    with engine.begin() as conn:
        before = _continuation_guard_rows(conn)
        assert require_continuation_mutation(conn, context) is True
        require_composer_operation_mutation_on_connection(conn, context)
        assert _continuation_guard_rows(conn) == before


@pytest.mark.parametrize("changed", ["parent_attempt", "history_claim_attempt"])
def test_continuation_history_current_attempt_must_match_immutable_history(operation_store, changed):
    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    engine, _repository, _authority, _service, sid = operation_store
    context, operation_id = _seed_live_continuation_history(operation_store)
    with engine.begin() as conn:
        if changed == "parent_attempt":
            conn.execute(update(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == operation_id).values(attempt=2))
        else:
            conn.execute(update(attempts).where(attempts.c.session_id == str(sid)).values(claim_attempt=2))
        before = _continuation_guard_rows(conn)
        with pytest.raises(ComposerOperationFenceLost):
            require_composer_operation_mutation_on_connection(conn, context)
        assert _continuation_guard_rows(conn) == before


@pytest.mark.parametrize(
    "changed",
    [
        "parent_sequence",
        "parent_generation",
        "parent_queued",
        "parent_terminal",
        "principal_actor",
        "principal_archive",
        "segment_generation",
        "segment_queued",
        "segment_yielded",
        "segment_operation_id",
        "segment_epoch",
        "segment_token",
        "segment_deadline",
        "history_revoked",
        "history_operation_id",
        "history_token",
        "fence_operation_id",
        "fence_epoch",
        "fence_token",
        "fence_kind",
        "fence_released",
        "fence_expired",
        "fence_missing",
    ],
)
def test_continuation_history_rejects_independent_sql_identity_and_liveness_substitutions(operation_store, changed):
    from datetime import timedelta

    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.composer_operations import composer_operation_result_hash
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    engine, _repository, _authority, _service, sid = operation_store
    context, operation_id = _seed_live_continuation_history(
        operation_store, parent_status="queued" if changed == "parent_queued" else "running"
    )
    with engine.begin() as conn:
        now = database_now(conn)
        if changed == "parent_sequence":
            conn.execute(update(jobs).values(current_segment_sequence=2))
        elif changed == "parent_generation":
            conn.execute(update(jobs).values(ownership_generation=2))
        elif changed == "parent_queued":
            assert conn.execute(select(jobs.c.status)).scalar_one() == "queued"
        elif changed == "parent_terminal":
            error_json = failed().model_dump_json()
            conn.execute(
                update(jobs).values(
                    status="failed",
                    phase="settled",
                    request_json=None,
                    settled_at=now,
                    settled_by="owner_terminal",
                    failure_code="worker_lost",
                    result_schema="composer_operation_error.v1",
                    result_json=error_json,
                    result_sha256=composer_operation_result_hash(error_json),
                )
            )
        elif changed == "principal_actor":
            ensure_test_identity(conn, identity_id="another-actor")
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(user_id="another-actor"))
        elif changed == "principal_archive":
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=now))
        elif changed == "segment_generation":
            conn.execute(update(segments).values(generation=2))
        elif changed == "segment_queued":
            conn.execute(update(segments).values(state="queued", started_at=None, cooperative_at=None, deadline_at=None))
        elif changed == "segment_yielded":
            conn.execute(update(segments).values(state="yielded"))
        elif changed == "segment_operation_id":
            conn.execute(update(segments).values(session_operation_id="other-fence"))
        elif changed == "segment_epoch":
            conn.execute(update(segments).values(session_operation_epoch=context.fence.operation_epoch + 1))
        elif changed == "segment_token":
            conn.execute(update(segments).values(session_operation_lease_token="other-token"))
        elif changed == "segment_deadline":
            conn.execute(update(segments).values(deadline_at=now - timedelta(seconds=1)))
        elif changed == "history_revoked":
            conn.execute(update(attempts).values(revoked_at=now, outcome="interrupted"))
        elif changed == "history_operation_id":
            conn.execute(update(attempts).values(session_operation_id="other-fence"))
        elif changed == "history_token":
            conn.execute(update(attempts).values(session_operation_lease_token="other-token"))
        elif changed == "fence_operation_id":
            conn.execute(update(fences).values(operation_id="other-fence"))
        elif changed == "fence_epoch":
            conn.execute(update(fences).values(operation_epoch=context.fence.operation_epoch + 1))
        elif changed == "fence_token":
            conn.execute(update(fences).values(lease_token="other-token"))
        elif changed == "fence_kind":
            conn.execute(update(fences).values(operation_kind="proposal"))
        elif changed == "fence_released":
            conn.execute(update(fences).values(released_at=now))
        elif changed == "fence_expired":
            conn.execute(update(fences).values(lease_expires_at=now - timedelta(seconds=1)))
        else:
            assert changed == "fence_missing"
            conn.execute(delete(fences))
        before = _continuation_guard_rows(conn)
        with pytest.raises(ComposerOperationFenceLost):
            require_composer_operation_mutation_on_connection(conn, context)
        assert _continuation_guard_rows(conn) == before
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.operation_id)).scalar_one() == operation_id


def test_continuation_history_stop_and_deadline_are_transaction_local_controls(operation_store):
    from datetime import timedelta

    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.continuation import require_continuation_mutation
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    engine, _repository, _authority, _service, _sid = operation_store
    context, operation_id = _seed_live_continuation_history(operation_store)
    with engine.connect() as conn:
        transaction = conn.begin()
        now = database_now(conn)
        conn.execute(update(jobs).values(cancel_requested_at=now))
        before = _continuation_guard_rows(conn)
        with pytest.raises(ComposerOperationCancelledDuringTurn) as stopped:
            require_composer_operation_mutation_on_connection(conn, context)
        assert stopped.value.claim.operation_id == operation_id
        assert stopped.value.claim.attempt == 1
        assert stopped.value.claim.segment_sequence == stopped.value.claim.ownership_generation == 1
        assert _continuation_guard_rows(conn) == before
        assert require_continuation_mutation(conn, context, allow_cancel=True) is True
        conn.execute(update(segments).values(deadline_at=now - timedelta(seconds=1)))
        before = _continuation_guard_rows(conn)
        with pytest.raises(ComposerOperationFenceLost):
            require_continuation_mutation(conn, context, allow_cancel=True)
        assert require_continuation_mutation(conn, context, allow_cancel=True, allow_expired_segment=True) is True
        assert _continuation_guard_rows(conn) == before
        transaction.rollback()
    with engine.begin() as conn:
        assert require_continuation_mutation(conn, context) is True


def test_continuation_history_missing_segment_is_refused_by_actual_foreign_key(operation_store):
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    engine, _repository, _authority, _service, _sid = operation_store
    _seed_live_continuation_history(operation_store)
    with engine.begin() as conn:
        before = _continuation_guard_rows(conn)
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(update(attempts).values(sequence=2))
        assert _continuation_guard_rows(conn) == before


def test_continuation_history_unknown_sync_and_noncompose_preserve_prior_behavior(operation_store):
    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.continuation import require_continuation_mutation

    engine, repository, _authority, _service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    with engine.begin() as conn:
        before = _continuation_guard_rows(conn)
        assert require_continuation_mutation(conn, context) is False
        require_composer_operation_mutation_on_connection(conn, context)
        assert _continuation_guard_rows(conn) == before
    repository.release(context)
    noncompose = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.PROPOSAL, owner_instance_id="test-owner", lease_seconds=30
    )
    with engine.begin() as conn:
        before = _continuation_guard_rows(conn)
        assert require_continuation_mutation(conn, noncompose) is False
        require_composer_operation_mutation_on_connection(conn, noncompose)
        assert _continuation_guard_rows(conn) == before


def test_continuation_history_owed_p4_scope_predicate_has_exact_nominal_positive_control(operation_store):
    from elspeth.web.sessions.continuation import (
        _OWED_P4_SCOPE_KEY,
        _OWED_P4_SEAL,
        _owed_continuation_p4_active,
        _OwedP4Scope,
    )

    engine, _repository, _authority, _service, _sid = operation_store
    context, _operation_id = _seed_live_continuation_history(operation_store)
    with engine.begin() as conn:
        assert _owed_continuation_p4_active(conn, context) is False
        before = _continuation_guard_rows(conn)
        conn.info[_OWED_P4_SCOPE_KEY] = _OwedP4Scope(conn, context, _OWED_P4_SEAL)
        try:
            assert _owed_continuation_p4_active(conn, context) is True
            assert _continuation_guard_rows(conn) == before
        finally:
            del conn.info[_OWED_P4_SCOPE_KEY]


@pytest.mark.parametrize("changed", ["impostor", "seal", "connection", "context", "inactive"])
def test_continuation_history_owed_p4_scope_cannot_escape_exact_active_transaction(operation_store, changed):
    from dataclasses import replace

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import (
        _OWED_P4_SCOPE_KEY,
        _OWED_P4_SEAL,
        _owed_continuation_p4_active,
        _OwedP4Scope,
    )

    engine, _repository, _authority, _service, _sid = operation_store
    context, _operation_id = _seed_live_continuation_history(operation_store)
    with engine.connect() as conn, engine.connect() as other:
        conn.begin()
        other.begin()
        scope = _OwedP4Scope(conn, context, _OWED_P4_SEAL)
        if changed == "impostor":
            selected = object()
        elif changed == "seal":
            selected = replace(scope, seal=object())
        elif changed == "connection":
            selected = replace(scope, connection=other)
        elif changed == "context":
            selected = replace(scope, context=replace(context, operation_kind=SessionOperationKind.PROPOSAL))
        else:
            selected = scope
            assert changed == "inactive"
            conn.rollback()
        conn.info[_OWED_P4_SCOPE_KEY] = selected
        try:
            with pytest.raises(AuditIntegrityError, match="exact current SQL owner transaction"):
                _owed_continuation_p4_active(conn, context)
        finally:
            del conn.info[_OWED_P4_SCOPE_KEY]


@pytest.mark.parametrize("end", ["commit", "rollback"])
@pytest.mark.parametrize("stop", ["cancel", "expired"])
def test_continuation_history_owed_scope_refuses_root_transaction_replacement(operation_store, end, stop):
    from datetime import timedelta

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import (
        _OWED_P4_SCOPE_KEY,
        _OWED_P4_SEAL,
        _owed_continuation_p4_active,
        _OwedP4Scope,
        require_continuation_mutation,
    )
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    engine, _repository, _authority, _service, _sid = operation_store
    context, _operation_id = _seed_live_continuation_history(operation_store)
    with engine.connect() as conn:
        first = conn.begin()
        scope = _OwedP4Scope(conn, context, _OWED_P4_SEAL)
        conn.info[_OWED_P4_SCOPE_KEY] = scope
        assert _owed_continuation_p4_active(conn, context) is True
        assert require_continuation_mutation(conn, context) is True
        if end == "commit":
            first.commit()
        else:
            first.rollback()
        with pytest.raises(AuditIntegrityError):
            _owed_continuation_p4_active(conn, context)
        second = conn.begin()
        assert second is not first
        now = database_now(conn)
        if stop == "cancel":
            conn.execute(update(jobs).values(cancel_requested_at=now))
            expected = ComposerOperationCancelledDuringTurn
        else:
            conn.execute(update(segments).values(deadline_at=now - timedelta(seconds=1)))
            expected = ComposerOperationFenceLost
        del conn.info[_OWED_P4_SCOPE_KEY]
        before = _continuation_guard_rows(conn)
        with pytest.raises(expected):
            require_continuation_mutation(conn, context)
        assert _continuation_guard_rows(conn) == before
        conn.info[_OWED_P4_SCOPE_KEY] = scope
        try:
            with pytest.raises(AuditIntegrityError, match="exact current SQL owner transaction"):
                require_continuation_mutation(conn, context)
            assert _continuation_guard_rows(conn) == before
        finally:
            del conn.info[_OWED_P4_SCOPE_KEY]
            second.rollback()


def test_continuation_history_owed_scope_construction_requires_active_root_transaction(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import _OWED_P4_SEAL, _OwedP4Scope

    engine, _repository, _authority, _service, _sid = operation_store
    context, _operation_id = _seed_live_continuation_history(operation_store)
    with engine.connect() as conn:
        assert conn.get_transaction() is None
        with pytest.raises(AuditIntegrityError, match="active SQL owner transaction"):
            _OwedP4Scope(conn, context, _OWED_P4_SEAL)


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_continuation_history_owed_scope_preserves_savepoints_within_same_root(operation_store, end):
    from elspeth.web.sessions.continuation import (
        _OWED_P4_SCOPE_KEY,
        _OWED_P4_SEAL,
        _owed_continuation_p4_active,
        _OwedP4Scope,
        require_continuation_mutation,
    )

    engine, _repository, _authority, _service, _sid = operation_store
    context, _operation_id = _seed_live_continuation_history(operation_store)
    with engine.begin() as conn:
        root = conn.get_transaction()
        conn.info[_OWED_P4_SCOPE_KEY] = _OwedP4Scope(conn, context, _OWED_P4_SEAL)
        try:
            nested = conn.begin_nested()
            assert conn.get_transaction() is root
            before = _continuation_guard_rows(conn)
            assert _owed_continuation_p4_active(conn, context) is True
            assert require_continuation_mutation(conn, context) is True
            assert _continuation_guard_rows(conn) == before
            if end == "commit":
                nested.commit()
            else:
                nested.rollback()
            assert conn.get_transaction() is root
            assert _owed_continuation_p4_active(conn, context) is True
            before = _continuation_guard_rows(conn)
            assert require_continuation_mutation(conn, context) is True
            assert _continuation_guard_rows(conn) == before
        finally:
            del conn.info[_OWED_P4_SCOPE_KEY]


def admit(
    authority,
    sid,
    request=None,
    *,
    actor="alice",
    provider="local",
    maximum=64,
    deadline_seconds=180.0,
    quota=None,
    lifecycle_version=1,
    continuation_enabled=False,
    seed=None,
):
    request = request or SendMessageRequest(operation_id=str(uuid4()), content="Build")
    kind = "compose_message" if isinstance(request, SendMessageRequest) else "compose_recompose"
    return authority.admit(
        quota=quota
        if quota is not None
        else ComposerQuotaAdmission(
            RepositoryRateLimitAuthority(authority._engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000
        ),
        session_id=sid,
        operation_id=request.operation_id,
        kind=kind,
        request_hash=(composer_operation_request_hash if lifecycle_version == 1 else composer_operation_v2_request_hash)(
            session_id=sid, kind=kind, request=request
        ),
        actor_user_id=actor,
        request_id="correlation",
        base_state_id=request.state_id,
        request_json=request.model_dump_json(),
        deadline_seconds=deadline_seconds,
        max_nonterminal=maximum,
        auth_provider_type=provider,
        lifecycle_version=lifecycle_version,
        continuation_enabled=continuation_enabled,
        seed=seed,
    )


def _admission_v2_authority(operation_store, *, policy=None):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    return ComposerAsyncOperationAuthority(
        operation_store[0],
        owner_instance_id="test-owner",
        claim_lease_seconds=30,
        continuation_policy=policy if policy is not None else minimal_continuation_policy(),
    )


def _admission_rows(engine):
    from elspeth.web.sessions.models import metadata

    with engine.connect() as conn:
        return {
            table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr)
            for table in sorted(metadata.tables.values(), key=lambda table: table.name)
        }


def _admission_state_data():
    from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
    from elspeth.web.sessions.protocol import CompositionStateData

    node = NodeSpec(
        id="owned-transform",
        node_type="transform",
        plugin="field_mapper",
        input="source",
        on_success="output",
        on_error="discard",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    raw = CompositionState(nodes=(node,), edges=(), outputs=(), version=1, metadata=PipelineMetadata()).to_dict()
    return CompositionStateData(
        sources=raw["sources"], nodes=raw["nodes"], edges=raw["edges"], outputs=raw["outputs"], metadata_=raw["metadata"]
    )


@pytest.mark.parametrize("lifecycle_version", (1, 2))
@pytest.mark.parametrize("case", ("valid", "hash_mismatch", "operation_id_mismatch", "state_id_mismatch", "selected_hash_raises"))
def test_admission_hash_branch_and_early_binding_refusals_preserve_rows(operation_store, monkeypatch, lifecycle_version, case):
    from elspeth.web.coordination import composer_operation_authority as module

    engine, _repository, legacy_authority, _service, sid = operation_store
    authority = legacy_authority if lifecycle_version == 1 else _admission_v2_authority(operation_store)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Admission hash branch")
    if lifecycle_version == 1:
        request_hash = composer_operation_request_hash(session_id=sid, kind="compose_message", request=request)
    else:
        request_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request)
    original_v1 = module.composer_operation_request_hash
    original_v2 = module.composer_operation_v2_request_hash
    calls = []
    selected_error = None

    def observed_v1(*, session_id, kind, request):
        calls.append(("v1", session_id, kind, request))
        if selected_error is not None:
            raise selected_error
        return original_v1(session_id=session_id, kind=kind, request=request)

    def observed_v2(*, session_id, kind, request):
        calls.append(("v2", session_id, kind, request))
        if selected_error is not None:
            raise selected_error
        return original_v2(session_id=session_id, kind=kind, request=request)

    # Intercept the authority's bindings only. The real v2 implementation is
    # allowed to invoke its defining module's v1 helper internally.
    monkeypatch.setattr(module, "composer_operation_request_hash", observed_v1)
    monkeypatch.setattr(module, "composer_operation_v2_request_hash", observed_v2)
    kwargs = {
        "quota": ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
        "session_id": sid,
        "operation_id": request.operation_id,
        "kind": "compose_message",
        "request_hash": request_hash,
        "actor_user_id": "alice",
        "request_id": "correlation",
        "base_state_id": request.state_id,
        "request_json": request.model_dump_json(),
        "deadline_seconds": 180.0,
        "max_nonterminal": 64,
        "auth_provider_type": "local",
        "lifecycle_version": lifecycle_version,
        "continuation_enabled": lifecycle_version == 2,
    }
    # Every refusal starts with a real fresh positive under the same spies.
    record, fresh = authority.admit(**kwargs)
    expected_calls = [("v1" if lifecycle_version == 1 else "v2", sid, "compose_message", request)]
    assert calls == expected_calls
    assert fresh and record.lifecycle_version == lifecycle_version
    assert record.operation_id == request.operation_id and record.request_hash == request_hash
    if case == "valid":
        return

    before = _admission_rows(engine)
    calls.clear()
    if case == "operation_id_mismatch":
        kwargs["operation_id"] = str(uuid4())
    elif case == "state_id_mismatch":
        kwargs["base_state_id"] = uuid4()
    elif case == "hash_mismatch":
        kwargs["request_hash"] = ("0" if request_hash[0] != "0" else "1") + request_hash[1:]
    else:
        assert case == "selected_hash_raises"
        selected_error = RuntimeError("selected admission hash failed")

    if selected_error is not None:
        with pytest.raises(RuntimeError) as refused:
            authority.admit(**kwargs)
        assert refused.value is selected_error
    else:
        with pytest.raises(ValueError, match=r"^composer request does not match its immutable admission binding$"):
            authority.admit(**kwargs)
    assert calls == ([] if case in {"operation_id_mismatch", "state_id_mismatch"} else expected_calls)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("mode", ["auto_commit", "explicit_approve"])
@pytest.mark.parametrize("supplied", [False, True])
def test_v2_admission_fresh_bundle_uses_actual_sql_mode_and_reserved_ingress(operation_store, mode, supplied):
    from elspeth.web.composer.continuation import reserved_user_message_id
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.models import chat_messages_table, rate_limit_events_table
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    engine, _repository, _authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(trust_mode=mode))
    before = _admission_rows(engine)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Atomic continuation")
    record, fresh = admit(
        authority,
        sid,
        request,
        lifecycle_version=2,
        continuation_enabled=True,
        seed=_admission_seed(sid, request, mode=mode) if supplied else None,
    )
    assert fresh and record.lifecycle_version == 2 and record.deadline_at is None
    assert record.ingress_phase == "pending" and record.user_message_id is None
    assert str(record.reserved_user_message_id) == reserved_user_message_id(sid, request.operation_id)
    assert record.current_segment_sequence == record.ownership_generation == 1
    assert record.status_revision == record.journal_cursor == 0
    with engine.connect() as conn:
        assert len(conn.execute(select(checkpoints)).all()) == len(conn.execute(select(chunks)).all()) == 1
        snapshot = load_snapshot(conn, session_id=str(sid), operation_id=request.operation_id)
        stack = decode_continuation_snapshot(snapshot.payload_json)
        assert stack.mutation_ceiling.initial_trust_mode == mode
        assert stack.logical.original_user_message_id == str(record.reserved_user_message_id)
        assert stack.provider_cost_policy.applicability == "not_applicable"
        (segment,) = conn.execute(select(segments)).all()
        assert segment.state == "queued" and segment.sequence == segment.generation == 1
        assert segment.deadline_at is segment.claim_token is segment.started_at is None
        assert segment.next_action_json == authority._continuation_policy.actions.session_sql.model_dump_json()
        assert len(conn.execute(select(checkpoints)).all()) == len(conn.execute(select(chunks)).all()) == 1
        assert conn.execute(select(attempts)).all() == []
        assert conn.execute(select(chat_messages_table)).all() == []
        assert len(conn.execute(select(rate_limit_events_table)).all()) == 1
    after = _admission_rows(engine)
    for name in ("sessions", "session_operation_fences", "quota_provider_attempts", "token_usage_ledger"):
        assert after[name] == before[name]


def test_v2_admission_fresh_disabled_refuses_exact_body_without_new_rows(operation_store):
    engine, _repository, authority, _service, sid = operation_store
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationPreconditionRefused) as refused:
        admit(authority, sid, lifecycle_version=2)
    assert refused.value.error.http_status == 409
    assert refused.value.error.body == {
        "detail": {
            "error_type": "composer_continuation_disabled",
            "detail": "New continuation requests are disabled.",
        }
    }
    assert _admission_rows(engine) == before


def _admission_seed(sid, request, *, mode="auto_commit", policy=None):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    from elspeth.web.composer.continuation import prepare_composer_continuation_admission

    policy = policy if policy is not None else minimal_continuation_policy()
    kind = "compose_message" if isinstance(request, SendMessageRequest) else "compose_recompose"
    return prepare_composer_continuation_admission(
        session_id=sid,
        operation_id=request.operation_id,
        kind=kind,
        request_json=request.model_dump_json(),
        request_hash=composer_operation_v2_request_hash(session_id=sid, kind=kind, request=request),
        base_state_id=request.state_id,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_trust_mode=mode,
        initial_action_json=policy.actions.session_sql.model_dump_json(),
        original_user_message_id=str(request.expected_user_message_id) if isinstance(request, RecomposeRequest) else None,
    )


def test_v2_admission_disabled_exact_replay_ignores_new_seed_policy_and_current_state(operation_store, monkeypatch):
    from sqlalchemy import event

    from elspeth.web.coordination import composer_operation_authority as module

    engine, _repository, old_authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Replay")
    first, _ = admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
    before = _admission_rows(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("exact replay consulted fresh admission prerequisites")

    def no_mode_read(conn, cursor, statement, parameters, context, executemany):
        assert not (statement.lstrip().upper().startswith("SELECT") and "sessions.trust_mode" in statement)

    monkeypatch.setattr(module, "prepare_composer_continuation_admission", forbidden)
    monkeypatch.setattr(module, "check_composer_request_preconditions_on_connection", forbidden)
    event.listen(engine, "before_cursor_execute", no_mode_read)
    try:
        replay, fresh = admit(old_authority, sid, request, lifecycle_version=2, continuation_enabled=False)
        assert not fresh and replay == first
        changed = request.model_copy(update={"content": "changed", "state_id": uuid4()})
        with pytest.raises(ComposerOperationConflictError):
            admit(old_authority, sid, changed, lifecycle_version=2)
        # D-2: the same bytes retried under the other lifecycle are the same
        # request, matched by the stored row's own lifecycle hash.
        replay_v1, fresh_v1 = admit(old_authority, sid, request)
        assert not fresh_v1 and replay_v1 == first
        with pytest.raises(ComposerOperationConflictError):
            admit(old_authority, sid, changed)
    finally:
        event.remove(engine, "before_cursor_execute", no_mode_read)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("value", [{}, object()])
def test_v2_admission_policy_constructor_is_nominal(operation_store, value):
    engine = operation_store[0]
    with pytest.raises(TypeError, match="owned complete admission policy"):
        ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=value)


def test_v2_admission_missing_server_policy_refuses_without_mutation(operation_store):
    engine, _repository, authority, _service, sid = operation_store
    before = _admission_rows(engine)
    with pytest.raises(ValueError, match="no validated server policy"):
        admit(authority, sid, lifecycle_version=2, continuation_enabled=True)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize(
    "changed", ["nominal", "json", "mode", "session", "operation", "request_hash", "base", "reserved", "cursor", "action", "policy"]
)
def test_v2_admission_refuses_corrupt_or_substituted_seed_before_writes(operation_store, changed):
    import json
    from dataclasses import replace

    from tests.helpers.continuation_policy import minimal_continuation_policy

    from elspeth.contracts.errors import AuditIntegrityError

    engine, _repository, _authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Exact binding")
    seed = _admission_seed(sid, request)
    data = json.loads(seed.snapshot_json)
    if changed == "nominal":
        seed = {"snapshot_json": seed.snapshot_json}
    elif changed == "json":
        seed = replace(seed, snapshot_json="{}")
    elif changed == "action":
        seed = replace(seed, action_json=minimal_continuation_policy().actions.primary.model_dump_json())
    else:
        if changed == "mode":
            data["mutation_ceiling"]["initial_trust_mode"] = "explicit_approve"
        elif changed in ("session", "operation", "request_hash", "base", "reserved"):
            key = {
                "session": "session_id",
                "operation": "operation_id",
                "request_hash": "request_hash",
                "base": "base_state_id",
                "reserved": "original_user_message_id",
            }[changed]
            data["logical"][key] = "b" * 64 if changed == "request_hash" else str(uuid4())
        elif changed == "cursor":
            data["cursor"] = 1
        else:
            assert changed == "policy"
            policy = minimal_continuation_policy()
            policy = policy.model_copy(update={"segment": policy.segment.model_copy(update={"reserve": 26.0})})
            data["admission_policy_json"] = policy.model_dump_json()
            data["policy_hash"] = policy.policy_hash
            seed = replace(seed, policy_hash=policy.policy_hash)
        seed = replace(seed, snapshot_json=json.dumps(data))
    before = _admission_rows(engine)
    with pytest.raises((AuditIntegrityError, ValueError)):
        admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True, seed=seed)
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "different", "completed", "missing"])
async def test_v2_admission_recompose_joins_actual_original_user(operation_store, case):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.models import chat_messages_table

    engine, repository, _authority, service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    original = None
    if case != "missing":
        original = await service.add_message(sid, "user", "Build", writer_principal="route_user_message", session_operation_context=context)
        await service.add_message(
            sid,
            "assistant",
            "Saved reply" if case == "completed" else "tool prefix",
            writer_principal="compose_loop",
            session_operation_context=context,
            tool_calls=None
            if case == "completed"
            else [{"id": "call", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}],
        )
    repository.release(context)
    request = RecomposeRequest(
        operation_id=str(uuid4()), expected_user_message_id=uuid4() if case in ("missing", "different") else original.id
    )
    authority = _admission_v2_authority(operation_store)
    before = _admission_rows(engine)
    if case != "valid":
        with pytest.raises(ComposerOperationPreconditionRefused) as refused:
            admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
        expected = {
            "missing": {"detail": "No messages to recompose from"},
            "different": "recompose_user_message_mismatch",
            "completed": "recompose_already_completed",
        }[case]
        if case == "missing":
            assert refused.value.error.body == expected
        else:
            assert refused.value.error.body["detail"]["error_type"] == expected
        assert _admission_rows(engine) == before
    else:
        record, fresh = admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
        assert fresh and record.ingress_phase == "committed"
        assert record.user_message_id == record.reserved_user_message_id == original.id
        with engine.connect() as conn:
            assert [row.id for row in conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "user"))] == [
                str(original.id)
            ]
            snapshot = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
            assert decode_continuation_snapshot(snapshot.payload_json).logical.original_user_message_id == str(original.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("consistent_publication", [False, True])
async def test_malformed_v2_base_refuses_before_fresh_admission_writes(operation_store, consistent_publication):
    from dataclasses import replace
    from traceback import extract_tb

    from elspeth.contracts.composer_content import ContentPublication
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.sessions.content_publication import read_content_publication, storage_content_hash
    from elspeth.web.sessions.converters import state_from_record
    from elspeth.web.sessions.models import composition_states_table
    from elspeth.web.sessions.state_envelope import envelope_state_column

    engine, repository, _authority, service, sid = operation_store
    prior = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        saved = await service.save_composition_state(
            sid, _admission_state_data(), provenance="session_seed", session_operation_context=prior
        )
    finally:
        repository.release(prior)
    assert read_content_publication(saved) is not None
    assert state_from_record(saved).nodes[0].node_type == "transform"
    bad_nodes = deep_thaw(saved.nodes)
    bad_nodes[0]["branches"] = 17
    changes = {"nodes": envelope_state_column(bad_nodes)}
    if consistent_publication:
        corrupted = replace(saved, nodes=bad_nodes)
        composer_meta = deep_thaw(saved.composer_meta)
        composer_meta["content_publication"] = ContentPublication(
            saved.session_id, saved.id, saved.version, storage_content_hash(corrupted)
        ).to_payload()
        changes["composer_meta"] = envelope_state_column(composer_meta)
    with engine.begin() as conn:
        conn.execute(update(composition_states_table).where(composition_states_table.c.id == str(saved.id)).values(**changes))
    request = SendMessageRequest(operation_id=str(uuid4()), state_id=saved.id, content="Inspect graph")
    before = _admission_rows(engine)
    assert before["rate_limit_buckets"] == before["rate_limit_events"] == []
    expected = "base state cannot be hydrated" if consistent_publication else "Content publication does not match"
    with pytest.raises(AuditIntegrityError, match=expected) as refused:
        admit(
            _admission_v2_authority(operation_store),
            sid,
            request,
            lifecycle_version=2,
            continuation_enabled=True,
            seed=_admission_seed(sid, request),
        )
    if consistent_publication:
        cause = refused.value.__cause__
        assert type(cause) is TypeError and str(cause) == "'int' object is not iterable"
        frame = extract_tb(cause.__traceback__)[-1]
        assert frame.name == "from_dict" and "tuple(branches)" in frame.line
    else:
        assert refused.value.__cause__ is None
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_v2_admission_decoder_preconditions_and_async_forwarding_use_real_rows(operation_store):
    from elspeth.web.coordination import composer_operation_authority as operation_authority_module
    from elspeth.web.coordination.composer_operation_authority import admit_composer_operation
    from elspeth.web.sessions import service as sessions_service_module
    from elspeth.web.sessions.composer_operation_preconditions import (
        ComposerOperationPreconditions,
        check_composer_operation_preconditions_on_connection,
        check_composer_request_preconditions_on_connection,
    )
    from elspeth.web.sessions.converters import state_record_from_row
    from elspeth.web.sessions.models import composition_states_table
    from elspeth.web.sessions.state_decoder import state_record_from_row as canonical_decoder

    assert state_record_from_row is canonical_decoder
    assert sessions_service_module.state_record_from_row is canonical_decoder
    assert operation_authority_module.state_record_from_row is canonical_decoder
    assert canonical_decoder.__module__ == "elspeth.web.sessions.state_decoder"

    engine, repository, _authority, service, sid = operation_store
    prior = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        saved = await service.save_composition_state(
            sid, _admission_state_data(), provenance="session_seed", session_operation_context=prior
        )
    finally:
        repository.release(prior)
    authority = _admission_v2_authority(operation_store)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Current graph", state_id=saved.id)
    record, fresh = await admit_composer_operation(
        authority,
        quota=ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_hash=composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request),
        actor_user_id="alice",
        request_id=None,
        base_state_id=saved.id,
        request_json=request.model_dump_json(),
        deadline_seconds=180,
        max_nonterminal=64,
        auth_provider_type="local",
        lifecycle_version=2,
        continuation_enabled=True,
    )
    assert fresh and record.lifecycle_version == 2
    full = authority.get_for_turn(session_id=sid, operation_id=request.operation_id)
    with engine.connect() as conn:
        row = conn.execute(select(composition_states_table)).one()
        assert canonical_decoder(row) == state_record_from_row(row)
        assert state_record_from_row(row) == service._row_to_state_record(row) == saved
        assert check_composer_operation_preconditions_on_connection(conn, full, auth_provider_type="local") is None
        preconditions = ComposerOperationPreconditions(sid, "alice", None, saved.id, "compose_message", request.model_dump_json())
        assert check_composer_request_preconditions_on_connection(conn, preconditions, auth_provider_type="local") is None
        with pytest.raises(TypeError, match="job must be ComposerOperationRecord"):
            check_composer_operation_preconditions_on_connection(conn, preconditions, auth_provider_type="local")
        with pytest.raises(TypeError, match="job must be ComposerOperationPreconditions"):
            check_composer_request_preconditions_on_connection(conn, full, auth_provider_type="local")


@pytest.mark.parametrize("fault", ["chunk", "checkpoint", "segment", "quota"])
def test_v2_admission_child_sql_failure_rolls_back_whole_bundle_and_charge(operation_store, fault):
    from sqlalchemy import event

    from elspeth.web.coordination.rate_limit_authority import ComposerQuotaExceeded

    engine, _repository, _authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    quota = ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 1)
    if fault == "quota":
        with engine.begin() as conn:
            quota.check_on_connection(conn, "alice")
    before = _admission_rows(engine)
    observed = []
    targets = {
        "chunk": "composer_continuation_chunks",
        "checkpoint": "composer_continuation_checkpoints",
        "segment": "composer_operation_segments",
    }

    def fault_after_real_insert(conn, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.isinsert and context.compiled.statement.table.name == targets.get(fault):
            assert conn.execute(select(jobs)).one().lifecycle_version == 2
            observed.append(context.compiled.statement.table.name)
            raise RuntimeError("controlled continuation child SQL fault")

    event.listen(engine, "after_cursor_execute", fault_after_real_insert)
    try:
        with pytest.raises(ComposerQuotaExceeded if fault == "quota" else RuntimeError):
            admit(authority, sid, lifecycle_version=2, continuation_enabled=True, quota=quota)
    finally:
        event.remove(engine, "after_cursor_execute", fault_after_real_insert)
    assert observed == ([] if fault == "quota" else [targets[fault]])
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("case", ["replay", "disabled", "refused"])
def test_v2_admission_expired_quota_maintenance_is_separate_from_new_admission(operation_store, case):
    from datetime import timedelta

    from sqlalchemy import insert

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.models import rate_limit_buckets_table as buckets
    from elspeth.web.sessions.models import rate_limit_events_table as events

    engine, _repository, _authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Expiry counterevidence")
    if case == "replay":
        admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
    digest = "e" * 64
    expired_id = str(uuid4())
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            insert(buckets).values(
                subject_digest=digest,
                window_seconds=60,
                request_limit=1,
                updated_at=now - timedelta(minutes=2),
                expires_at=now - timedelta(minutes=1),
            )
        )
        conn.execute(
            insert(events).values(
                event_id=expired_id, subject_digest=digest, occurred_at=now - timedelta(minutes=2), expires_at=now - timedelta(minutes=1)
            )
        )
        expired_bucket = tuple(conn.execute(select(buckets).where(buckets.c.subject_digest == digest)).one())
        expired_event = tuple(conn.execute(select(events).where(events.c.event_id == expired_id)).one())
    before = _admission_rows(engine)
    assert expired_bucket in before["rate_limit_buckets"] and expired_event in before["rate_limit_events"]
    if case == "replay":
        record, fresh = admit(operation_store[2], sid, request, lifecycle_version=2)
        assert not fresh and record.operation_id == request.operation_id
    elif case == "disabled":
        with pytest.raises(ComposerOperationPreconditionRefused):
            admit(authority, sid, request, lifecycle_version=2)
    else:
        with pytest.raises(AuditIntegrityError, match="another initial session trust mode"):
            admit(
                authority,
                sid,
                request,
                lifecycle_version=2,
                continuation_enabled=True,
                seed=_admission_seed(sid, request, mode="explicit_approve"),
            )
    after = _admission_rows(engine)
    for table, rows in before.items():
        if table == "rate_limit_buckets":
            assert after[table] == [row for row in rows if row != expired_bucket]
        elif table == "rate_limit_events":
            assert after[table] == [row for row in rows if row != expired_event]
        else:
            assert after[table] == rows


@pytest.mark.parametrize("same_id", [False, True])
def test_v2_admission_real_concurrency_has_one_atomic_fresh_winner(operation_store, same_id):
    engine, _repository, _authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    common_id = str(uuid4())

    def submit(index):
        request = SendMessageRequest(operation_id=common_id if same_id else str(uuid4()), content="Concurrent")
        try:
            return admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
        except ComposerOperationActiveError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))
    assert sum(result is not None and result[1] for result in results) == 1
    assert sum(result is not None for result in results) == (2 if same_id else 1)
    rows = _admission_rows(engine)
    for name in (
        "composer_async_operations",
        "composer_continuation_chunks",
        "composer_continuation_checkpoints",
        "composer_operation_segments",
        "rate_limit_events",
    ):
        assert len(rows[name]) == 1
    assert rows["composer_segment_attempts"] == rows["chat_messages"] == []


@pytest.mark.parametrize(
    "changes", [{"lifecycle_version": True}, {"lifecycle_version": 3}, {"continuation_enabled": 1}, {"seed": object()}]
)
def test_v2_admission_configuration_refusals_preserve_default_v1(operation_store, changes):
    engine, _repository, authority, _service, sid = operation_store
    before = _admission_rows(engine)
    with pytest.raises(ValueError):
        admit(authority, sid, **changes)
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "foreign", "stale", "actor", "provider", "archive"])
async def test_v2_admission_current_principal_and_base_refusals_before_new_writes(operation_store, case):
    engine, repository, _authority, service, sid = operation_store
    state_id = None
    if case in ("foreign", "stale"):
        state_session = sid
        if case == "foreign":
            state_session = repository.create_session_with_initial_fence(
                user_id="alice", title="Other", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
            ).id
        prior = repository.acquire(
            session_id=state_session, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
        )
        try:
            saved = await service.save_composition_state(
                state_session, _admission_state_data(), provenance="session_seed", session_operation_context=prior
            )
        finally:
            repository.release(prior)
        if case == "foreign":
            state_id = saved.id
    elif case == "missing":
        state_id = uuid4()
    elif case == "archive":
        with engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
    request = SendMessageRequest(operation_id=str(uuid4()), content="Current authorization", state_id=state_id)
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationPreconditionRefused) as refused:
        admit(
            _admission_v2_authority(operation_store),
            sid,
            request,
            lifecycle_version=2,
            continuation_enabled=True,
            actor="other" if case == "actor" else "alice",
            provider="oidc" if case == "provider" else "local",
        )
    if case == "stale":
        assert refused.value.error.http_status == 409
        assert refused.value.error.body["error_type"] == "stale_compose_state"
    else:
        assert refused.value.error.http_status == 404
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("lifecycle", [1, 2])
def test_v2_admission_actual_insert_conflict_replay_preserves_existing_bundle(operation_store, monkeypatch, lifecycle):
    from sqlalchemy import event

    from elspeth.web.coordination import composer_operation_authority as module

    engine, _repository, _authority, _service, sid = operation_store
    authority = _admission_v2_authority(operation_store)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Savepoint conflict")
    original, _ = admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
    before = _admission_rows(engine)
    actual_read = module._read
    reads = []
    errors = []

    def initial_miss(conn, selected_sid, operation_id, *, body=False):
        row = actual_read(conn, selected_sid, operation_id, body=body)
        reads.append(row is not None)
        return None if len(reads) == 1 else row

    def capture_unique_refusal(context):
        errors.append(context.is_disconnect)

    monkeypatch.setattr(module, "_read", initial_miss)
    event.listen(engine, "handle_error", capture_unique_refusal)
    try:
        # D-2: a retry under either lifecycle replays the stored V2 bundle.
        replay, fresh = (
            admit(authority, sid, request)
            if lifecycle == 1
            else admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
        )
        assert not fresh and replay == original
    finally:
        event.remove(engine, "handle_error", capture_unique_refusal)
    assert reads == [True, True] and errors == [False]
    assert _admission_rows(engine) == before


def test_v2_admission_default_v1_never_consults_continuation_seed(operation_store, monkeypatch):
    from elspeth.web.coordination import composer_operation_authority as module

    def forbidden(*args, **kwargs):
        raise AssertionError("V1 invoked continuation seed producer")

    monkeypatch.setattr(module, "prepare_composer_continuation_admission", forbidden)
    _engine, _repository, authority, _service, sid = operation_store
    record, fresh = admit(authority, sid)
    assert fresh and record.lifecycle_version == 1
    assert record.legacy_deadline_at is not None


@pytest.mark.parametrize("deadline", [0, -1, float("nan")])
def test_v2_admission_keeps_legacy_worker_budget_validation(operation_store, deadline):
    engine, _repository, _authority, _service, sid = operation_store
    before = _admission_rows(engine)
    with pytest.raises(ValueError, match="invalid composer request budget"):
        admit(_admission_v2_authority(operation_store), sid, deadline_seconds=deadline, lifecycle_version=2, continuation_enabled=True)
    assert _admission_rows(engine) == before


def start(store, request=None, *, deadline_seconds=180.0):
    _engine, repository, authority, _service, sid = store
    record, _ = admit(authority, sid, request, deadline_seconds=deadline_seconds)
    (claim,) = authority.claim_next(limit=1)
    context = repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    return record, ComposerOperationRunning(claim=claim, session_operation_context=context)


def cancelled(*, request_id):
    return ComposerOperationError(
        http_status=499,
        failure_code="request_cancelled",
        error_type="request_cancelled",
        body={"error_type": "request_cancelled", "detail": "The composer request was stopped.", "request_id": request_id},
        diagnostic_id=None,
    )


def failed():
    return ComposerOperationError(
        http_status=503,
        failure_code="worker_lost",
        error_type="composer_operation_worker_lost",
        body={"error_type": "composer_operation_worker_lost", "detail": "Lost"},
        diagnostic_id=None,
    )


@pytest.mark.asyncio
async def test_failure_history_counts_only_exact_session_fence_revisions(operation_store):

    _engine, repository, authority, service, sid = operation_store
    initial = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    initial_state = await service.save_composition_state(
        sid, composition_state_data(), provenance="session_seed", session_operation_context=initial
    )
    repository.release(initial)
    record, running = start(operation_store, SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=initial_state.id))
    assert record.operation_id != running.session_operation_context.fence.operation_id
    for _ in range(2):
        last_owned = await service.save_composition_state(
            sid, composition_state_data(), provenance="session_seed", session_operation_context=running.session_operation_context
        )
    await service.fail_composer_async_operation(running, failure=failed())
    repository.release(running.session_operation_context)
    later = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    later_state = await service.save_composition_state(
        sid, composition_state_data(), provenance="session_seed", session_operation_context=later
    )
    repository.release(later)
    (outcome,), cursor = authority.list_failure_outcomes(session_id=sid, actor_user_id="alice")
    assert cursor is None
    assert outcome.operation_id == record.operation_id
    assert outcome.saved_revisions.attribution == "fenced"
    assert outcome.saved_revisions.count == 2
    assert outcome.saved_revisions.latest_state_id == last_owned.id
    assert outcome.saved_revisions.latest_version == last_owned.version
    assert later_state.version > outcome.saved_revisions.latest_version
    assert authority.list_failure_outcomes(session_id=sid, actor_user_id="other-actor") == ((), None)
    assert authority.list_failure_outcomes(session_id=uuid4(), actor_user_id="alice", operation_id=record.operation_id) == ((), None)


def test_failure_cursor_codec_preserves_wire_bytes_and_concrete_dto():
    from datetime import UTC, datetime

    from elspeth.web.sessions.composer_operations import (
        ComposerOperationFailureCursor,
        decode_composer_failure_cursor,
        encode_composer_failure_cursor,
    )

    cursor = ComposerOperationFailureCursor(
        settled_at=datetime(2026, 10, 15, 1, 2, 3, 456789, tzinfo=UTC),
        operation_id="00000000-0000-4000-8000-000000000001",
    )
    # Measured from the immutable pre-extraction codec; independent of this DTO's current serialization.
    previous_wire = (
        "eyJzZXR0bGVkX2F0IjoiMjAyNi0xMC0xNVQwMTowMjowMy40NTY3ODlaIiwib3BlcmF0aW9uX2lk"
        "IjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAxIn0="
    )
    assert encode_composer_failure_cursor(cursor) == previous_wire
    restored = decode_composer_failure_cursor(previous_wire)
    assert type(restored) is ComposerOperationFailureCursor
    assert restored == cursor
    assert "encode" not in ComposerOperationFailureCursor.__dict__
    assert "decode" not in ComposerOperationFailureCursor.__dict__


@pytest.mark.parametrize("value", ("", "x" * 513, "!", "é", "e30=", "bnVsbA=="))
def test_failure_cursor_codec_retains_invalid_wire_refusal(value):
    from elspeth.web.sessions.composer_operations import decode_composer_failure_cursor

    with pytest.raises(ValueError):
        decode_composer_failure_cursor(value)


def test_failure_history_tied_timestamp_cursor_is_stable_and_actor_scoped(operation_store, monkeypatch):
    from datetime import timedelta

    from elspeth.web.sessions.composer_operations import (
        ComposerOperationFailureCursor,
        decode_composer_failure_cursor,
        encode_composer_failure_cursor,
    )

    engine, _repository, authority, _service, sid = operation_store
    with engine.connect() as conn:
        fixed_now = database_now(conn) + timedelta(seconds=1)
    monkeypatch.setattr("elspeth.web.coordination.composer_operation_authority.database_now", lambda _conn: fixed_now)
    for _ in range(4):
        record, _ = admit(authority, sid)
        authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    all_outcomes, _ = authority.list_failure_outcomes(session_id=sid, actor_user_id="alice")
    assert {outcome.settled_at for outcome in all_outcomes} == {fixed_now}
    assert [outcome.operation_id for outcome in all_outcomes] == sorted((outcome.operation_id for outcome in all_outcomes), reverse=True)
    # A controlled clock forces the tie; every continuation must constrain both keys.
    for index, outcome in enumerate(all_outcomes):
        cursor = ComposerOperationFailureCursor(settled_at=outcome.settled_at, operation_id=outcome.operation_id)
        restored = decode_composer_failure_cursor(encode_composer_failure_cursor(cursor))
        remaining, _ = authority.list_failure_outcomes(session_id=sid, actor_user_id="alice", cursor=restored)
        assert remaining == all_outcomes[index + 1 :]
        assert authority.list_failure_outcomes(session_id=sid, actor_user_id="other-actor", cursor=restored) == ((), None)


def test_failure_history_omits_private_error_body_and_checks_sealed_hash(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError

    engine, _repository, authority, _service, sid = operation_store
    record, _ = admit(authority, sid)
    private = "SYNTHETIC_PRIVATE_FAILURE_CANARY"

    def private_cancelled(*, request_id):
        return ComposerOperationError(
            http_status=499,
            failure_code="request_cancelled",
            error_type=private,
            body={"detail": private, "request_id": private},
            diagnostic_id=None,
        )

    authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=private_cancelled)
    (outcome,), _ = authority.list_failure_outcomes(session_id=sid, actor_user_id="alice")
    assert private not in outcome.model_dump_json()
    assert outcome.request_id == "correlation"
    assert outcome.error_type is None
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(result_sha256="0" * 64))
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_composer_async_operations_transition_guard")
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(result_sha256="0" * 64))
    with pytest.raises(AuditIntegrityError):
        authority.list_failure_outcomes(session_id=sid, actor_user_id="alice")


def response(message, proposals, state):
    return MessageWithStateResponse(
        message=ChatMessageResponse(
            id=str(message.id),
            session_id=str(message.session_id),
            role="assistant",
            content=message.content,
            segments=[],
            created_at=message.created_at,
            sequence_no=message.sequence_no,
        ),
        state=None,
        proposals=[],
    )


def test_admission_replay_conflict_active_owner_and_provider(operation_store):
    _engine, _repo, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    first, fresh = admit(authority, sid, request)
    replay, second = admit(authority, sid, request)
    assert fresh and not second and first == replay and replay.request_json is None
    with pytest.raises(ComposerOperationConflictError):
        admit(authority, sid, request.model_copy(update={"content": "Changed"}))
    with pytest.raises(ComposerOperationActiveError):
        admit(authority, sid)
    for actor, provider in (("bob", "local"), ("alice", "oidc")):
        with pytest.raises(ComposerOperationPreconditionRefused) as refused:
            admit(authority, sid, actor=actor, provider=provider)
        assert refused.value.error.http_status == 404
    assert authority.count_nonterminal(limit=1) == 1


def test_concurrent_same_id_is_one_immutable_admission(operation_store):
    _engine, _repo, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: admit(authority, sid, request), range(4)))
    assert sum(fresh for _record, fresh in results) == 1
    assert len({record.operation_id for record, _fresh in results}) == 1


def test_claim_is_queue_only_and_running_never_requeued(operation_store):
    engine, _repo, authority, _service, sid = operation_store
    record, running = start(operation_store)
    assert authority.claim_next(limit=2) == ()
    with pytest.raises(ComposerOperationFenceLost):
        authority.renew_claim(running.claim)
    with pytest.raises(ComposerOperationFenceLost):
        authority.release_claim(running.claim)
    assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(status="queued"))


def test_start_refusal_rolls_back_fence_and_has_no_user_side_effect(operation_store):
    engine, repository, authority, _service, sid = operation_store
    request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=uuid4())
    admit(authority, sid, request)
    (claim,) = authority.claim_next(limit=1)
    with engine.connect() as conn:
        before = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
    with pytest.raises(ComposerOperationPreconditionRefused) as refused:
        repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    assert refused.value.error.body == {"detail": "No messages to recompose from"}
    with engine.connect() as conn:
        after = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
    assert before == after
    assert authority.get(session_id=sid, operation_id=claim.operation_id).status == "queued"


@pytest.mark.asyncio
async def test_cancel_refuses_business_but_sealed_failure_can_settle(operation_store):
    _engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        await service.update_session_title(sid, "late", session_operation_context=running.session_operation_context)
    # Captured nominal integrity evidence survives a committed Stop marker.
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    failure = project_composer_operation_error(AuditIntegrityError("private evidence"), request_id="correlation")
    terminal = await service.fail_composer_async_operation(running, failure=failure, authoritative_failure=True)
    assert terminal.status == "failed" and terminal.result_json == canonical_json(failure.model_dump(mode="json"))
    with pytest.raises(ComposerOperationFenceLost):
        repository.mutate(running.session_operation_context, lambda tx: tx.session.set_title(title="late", updated_at=tx.database_now))
    repository.release(running.session_operation_context)
    assert authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled) == terminal


@pytest.mark.asyncio
async def test_terminal_atomic_publication_and_immutability(operation_store):
    engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert terminal.status == "completed" and terminal.session_operation_epoch == running.session_operation_context.fence.operation_epoch
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    with pytest.raises(ComposerOperationFenceLost):
        await service.update_session_title(sid, "late", session_operation_context=running.session_operation_context)
    for mutation in (update(jobs).values(result_sha256="0" * 64), delete(jobs)):
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(mutation.where(jobs.c.operation_id == record.operation_id))
    repository.compare_and_swap(running.session_operation_context)
    with pytest.raises(ComposerOperationFenceLost):
        repository.mutate(running.session_operation_context, lambda transaction: None)
    repository.release(running.session_operation_context)
    # Lifecycle cascade is permitted, independent deletes are refused.
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(sid)))
    assert authority.get(session_id=sid, operation_id=record.operation_id) is None


@pytest.mark.asyncio
async def test_cancelled_terminal_awaiter_joins_actual_sql_and_returns_committed_terminal(operation_store, monkeypatch):
    _engine, _repo, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    entered, release = Event(), Event()
    original = service._insert_chat_message

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", delayed)
    task = asyncio.create_task(
        service.complete_composer_async_operation(
            running,
            assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
            assistant_record=None,
            audit_cohort=(),
            audit_composition_state_id=None,
            build_response=response,
        )
    )
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    terminal = await task
    assert terminal.status == "completed"
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal


@pytest.mark.parametrize(
    "values",
    (
        {"claim_token": None},
        {"claim_owner_instance_id": None},
        {"session_operation_id": None},
        {"session_operation_lease_token": None},
        {"session_operation_epoch": None},
        {"started_at": None},
        {"request_json": None},
        {"claim_expires_at": "non-null"},
        {"attempt": 0},
    ),
)
def test_running_bundle_rejects_nullability_and_fence_holes(operation_store, values):
    engine, _repo, _authority, _service, _sid = operation_store
    record, _running = start(operation_store)
    if values == {"claim_expires_at": "non-null"}:
        with engine.connect() as conn:
            values = {"claim_expires_at": database_now(conn)}
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(**values))


def test_queued_cancel_settles_without_a_claim_and_retains_immutable_body(operation_store):
    engine, _repo, authority, _service, sid = operation_store
    record, _fresh = admit(authority, sid)
    terminal = authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    assert terminal.status == "failed" and terminal.failure_code == "request_cancelled"
    assert terminal.settled_by == "request_cancel" and terminal.session_operation_epoch is None
    assert terminal.request_json is None and authority.claim_next(limit=1) == ()
    with engine.connect() as conn:
        names = {entry["name"] for entry in inspect(conn).get_foreign_keys("composer_async_operations")}
        assert "fk_composer_operations_user_message_session" in names
        assert "fk_composer_operations_base_state_session" in names


@pytest.mark.asyncio
async def test_recompose_tool_prefix_is_eligible_bare_narration_is_not(operation_store):
    _engine, repository, authority, service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    user = await service.add_message(sid, "user", "Build", writer_principal="route_user_message", session_operation_context=context)
    await service.add_message(
        sid,
        "assistant",
        "I started",
        writer_principal="compose_loop",
        session_operation_context=context,
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}],
    )
    repository.release(context)
    request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=user.id)
    record, running = start(operation_store, request)
    assert authority.get_for_turn(session_id=sid, operation_id=record.operation_id).user_message_id == user.id
    await service.add_message(
        sid, "assistant", "Saved reply", writer_principal="compose_loop", session_operation_context=running.session_operation_context
    )
    await service.fail_composer_async_operation(running, failure=failed())
    repository.release(running.session_operation_context)
    new_request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=user.id)
    admit(authority, sid, new_request)
    (claim,) = authority.claim_next(limit=1)
    with pytest.raises(ComposerOperationPreconditionRefused) as refused:
        repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    assert refused.value.error.body["detail"]["error_type"] == "recompose_already_completed"


@pytest.mark.asyncio
async def test_terminal_projection_error_replays_committed_terminal(operation_store, monkeypatch):
    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original = service._run_composer_sql

    async def committed_then_lost(func, *args, **kwargs):
        result = await original(func, *args, **kwargs)
        if isinstance(result, ComposerOperationRecord) and result.status == "completed":
            raise RuntimeError("lost acknowledgment")
        return result

    monkeypatch.setattr(service, "_run_composer_sql", committed_then_lost)
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert terminal == authority.get(session_id=sid, operation_id=record.operation_id)


@pytest.mark.asyncio
async def test_direct_sql_child_cancel_waits_for_actual_thread_completion(operation_store, monkeypatch):
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    children = []
    original = service._run_composer_sql

    async def capture_child(func):
        children.append(asyncio.current_task())
        return await original(func)

    def delayed_sql():
        entered.set()
        assert release.wait(5)
        completed.set()
        return authority.get_for_turn(session_id=sid, operation_id=record.operation_id)

    monkeypatch.setattr(service, "_run_composer_sql", capture_child)
    task = asyncio.create_task(service._run_composer_terminal_sql(running, delayed_sql))
    await _wait_for_thread_event(entered)
    children[0].cancel()
    await asyncio.sleep(0.03)
    assert not completed.is_set() and not task.done()
    release.set()
    result = await task
    assert completed.is_set() and result.status == "running"
    assert not isinstance(result, ComposerTerminalSQLCompletionUnknown)


@pytest.mark.asyncio
async def test_failed_fresh_writer_read_preserves_unknown_terminal_custody(operation_store, monkeypatch):
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    calls = []

    def sql_failure():
        calls.append("sql")
        raise RuntimeError("before commit")

    def unavailable_read(self, **kwargs):
        raise RuntimeError("writer unavailable")

    monkeypatch.setattr(type(authority), "get_with_database_now", unavailable_read)
    with pytest.raises(ComposerTerminalSQLCompletionUnknown):
        await service._run_composer_terminal_sql(running, sql_failure, can_retry=lambda row, now: True)
    assert calls == ["sql"]
    monkeypatch.undo()
    assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"


@pytest.mark.asyncio
async def test_unchanged_state_retries_once_after_actual_sql_completion(operation_store):
    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    calls = []

    def sql_failure_then_result():
        calls.append("sql")
        if len(calls) == 1:
            raise RuntimeError("rollback before commit")
        return authority.get_for_turn(session_id=sid, operation_id=record.operation_id)

    result = await service._run_composer_terminal_sql(running, sql_failure_then_result, can_retry=lambda row, now: True)
    assert calls == ["sql", "sql"] and result.status == "running"


def test_admission_binding_conflict_precedes_missing_base_membership(operation_store):
    _engine, _repository, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    admit(authority, sid, request)
    conflicting = SendMessageRequest(operation_id=request.operation_id, content="Changed", state_id=uuid4())
    with pytest.raises(ComposerOperationConflictError):
        admit(authority, sid, conflicting)
    # A fresh missing base is rejected before persistence once capacity permits.
    other = _repository.create_session_with_initial_fence(
        user_id="alice", title="Other", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    fresh = SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=uuid4())
    with pytest.raises(ComposerOperationPreconditionRefused) as refusal:
        admit(authority, other.id, fresh)
    assert refusal.value.error.http_status == 404
    assert authority.get(session_id=other.id, operation_id=fresh.operation_id) is None


@pytest.mark.asyncio
async def test_changed_stop_state_prevents_exact_retry(operation_store):
    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    calls = []

    def sql_rollback_with_stop():
        calls.append("sql")
        authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
        raise RuntimeError("rolled back")

    with pytest.raises(RuntimeError, match="rolled back"):
        await service._run_composer_terminal_sql(
            running, sql_rollback_with_stop, can_retry=lambda row, now: row.cancel_requested_at is None
        )
    assert calls == ["sql"]
    terminal = await service.fail_composer_async_operation(running, failure=failed())
    assert terminal.failure_code == "request_cancelled"


@pytest.mark.asyncio
async def test_real_schema_cross_session_and_cross_operation_bindings_are_rejected(operation_store):
    from sqlalchemy import insert

    from elspeth.web.sessions.models import message_ingress_receipts_table

    engine, repository, authority, service, sid = operation_store
    other = repository.create_session_with_initial_fence(
        user_id="alice", title="Foreign", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    foreign_context = repository.acquire(
        session_id=other.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    foreign_user = await service.add_message(
        other.id, "user", "Foreign", writer_principal="route_user_message", session_operation_context=foreign_context
    )
    state = await service.save_composition_state(
        other.id, composition_state_data(), provenance="session_seed", session_operation_context=foreign_context
    )
    repository.release(foreign_context)
    state_id = state.id
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=state_id)
    with pytest.raises(ComposerOperationPreconditionRefused) as refusal:
        admit(authority, sid, request)
    assert refusal.value.error.http_status == 404 and authority.get(session_id=sid, operation_id=request.operation_id) is None
    record, running = start(operation_store)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(user_message_id=str(foreign_user.id)))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(actor_user_id="missing-identity"))
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    with engine.connect() as conn:
        receipt = dict(
            conn.execute(select(message_ingress_receipts_table).where(message_ingress_receipts_table.c.session_id == str(sid)))
            .mappings()
            .one()
        )
    await service.fail_composer_async_operation(running, failure=failed())
    repository.release(running.session_operation_context)
    second, _ = admit(authority, sid)
    receipt["operation_id"] = second.operation_id
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(insert(message_ingress_receipts_table).values(**receipt))
    receipt["session_id"] = str(other.id)
    receipt["operation_id"] = record.operation_id
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(insert(message_ingress_receipts_table).values(**receipt))
    with engine.connect() as conn:
        assert (
            len(conn.execute(select(message_ingress_receipts_table).where(message_ingress_receipts_table.c.session_id == str(sid))).all())
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("assistant", "audit", "terminal"))
async def test_terminal_transaction_fault_windows_keep_one_bundle(operation_store, monkeypatch, seam):
    import elspeth.web.sessions.service as service_module
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    builds = []
    calls = []

    def build(message, proposals, current):
        builds.append(message.id)
        return response(message, proposals, current)

    if seam == "assistant":
        original = service._insert_chat_message

        def inject(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs["role"] == "assistant":
                calls.append("assistant")
                raise RuntimeError("after assistant before commit")
            return value

        monkeypatch.setattr(service, "_insert_chat_message", inject)
    elif seam == "audit":
        original = service._write_audit_cohort_on_connection

        def inject(*args, **kwargs):
            original(*args, **kwargs)
            calls.append("audit")
            raise RuntimeError("after audit before commit")

        monkeypatch.setattr(service, "_write_audit_cohort_on_connection", inject)
    else:
        original = service_module.settle_composer_operation_on_connection

        def inject(*args, **kwargs):
            value = original(*args, **kwargs)
            calls.append("terminal")
            if len(calls) == 1:
                raise RuntimeError("after terminal CAS before commit")
            return value

        monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", inject)
    operation = service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(AuditMessageDraft(role="audit", content="Evidence"),),
        audit_composition_state_id=None,
        build_response=build,
    )
    if seam == "terminal":
        terminal = await operation
        assert terminal.status == "completed" and len(calls) == 2 and len(builds) == 1
        assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
        expected_roles = ["user", "assistant", "audit"]
    else:
        with pytest.raises(RuntimeError, match="before commit"):
            await operation
        assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
        assert calls == [seam] and builds == []
        expected_roles = ["user"]
    with engine.connect() as conn:
        roles = list(
            conn.execute(
                select(chat_messages_table.c.role)
                .where(chat_messages_table.c.session_id == str(sid))
                .order_by(chat_messages_table.c.sequence_no)
            ).scalars()
        )
    assert roles == expected_roles


def test_distinct_ids_race_has_one_active_winner(operation_store):
    _engine, _repository, authority, _service, sid = operation_store
    requests = [SendMessageRequest(operation_id=str(uuid4()), content="Build") for _ in range(4)]

    def submit(request):
        try:
            record, fresh = admit(authority, sid, request)
        except ComposerOperationActiveError as exc:
            return exc.operation_id, False
        return record.operation_id, fresh

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, requests))
    assert sum(fresh for _operation_id, fresh in results) == 1
    assert len({operation_id for operation_id, _fresh in results}) == 1
    assert authority.count_nonterminal(limit=10) == 1


def test_stale_claim_cannot_release_or_renew_reclaimed_work(operation_store):
    from datetime import timedelta

    engine, _repository, authority, _service, sid = operation_store
    record, _fresh = admit(authority, sid)
    (first,) = authority.claim_next(limit=1)
    with engine.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.operation_id == record.operation_id)
            .values(claim_expires_at=database_now(conn) - timedelta(seconds=1))
        )
    with pytest.raises(ComposerOperationFenceLost):
        authority.renew_claim(first)
    (second,) = authority.claim_next(limit=1)
    assert second.attempt == first.attempt + 1 and second.claim_token != first.claim_token
    with pytest.raises(ComposerOperationFenceLost):
        authority.release_claim(first)
    assert authority.renew_claim(second).claim_token_present
    authority.release_claim(second)
    current = authority.get(session_id=sid, operation_id=record.operation_id)
    assert current.status == "queued" and not current.claim_token_present


def _admit_in_independent_process(database_url: str, session_id: str, operation_id: str) -> tuple[str, bool]:
    engine = create_session_engine(database_url)
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    try:
        try:
            record, fresh = admit(authority, UUID(session_id), SendMessageRequest(operation_id=operation_id, content="Build"))
        except ComposerOperationActiveError as exc:
            return exc.operation_id, False
        return record.operation_id, fresh
    finally:
        engine.dispose()


@pytest.mark.parametrize("same_id", (True, False))
def test_independent_process_admission_converges_on_one_action(operation_store, same_id):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    engine, _repository, authority, _service, sid = operation_store
    first = str(uuid4())
    ids = [first, first if same_id else str(uuid4())]
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_admit_in_independent_process, [engine.url.render_as_string(hide_password=False)] * 2, [str(sid)] * 2, ids))
    assert sum(fresh for _operation_id, fresh in results) == 1
    assert len({operation_id for operation_id, _fresh in results}) == 1
    assert authority.count_nonterminal(limit=10) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_state", ("stop", "deadline"))
@pytest.mark.parametrize("original_integrity", (False, True))
async def test_stop_between_fresh_read_and_retry_lock_refuses_success(operation_store, monkeypatch, changed_state, original_integrity):
    import elspeth.web.sessions.service as service_module
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store, deadline_seconds=3.0 if changed_state == "deadline" else 180.0)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original_settle = service_module.settle_composer_operation_on_connection
    original_read = type(authority).get_with_database_now
    original_error = (
        AuditIntegrityError("before commit acknowledgment") if original_integrity else RuntimeError("before commit acknowledgment")
    )
    calls = []
    read_calls = []
    builds = []

    def lose_before_commit(*args, **kwargs):
        original_settle(*args, **kwargs)
        calls.append("terminal")
        raise original_error

    def stop_after_snapshot(self, **kwargs):
        result = original_read(self, **kwargs)
        if not read_calls:
            read_calls.append("stop")
            if changed_state == "stop":
                authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
            else:
                import time

                time.sleep(4.1)
        return result

    def build(message, proposals, current):
        builds.append(message.id)
        return response(message, proposals, current)

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(service_module, "settle_composer_operation_on_connection", lose_before_commit)
        fault_patch.setattr(type(authority), "get_with_database_now", stop_after_snapshot)
        with pytest.raises(AuditIntegrityError if original_integrity else RuntimeError, match="before commit acknowledgment") as captured:
            await service.complete_composer_async_operation(
                running,
                assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
                assistant_record=None,
                audit_cohort=(),
                audit_composition_state_id=None,
                build_response=build,
            )
        assert calls == ["terminal"] and len(builds) == 1 and read_calls == ["stop"]
    assert captured.value is original_error
    terminal = await service.fail_composer_async_operation(
        running, failure=project_composer_operation_error(captured.value, request_id=None), authoritative_failure=original_integrity
    )
    assert terminal.failure_code == (
        "http_error" if original_integrity else "request_cancelled" if changed_state == "stop" else "deadline_expired"
    )
    if changed_state == "deadline" and not original_integrity:
        assert terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.http_status == 504 and error.failure_code == "deadline_expired"
        assert error.error_type == "composer_operation_deadline_expired"
        assert error.body == {
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request timed out. Reload to review the current session state before resubmitting.",
            "timeout_seconds": 3.0,
            "request_id": "correlation",
        }
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            "user"
        ]


@pytest.mark.asyncio
async def test_new_job_actor_and_base_foreign_keys_have_unmasked_negative_controls(operation_store):
    from sqlalchemy import insert

    engine, repository, authority, service, sid = operation_store
    parent, _fresh = admit(authority, sid)
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    state = await service.save_composition_state(
        sid, composition_state_data(), provenance="session_seed", session_operation_context=context
    )
    repository.release(context)
    with engine.connect() as conn:
        template = dict(conn.execute(select(jobs).where(jobs.c.operation_id == parent.operation_id)).mappings().one())
    for field, invalid in (("actor_user_id", "absent-actor"), ("base_state_id", str(state.id))):
        other = repository.create_session_with_initial_fence(
            user_id="alice", title="No job", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
        )
        values = dict(template)
        values["session_id"] = str(other.id)
        values[field] = invalid
        request = SendMessageRequest(
            operation_id=parent.operation_id, content="Build", state_id=state.id if field == "base_state_id" else None
        )
        values["request_json"] = request.model_dump_json()
        values["request_hash"] = composer_operation_request_hash(session_id=other.id, kind="compose_message", request=request)
        with engine.begin() as conn, pytest.raises(IntegrityError) as error:
            conn.execute(insert(jobs).values(**values))
        assert "foreign key" in str(error.value).lower()
        assert authority.get(session_id=other.id, operation_id=parent.operation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("hash", "shape"))
async def test_terminal_read_rejects_tampering_after_named_guard_removed(operation_store, tamper):
    from elspeth.contracts.errors import AuditIntegrityError

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(result_sha256="0" * 64))
    with engine.begin() as conn:
        sql = "DROP TRIGGER trg_composer_async_operations_transition_guard"
        if conn.dialect.name == "postgresql":
            sql += " ON composer_async_operations"
        conn.exec_driver_sql(sql)
        values = {"result_sha256": "0" * 64}
        if tamper == "shape":
            encoded = canonical_json({"unexpected": True})
            values = {"result_json": encoded, "result_sha256": terminal.result_sha256}
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(**values))
    with pytest.raises(AuditIntegrityError):
        authority.get(session_id=sid, operation_id=record.operation_id)


@pytest.mark.asyncio
async def test_final_job_base_delete_is_restricted_but_session_cascade_succeeds(operation_store):
    from sqlalchemy import delete

    from elspeth.web.sessions.models import composition_states_table, sessions_table

    engine, repository, authority, service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    state = await service.save_composition_state(
        sid, composition_state_data(), provenance="session_seed", session_operation_context=context
    )
    repository.release(context)
    record, fresh = admit(authority, sid, SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=state.id))
    assert fresh and record.base_state_id == state.id
    with engine.begin() as conn, pytest.raises(IntegrityError, match=r"(?i)foreign key"):
        conn.execute(delete(composition_states_table).where(composition_states_table.c.id == str(state.id)))
    with engine.connect() as conn:
        assert conn.execute(
            select(composition_states_table.c.id).where(composition_states_table.c.id == str(state.id))
        ).scalar_one() == str(state.id)
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(sid)))
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.operation_id).where(jobs.c.session_id == str(sid))).all() == []
        assert conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == str(sid))).all() == []


@pytest.mark.parametrize("changed_state", ("stop", "deadline"))
@pytest.mark.parametrize("original_integrity", (False, True))
def test_queued_settlement_rechecks_priority_under_owned_claim_lock(operation_store, changed_state, original_integrity):
    import time

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    _engine, _repository, authority, _service, sid = operation_store
    record, _fresh = admit(authority, sid, deadline_seconds=3.0 if changed_state == "deadline" else 180.0)
    (claim,) = authority.claim_next(limit=1)
    original = AuditIntegrityError("captured accounting evidence") if original_integrity else RuntimeError("captured start fault")
    failure = project_composer_operation_error(original, request_id="correlation")
    if changed_state == "stop":
        marked = authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
        assert marked.status == "queued" and marked.cancel_requested_at is not None
    else:
        time.sleep(4.1)
    terminal = authority.settle_unstarted(
        claim, session_id=sid, operation_id=record.operation_id, failure=failure, authoritative_failure=original_integrity
    )
    assert terminal.failure_code == (
        "http_error" if original_integrity else "request_cancelled" if changed_state == "stop" else "deadline_expired"
    )
    if changed_state == "deadline" and not original_integrity:
        assert terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.http_status == 504 and error.failure_code == "deadline_expired"
        assert error.error_type == "composer_operation_deadline_expired"
        assert error.body == {
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request timed out. Reload to review the current session state before resubmitting.",
            "timeout_seconds": 3.0,
            "request_id": "correlation",
        }
    assert terminal.status == "failed" and terminal.settled_by == "settle_unstarted" and terminal.user_message_id is None
    assert authority.settle_unstarted(claim, session_id=sid, operation_id=record.operation_id, failure=failed()) == terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_window", ("before_driver_commit", "after_driver_commit"))
async def test_driver_commit_fault_reconciles_one_immutable_terminal(operation_store, monkeypatch, commit_window):
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    driver_commit = engine.dialect.do_commit
    commits = []
    builds = []

    def fault_at_driver(connection):
        commits.append(commit_window)
        if len(commits) == 1:
            if commit_window == "after_driver_commit":
                driver_commit(connection)
            raise RuntimeError("driver commit acknowledgment lost")
        driver_commit(connection)

    def build(message, proposals, current):
        builds.append(message.id)
        return response(message, proposals, current)

    monkeypatch.setattr(engine.dialect, "do_commit", fault_at_driver)
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=build,
    )
    assert terminal.status == "completed" and len(builds) == 1
    assert len(commits) == (2 if commit_window == "before_driver_commit" else 1)
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            "user",
            "assistant",
        ]


async def _assert_operation_rows_absent(engine, sid):
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.operation_id).where(jobs.c.session_id == str(sid))).all() == []


@pytest.mark.asyncio
async def test_final_job_actor_delete_has_unmasked_restrict_control(operation_store):
    from sqlalchemy import delete, insert
    from tests.fixtures.identities import ensure_test_identity

    from elspeth.web.sessions.models import identities_table, sessions_table

    engine, repository, authority, _service, sid = operation_store
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="unbound-actor")
        assert conn.execute(delete(identities_table).where(identities_table.c.identity_id == "unbound-actor")).rowcount == 1
        ensure_test_identity(conn, identity_id="unbound-actor")
    record, _fresh = admit(authority, sid)
    with engine.connect() as conn:
        values = dict(conn.execute(select(jobs).where(jobs.c.operation_id == record.operation_id)).mappings().one())
    other = repository.create_session_with_initial_fence(
        user_id="alice", title="Actor control", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    values["session_id"] = str(other.id)
    values["actor_user_id"] = "unbound-actor"
    request = SendMessageRequest(operation_id=record.operation_id, content="Build")
    values["request_hash"] = composer_operation_request_hash(session_id=other.id, kind="compose_message", request=request)
    with engine.begin() as conn:
        conn.execute(insert(jobs).values(**values))
    with engine.begin() as conn, pytest.raises(IntegrityError, match=r"(?i)foreign key"):
        conn.execute(delete(identities_table).where(identities_table.c.identity_id == "unbound-actor"))
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(other.id)))
        assert conn.execute(delete(identities_table).where(identities_table.c.identity_id == "unbound-actor")).rowcount == 1
    await _assert_operation_rows_absent(engine, other.id)


@pytest.mark.asyncio
async def test_final_job_user_delete_guard_and_foreign_key_are_independent(operation_store):
    from sqlalchemy import delete

    from elspeth.web.sessions.models import chat_messages_table, sessions_table

    engine, _repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    ingress = await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(delete(chat_messages_table).where(chat_messages_table.c.id == str(ingress.message.id)))
    with engine.begin() as conn:
        sql = "DROP TRIGGER trg_chat_messages_no_delete"
        if conn.dialect.name == "postgresql":
            sql += " ON chat_messages"
        conn.exec_driver_sql(sql)
        ingress_sql = "DROP TRIGGER trg_message_ingress_receipts_no_delete"
        if conn.dialect.name == "postgresql":
            ingress_sql += " ON message_ingress_receipts"
        conn.exec_driver_sql(ingress_sql)
    with engine.begin() as conn, pytest.raises(IntegrityError, match=r"(?i)foreign key"):
        conn.execute(delete(chat_messages_table).where(chat_messages_table.c.id == str(ingress.message.id)))
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(sid)))
    await _assert_operation_rows_absent(engine, sid)


@pytest.mark.asyncio
async def test_exact_retry_preserves_entire_assistant_audit_and_response_bundle(operation_store, monkeypatch):
    import time

    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original_insert = service._insert_chat_message
    original_commit = engine.dialect.do_commit
    writes = []
    commits = []

    def capture_insert(conn, **kwargs):
        identifier = original_insert(conn, **kwargs)
        writes.append((identifier, kwargs["role"], kwargs["created_at"], kwargs["sequence_no"], kwargs["content"]))
        return identifier

    def lose_first_commit(connection):
        commits.append("commit")
        if len(commits) == 1:
            time.sleep(1.1)
            raise RuntimeError("first complete immutable bundle rolled back")
        original_commit(connection)

    monkeypatch.setattr(service, "_insert_chat_message", capture_insert)
    monkeypatch.setattr(engine.dialect, "do_commit", lose_first_commit)
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(AuditMessageDraft(role="audit", content="evidence"),),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert writes[:2] == writes[2:] and len(writes) == 4 and len(commits) == 2
    with engine.connect() as conn:
        stored = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == writes[0][0])).one()
    sealed = MessageWithStateResponse.model_validate_json(terminal.result_json, strict=True)
    assert sealed.message.created_at == service._row_to_chat_message_record(stored).created_at == writes[0][2]


@pytest.mark.asyncio
async def test_failed_terminal_has_one_exact_immutable_retry(operation_store, monkeypatch):
    import elspeth.web.sessions.service as service_module

    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    original_settle = service_module.settle_composer_operation_on_connection
    bundles = []

    def lose_first_ack(*args, **kwargs):
        bundles.append((kwargs["outcome"], kwargs["settled_at"]))
        result = original_settle(*args, **kwargs)
        if len(bundles) == 1:
            raise RuntimeError("failed terminal precommit acknowledgment")
        return result

    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", lose_first_ack)
    terminal = await service.fail_composer_async_operation(running, failure=failed())
    assert terminal.status == "failed" and terminal.failure_code == "worker_lost"
    assert len(bundles) == 2 and bundles[0] == bundles[1]
    assert terminal.settled_at == bundles[0][1]
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("normal", "retry", "audit_failure", "cancel", "deadline", "lost_fence"))
async def test_failed_terminal_private_cause_is_atomic_frozen_and_exactly_correlated(operation_store, monkeypatch, case):
    import json
    from datetime import timedelta

    import elspeth.web.sessions.service as service_module
    from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
    from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts
    from elspeth.web.coordination import composer_operation_authority as job_authority
    from elspeth.web.coordination.contracts import SessionOperationFenceLost
    from elspeth.web.sessions.composer_operation_errors import operation_failure_diagnostic_id, project_composer_operation_error
    from elspeth.web.sessions.models import chat_messages_table
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    engine, repository, authority, service, sid = operation_store
    if case == "deadline":
        # Keep the one-second job and its live fence stable through setup, then
        # cross the actual terminal selector's clock at the intended seam.
        with engine.connect() as conn:
            controlled_now = job_authority.database_now(conn)
        monkeypatch.setattr(job_authority, "database_now", lambda _conn: controlled_now)
        monkeypatch.setattr(service_module, "database_now", lambda _conn: controlled_now)
        monkeypatch.setattr(repository, "_database_now", lambda _conn: controlled_now)
    record, running = start(operation_store, deadline_seconds=1.0 if case == "deadline" else 180.0)
    original = RuntimeError("B7_PRIVATE_CAUSE_CANARY: late settlement failure")
    capture = ComposerOperationFailureFacts(
        record.operation_id, record.request_id, running.session_operation_context, capture_failure_facts(original)
    )
    failure = project_composer_operation_error(
        original,
        request_id=record.request_id,
        diagnostic_id=operation_failure_diagnostic_id(session_id=str(sid), operation_id=record.operation_id),
    )
    earlier = await service.add_message(
        sid,
        "audit",
        "Earlier durable audit cohort",
        writer_principal="compose_loop",
        session_operation_context=running.session_operation_context,
    )
    if case == "cancel":
        authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    elif case == "deadline":
        controlled_now = record.deadline_at + timedelta(microseconds=1)
    elif case == "lost_fence":
        repository.release(running.session_operation_context)
    actual_write = service._write_audit_cohort_on_connection
    actual_settle = service_module.settle_composer_operation_on_connection
    writes = []
    settlements = []

    def write(conn, **kwargs):
        writes.append((kwargs["drafts"], kwargs["message_ids"], kwargs["created_at"], kwargs["recorded_at"]))
        result = actual_write(conn, **kwargs)
        if case == "audit_failure":
            raise RuntimeError("Synthetic private cohort insert failure")
        return result

    def settle(*args, **kwargs):
        settlements.append((kwargs["outcome"], kwargs["settled_at"]))
        result = actual_settle(*args, **kwargs)
        if case == "retry" and len(settlements) == 1:
            raise RuntimeError("Synthetic acknowledgment failure after diagnostic insert")
        return result

    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", write)
    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", settle)
    if case in {"audit_failure", "lost_fence"}:
        with pytest.raises(ComposerTerminalSQLCompletionUnknown if case == "audit_failure" else SessionOperationFenceLost):
            await service.fail_composer_async_operation(running, failure=failure, operation_failure_facts=capture)
        terminal = authority.get(session_id=sid, operation_id=record.operation_id)
        assert terminal.status == "running"
    else:
        terminal = await service.fail_composer_async_operation(running, failure=failure, operation_failure_facts=capture)
        assert terminal.status == "failed"
        assert terminal.failure_code == (
            "request_cancelled" if case == "cancel" else "deadline_expired" if case == "deadline" else "operation_failed"
        )
        assert "B7_PRIVATE_CAUSE_CANARY" not in terminal.result_json
    rows = await service.get_messages(sid, limit=None)
    assert rows[0].id == earlier.id
    diagnostics = [envelope for row in rows for envelope in row.tool_calls or () if envelope.get("_kind") == "composer_diagnostic_evidence"]
    if case in {"audit_failure", "lost_fence"}:
        assert diagnostics == []
        assert len(writes) == (2 if case == "audit_failure" else 0)
        assert settlements == []
    else:
        assert len(diagnostics) == 1
        evidence = ComposerDiagnosticEvidence.from_envelope(
            diagnostics[0], expected_session_id=str(sid), expected_operation_id=running.session_operation_context.fence.operation_id
        )
        assert evidence.operation_epoch == running.session_operation_context.fence.operation_epoch
        assert evidence.result.content == capture.facts.canonical
        correlation = json.loads(evidence.arguments.content)
        selected = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert correlation["diagnostic_id"] == selected.diagnostic_id
        assert correlation["durable_operation_id"] == record.operation_id
        assert correlation["request_id"] == record.request_id
        assert correlation["failure_code"] == terminal.failure_code
        assert len(writes) == len(settlements) == (2 if case == "retry" else 1)
        with engine.connect() as conn:
            stored = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == writes[-1][1][0])).one()
        assert service._row_to_chat_message_record(stored).created_at == terminal.settled_at
    if len(writes) == 2:
        assert writes[0] == writes[1]
        assert writes[0][0][0] is writes[1][0][0]
    if case != "lost_fence":
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_failed_terminal_private_cause_waits_for_actual_sql_through_caller_cancellation(operation_store, monkeypatch):
    from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts
    from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    _engine, repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    original = RuntimeError("Private original finalization failure")
    capture = ComposerOperationFailureFacts(
        record.operation_id, record.request_id, running.session_operation_context, capture_failure_facts(original)
    )
    failure = project_composer_operation_error(original, request_id=record.request_id)
    entered, release, completed = Event(), Event(), Event()
    actual_write = service._write_audit_cohort_on_connection

    def write(conn, **kwargs):
        entered.set()
        assert release.wait(5), "Controlled diagnostic SQL was not released"
        result = actual_write(conn, **kwargs)
        completed.set()
        return result

    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", write)
    task = asyncio.create_task(service.fail_composer_async_operation(running, failure=failure, operation_failure_facts=capture))
    try:
        await _wait_for_thread_event(entered)
        task.cancel("First caller cancellation")
        await asyncio.sleep(0)
        task.cancel("Second caller cancellation")
        await asyncio.sleep(0.02)
        assert not completed.is_set() and not task.done()
    finally:
        release.set()
    terminal = await task
    assert completed.is_set() and terminal.status == "failed"
    diagnostics = [
        envelope
        for row in await service.get_messages(sid, limit=None)
        for envelope in row.tool_calls or ()
        if envelope.get("_kind") == "composer_diagnostic_evidence"
    ]
    assert len(diagnostics) == 1
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_required_ingress_cancellation_retains_actual_sql_until_binding_commit(operation_store, monkeypatch):
    from elspeth.web.sessions.models import chat_messages_table, message_ingress_receipts_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    entered, release = Event(), Event()
    original_insert = service._insert_chat_message

    def parked_insert(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", parked_insert)
    task = asyncio.create_task(
        service.add_message_with_transcript(
            sid,
            "user",
            "Build",
            operation_id=UUID(record.operation_id),
            requested_state_id=None,
            writer_principal="route_user_message",
            session_operation_context=running.session_operation_context,
            running=running,
        )
    )
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    current = authority.get(session_id=sid, operation_id=record.operation_id)
    assert current.status == "running" and current.user_message_id is not None
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.session_id == str(sid))).scalars().all() == [
            str(current.user_message_id)
        ]
        assert conn.execute(
            select(message_ingress_receipts_table.c.user_message_id).where(message_ingress_receipts_table.c.session_id == str(sid))
        ).scalars().all() == [str(current.user_message_id)]


@pytest.mark.asyncio
async def test_failed_terminal_prewrite_fence_refusal_preserves_recovery_authority(operation_store):
    from elspeth.web.coordination.contracts import SessionOperationFenceLost

    _engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    repository.release(running.session_operation_context)
    with pytest.raises(SessionOperationFenceLost):
        await service.fail_composer_async_operation(running, failure=failed())
    current = authority.get(session_id=sid, operation_id=record.operation_id)
    assert current.status == "running" and current.result_json is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["audit", "assistant"])
async def test_required_audit_sql_failure_preserves_canonical_integrity_classification(operation_store, monkeypatch, seam):
    from sqlalchemy.exc import OperationalError

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original_error = OperationalError("controlled required write", {}, RuntimeError("storage fault"))
    if seam == "audit":

        def refuse_audit(*args, **kwargs):
            raise original_error

        monkeypatch.setattr(service, "_write_audit_cohort_on_connection", refuse_audit)
    else:
        original_insert = service._insert_chat_message

        def refuse_assistant(*args, **kwargs):
            if kwargs["role"] == "assistant":
                raise original_error
            return original_insert(*args, **kwargs)

        monkeypatch.setattr(service, "_insert_chat_message", refuse_assistant)
    expected = AuditIntegrityError if seam == "audit" else OperationalError
    with pytest.raises(expected) as caught:
        await service.complete_composer_async_operation(
            running,
            assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
            assistant_record=None,
            audit_cohort=(AuditMessageDraft(role="audit", content="Evidence"),),
            audit_composition_state_id=None,
            build_response=response,
        )
    if seam == "audit":
        assert caught.value.__cause__ is original_error
    else:
        assert caught.value is original_error
    assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            "user"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["intermediate", "cohort"])
async def test_required_intermediate_cancellation_joins_actual_sql(operation_store, monkeypatch, writer):
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    entered, release = Event(), Event()
    original_insert = service._insert_chat_message

    def parked_insert(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", parked_insert)
    if writer == "intermediate":
        work = service.persist_compose_turn_async(
            session_id=str(sid),
            assistant_content="Observed",
            redacted_assistant_tool_calls=(),
            redacted_tool_rows=(),
            revoked_tool_rows=(),
            private_tool_evidence=(),
            parent_composition_state_id=None,
            expected_current_state_id=None,
            writer_principal="compose_loop",
            plugin_crash_pending=False,
            session_operation_context=running.session_operation_context,
        )
        expected_role = "assistant"
    else:
        work = service.add_messages_atomic(
            sid,
            (AuditMessageDraft(role="audit", content="Observed"),),
            writer_principal="compose_loop",
            session_operation_context=running.session_operation_context,
            audit_only=True,
        )
        expected_role = "audit"
    task = asyncio.create_task(work)
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            expected_role
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "cancel", "settle"])
async def test_required_provider_sql_cancellation_joins_committed_evidence(operation_store, monkeypatch, phase):
    from datetime import UTC, datetime

    from elspeth.web.coordination.quota_authority import TokenUsageEntry
    from elspeth.web.sessions import service as service_module
    from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table

    engine, _repository, _authority, service, _sid = operation_store
    _record, running = start(operation_store)
    context = running.session_operation_context
    from tests.fixtures.identities import grant_test_pipeline_user

    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    attempt = None
    if phase != "begin":
        attempt = await service.begin_provider_attempt(session_operation_context=context, source="auto_title")
    entered, release = Event(), Event()
    calls = []
    if phase == "begin":
        owner, name = service, "_begin_provider_attempt_sync"
        original = service._begin_provider_attempt_sync
    elif phase == "cancel":
        owner, name = service_module, "cancel_undispatched_provider_attempt_on_connection"
        original = service_module.cancel_undispatched_provider_attempt_on_connection
    else:
        owner, name = service, "_settle_provider_attempt_sync"
        original = service._settle_provider_attempt_sync

    def parked(*args, **kwargs):
        calls.append(phase)
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, parked)
    if phase == "begin":
        work = service.begin_provider_attempt(session_operation_context=context, source="auto_title")
    elif phase == "cancel":
        work = service.cancel_undispatched_provider_attempt(
            session_operation_context=context, attempt_id=attempt.attempt_id, requested_model="test/title"
        )
    else:
        work = service.settle_provider_attempt(
            session_operation_context=context,
            attempt_id=attempt.attempt_id,
            entry=TokenUsageEntry(
                model="test/title",
                prompt_tokens=3,
                completion_tokens=2,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                call_id=attempt.attempt_id,
                recorded_at=datetime.now(UTC),
            ),
        )
    task = asyncio.create_task(work)
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    with engine.connect() as conn:
        assert list(conn.execute(select(token_usage_ledger_table.c.entry_id)).scalars()) == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [phase]
    with engine.connect() as conn:
        attempts = conn.execute(select(quota_provider_attempts_table)).all()
        ledger = conn.execute(select(token_usage_ledger_table)).all()
    assert len(attempts) == 1
    if phase == "begin":
        assert attempts[0].settled_at is None and ledger == []
    else:
        assert attempts[0].settled_at is not None and len(ledger) == 1
        assert ledger[0].prompt_tokens == (0 if phase == "cancel" else 3)


@pytest.mark.asyncio
async def test_queued_provider_admission_cancellation_does_not_create_attempt(operation_store, monkeypatch):
    from tests.fixtures.identities import grant_test_pipeline_user

    from elspeth.web import async_workers
    from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table

    engine, _repository, _authority, service, _sid = operation_store
    _record, running = start(operation_store)
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    occupied, release, submitted = Event(), Event(), Event()

    class WitnessExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            result = super().submit(fn, *args, **kwargs)
            submitted.set()
            return result

    def occupy():
        occupied.set()
        assert release.wait(timeout=5)

    with WitnessExecutor(max_workers=1) as executor:
        blocker = executor.submit(occupy)
        await _wait_for_thread_event(occupied)
        submitted.clear()
        monkeypatch.setattr(async_workers, "_get_shared_executor", lambda: executor)
        task = asyncio.create_task(
            service.begin_provider_attempt(session_operation_context=running.session_operation_context, source="auto_title")
        )
        try:
            await _wait_for_thread_event(submitted)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not blocker.done()
            with engine.connect() as conn:
                assert conn.execute(select(quota_provider_attempts_table)).all() == []
                assert conn.execute(select(token_usage_ledger_table)).all() == []
        finally:
            release.set()
        blocker.result(timeout=5)
    with engine.connect() as conn:
        assert conn.execute(select(quota_provider_attempts_table)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed"])
async def test_each_status_bundle_constraints_are_unmasked_and_null_closed(operation_store, status):
    engine, _repository, authority, service, sid = operation_store
    if status in ("queued", "failed"):
        record, _fresh = admit(authority, sid)
        if status == "failed":
            authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    else:
        record, running = start(operation_store)
        if status == "completed":
            await service.add_message_with_transcript(
                sid,
                "user",
                "Build",
                operation_id=UUID(record.operation_id),
                requested_state_id=None,
                writer_principal="route_user_message",
                session_operation_context=running.session_operation_context,
                running=running,
            )
            await service.complete_composer_async_operation(
                running,
                assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
                assistant_record=None,
                audit_cohort=(),
                audit_composition_state_id=None,
                build_response=response,
            )
    with engine.begin() as conn:
        sql = "DROP TRIGGER trg_composer_async_operations_transition_guard"
        if conn.dialect.name == "postgresql":
            sql += " ON composer_async_operations"
        conn.exec_driver_sql(sql)
        now = database_now(conn)
        assert conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(status=status)).rowcount == 1
        if status == "queued":
            holes = (
                {"request_json": None},
                {"claim_token": "partial"},
                {"started_at": now},
                {"session_operation_id": "partial"},
                {"result_json": "{}"},
                {"settled_at": now},
                {"failure_code": "worker_lost"},
            )
        elif status == "running":
            holes = (
                {"request_json": None},
                {"claim_token": None},
                {"claim_owner_instance_id": None},
                {"started_at": None},
                {"session_operation_id": None},
                {"session_operation_lease_token": None},
                {"session_operation_epoch": None},
                {"claim_expires_at": now},
                {"result_json": "{}"},
                {"attempt": 0},
            )
        elif status == "completed":
            holes = (
                {"request_json": "{}"},
                {"claim_token": "partial"},
                {"claim_expires_at": now},
                {"claim_owner_instance_id": None},
                {"started_at": None},
                {"result_json": None},
                {"result_sha256": None},
                {"settled_at": None},
                {"settled_by": None},
                {"result_schema": None},
                {"user_message_id": None},
                {"cancel_requested_at": now},
                {"failure_code": "worker_lost"},
                {"session_operation_id": None},
                {"session_operation_lease_token": None},
                {"session_operation_epoch": None},
            )
        else:
            holes = (
                {"request_json": "{}"},
                {"claim_token": "partial"},
                {"claim_expires_at": now},
                {"result_json": None},
                {"result_sha256": None},
                {"settled_at": None},
                {"failure_code": None},
                {"settled_by": None},
                {"result_schema": None},
                {"session_operation_id": "partial"},
                {"session_operation_lease_token": "partial"},
                {"session_operation_epoch": 1},
                {"started_at": now},
            )
        for values in holes:
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(**values))
        assert conn.execute(select(jobs.c.status).where(jobs.c.operation_id == record.operation_id)).scalar_one() == status


@pytest.mark.parametrize("recovery", ["compose", "foreign_kind", "foreign_owner"])
def test_fresh_recovery_requires_persisted_compose_and_owned_instance(operation_store, recovery):
    from elspeth.contracts.session_operation import SessionOperationContext

    _engine, repository, authority, _service, sid = operation_store
    record, running = start(operation_store)
    repository.release(running.session_operation_context)
    kind = SessionOperationKind.PROPOSAL if recovery == "foreign_kind" else SessionOperationKind.COMPOSE
    fresh = repository.acquire(session_id=sid, operation_kind=kind, owner_instance_id="test-owner", lease_seconds=30)
    assert fresh is not None
    context = SessionOperationContext(fence=fresh.fence, operation_kind=SessionOperationKind.COMPOSE)
    if recovery == "foreign_owner":
        authority = ComposerAsyncOperationAuthority(_engine, owner_instance_id="foreign-owner", claim_lease_seconds=30)
    try:
        if recovery == "compose":
            terminal = authority.settle_lost(
                session_operation_context=context,
                session_id=sid,
                operation_id=record.operation_id,
                failure=failed(),
                authoritative_failure=True,
            )
            assert terminal.status == "failed" and terminal.settled_by == "settle_lost"
        else:
            with pytest.raises(ComposerOperationFenceLost):
                authority.settle_lost(
                    session_operation_context=context,
                    session_id=sid,
                    operation_id=record.operation_id,
                    failure=failed(),
                    authoritative_failure=True,
                )
            assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
    finally:
        repository.release(fresh)


@pytest.mark.asyncio
async def test_expired_job_refuses_business_write_at_database_clock(operation_store, monkeypatch):
    from datetime import timedelta

    from elspeth.web.coordination import composer_operation_authority as job_authority
    from elspeth.web.sessions.composer_operations import ComposerTurnDeadlineExpired

    engine, repository, _authority, service, sid = operation_store
    # Setup must not race a one-second wall-clock budget under coverage.
    # The persisted deadline is crossed explicitly below.
    record, running = start(operation_store)
    await service.update_session_title(sid, "Before deadline", session_operation_context=running.session_operation_context)
    with engine.connect() as conn:
        deadline = conn.execute(select(jobs.c.deadline_at).where(jobs.c.operation_id == record.operation_id)).scalar_one()
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(sid))).scalar_one() == "Before deadline"
    from elspeth.web.sessions.time_normalization import restore_utc

    monkeypatch.setattr(job_authority, "database_now", lambda conn: restore_utc(deadline) + timedelta(microseconds=1))
    with pytest.raises(ComposerTurnDeadlineExpired) as refused:
        await service.update_session_title(sid, "after deadline", session_operation_context=running.session_operation_context)
    assert refused.value.operation_id == record.operation_id
    assert refused.value.session_id == sid
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(sid))).scalar_one() == "Before deadline"
    # Sealed failure remains available under the exact live fence.
    failure = ComposerOperationError(
        http_status=504,
        failure_code="deadline_expired",
        error_type="deadline_expired",
        body={"error_type": "deadline_expired", "detail": "The composer deadline expired."},
        diagnostic_id=None,
    )
    terminal = await service.fail_composer_async_operation(running, failure=failure)
    assert terminal.status == "failed"
    assert terminal.result_json == canonical_json(failure.model_dump(mode="json"))
    repository.release(running.session_operation_context)


def test_continuation_chunk_collision_preserves_original_bytes(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import put_chunk, read_chunk

    engine, _repository, authority, _service, sid = operation_store
    record, _ = admit(authority, sid)
    with engine.begin() as conn:
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key="native-response",
            payload=b'{"value":"exact"}',
            codec="json.v2",
        )
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response") == b'{"value":"exact"}'
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key="native-response",
            payload=b'{"value":"exact"}',
            codec="json.v2",
        )
        with pytest.raises(AuditIntegrityError, match="immutable chunk collision"):
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=record.operation_id,
                key="native-response",
                payload=b'{"value":"other"}',
                codec="json.v2",
            )
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response") == b'{"value":"exact"}'


def test_continuation_chunk_digest_corruption_is_refused(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import put_chunk, read_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    engine, _repository, authority, _service, sid = operation_store
    record, _ = admit(authority, sid)
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response", payload=b"one", codec="json.v2")
        conn.execute(update(chunks).where(chunks.c.operation_id == record.operation_id).values(payload=b"two"))
        with pytest.raises(AuditIntegrityError, match="chunk digest"):
            read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response")


def test_continuation_chunk_codec_is_immutable(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import put_chunk, read_chunk

    engine, _repository, authority, _service, sid = operation_store
    record, _ = admit(authority, sid)
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="codec", payload=b"exact", codec="json.v2")
        with pytest.raises(AuditIntegrityError, match="immutable chunk collision"):
            put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="codec", payload=b"exact", codec="foreign.v3")
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="codec") == b"exact"


@pytest.mark.asyncio
async def test_v1_terminal_settlement_clears_claim_token_and_preserves_attribution(operation_store):
    from hashlib import sha256

    engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    try:
        with engine.connect() as connection:
            before = connection.execute(select(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)).one()
        assert before.lifecycle_version == 1 and before.status == "running" and before.claim_token is not None
        failure = failed()
        terminal = await service.fail_composer_async_operation(running, failure=failure)
        with engine.connect() as connection:
            after = connection.execute(select(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)).one()
        assert after.status == "failed" and after.claim_token is None
        for field in (
            "claim_owner_instance_id",
            "session_operation_id",
            "session_operation_lease_token",
            "session_operation_epoch",
            "attempt",
            "started_at",
        ):
            assert after._mapping[field] == before._mapping[field]
        expected_json = canonical_json(failure.model_dump(mode="json"))
        assert terminal.result_json == after.result_json == expected_json
        assert terminal.result_sha256 == after.result_sha256 == sha256(expected_json.encode()).hexdigest()
        assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "claim_owner_instance_id",
        "session_operation_id",
        "session_operation_lease_token",
        "session_operation_epoch",
        "attempt",
        "started_at",
    ],
)
async def test_v1_terminal_settlement_refuses_attribution_substitution(operation_store, field):
    from datetime import timedelta
    from hashlib import sha256

    engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    try:
        with engine.connect() as connection:
            before = connection.execute(select(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)).one()
        assert before.lifecycle_version == 1 and before.status == "running"
        failure = failed()
        encoded = canonical_json(failure.model_dump(mode="json"))
        values = {
            "status": "failed",
            "request_json": None,
            "claim_token": None,
            "claim_expires_at": None,
            "failure_code": failure.failure_code,
            "settled_by": "owner_terminal",
            "result_schema": "composer_operation_error.v1",
            "result_json": encoded,
            "result_sha256": sha256(encoded.encode()).hexdigest(),
        }
        if field == "started_at":
            values[field] = before.started_at + timedelta(microseconds=1)
        elif field in ("attempt", "session_operation_epoch"):
            values[field] = before._mapping[field] + 1
        else:
            values[field] = "forged-terminal-attribution"
        with pytest.raises(IntegrityError, match="composer operation immutable transition"), engine.begin() as connection:
            now = database_now(connection)
            if field == "started_at":
                # Keep independent timestamp CHECKs valid, exposing the guard.
                now += timedelta(seconds=1)
            connection.execute(
                update(jobs)
                .where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)
                .values(**values, updated_at=now, settled_at=now)
            )
        with engine.connect() as connection:
            after = connection.execute(select(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)).one()
        assert after == before
        # The unchanged real owner can still settle normally after the refusal.
        terminal = await service.fail_composer_async_operation(running, failure=failure)
        assert terminal.status == "failed" and terminal.result_json == encoded
        assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("replacement", [None, "forged-running-claim"])
def test_v1_running_claim_token_change_is_refused_without_row_changes(operation_store, replacement):
    engine, repository, _authority, _service, sid = operation_store
    record, running = start(operation_store)
    try:
        with engine.connect() as connection:
            before = connection.execute(select(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)).one()
        assert before.lifecycle_version == 1 and before.status == "running"
        with pytest.raises(IntegrityError, match="composer operation immutable transition"), engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)
                .values(claim_token=replacement)
            )
        with engine.connect() as connection:
            after = connection.execute(select(jobs).where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)).one()
        assert after == before
    finally:
        repository.release(running.session_operation_context)
