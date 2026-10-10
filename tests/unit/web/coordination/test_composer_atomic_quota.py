"""Job/quota commit coupling at the real durable admission boundary."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.coordination import test_composer_operation_authority as operation_authority_tests
from tests.unit.web.coordination.test_composer_operation_authority import admit

from elspeth.web.coordination.composer_operation_authority import admit_composer_operation
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, ComposerQuotaExceeded, RepositoryRateLimitAuthority
from elspeth.web.middleware.rate_limit import SharedRateLimiter
from elspeth.web.sessions.composer_operations import (
    ComposerOperationActiveError,
    ComposerOperationCapacityError,
    ComposerOperationConflictError,
    ComposerOperationPreconditionRefused,
    composer_operation_request_hash,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import rate_limit_events_table
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest

_KEY = b"atomic-composer-quota-test-key-32"
operation_store = operation_authority_tests.operation_store


def quota(engine, limit=1):
    return ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=_KEY), limit)


def counts(engine):
    with engine.connect() as conn:
        return (
            conn.execute(select(func.count()).select_from(jobs)).scalar_one(),
            conn.execute(select(func.count()).select_from(rate_limit_events_table)).scalar_one(),
        )


def another_session(repository, *, actor="alice"):
    return repository.create_session_with_initial_fence(
        user_id=actor, title="Quota sibling", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    ).id


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize("kind", ["message", "recompose"])
def test_overlapping_same_id_commits_one_job_and_one_charge(operation_store, monkeypatch, limit, kind):
    engine, _repo, authority, _service, sid = operation_store
    request = (
        SendMessageRequest(operation_id=str(uuid4()), content="One immutable action")
        if kind == "message"
        else RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=uuid4())
    )
    admission = quota(engine, limit)
    entered, release, contender = threading.Event(), threading.Event(), threading.Event()
    actual = RepositoryRateLimitAuthority.admit_on_connection

    def hold(self, conn, **kwargs):
        decision = actual(self, conn, **kwargs)
        assert conn.in_transaction()
        assert conn.execute(select(jobs.c.operation_id)).scalar_one() == request.operation_id
        entered.set()
        assert release.wait(5), "winner quota transaction was not released"
        return decision

    def compete():
        contender.set()
        return admit(authority, sid, request, quota=admission)

    monkeypatch.setattr(RepositoryRateLimitAuthority, "admit_on_connection", hold)
    with ThreadPoolExecutor(max_workers=2) as workers:
        winner = workers.submit(admit, authority, sid, request, quota=admission)
        try:
            assert entered.wait(5)
            duplicate = workers.submit(compete)
            assert contender.wait(5)
            assert counts(engine) == (0, 0)
            assert authority.claim_next(limit=1) == ()
        finally:
            release.set()
        results = (winner.result(timeout=5), duplicate.result(timeout=5))
    assert sorted(fresh for _row, fresh in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert counts(engine) == (1, 1)


def test_duplicate_leaves_second_slot_for_distinct_session(operation_store):
    engine, repository, authority, _service, sid = operation_store
    admission = quota(engine, 2)
    request = SendMessageRequest(operation_id=str(uuid4()), content="First")
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: admit(authority, sid, request, quota=admission), range(2)))
    assert sum(fresh for _row, fresh in results) == 1
    sibling = another_session(repository)
    admit(authority, sibling, quota=admission)
    rejected = another_session(repository)
    with pytest.raises(ComposerQuotaExceeded):
        admit(authority, rejected, quota=admission)
    assert counts(engine) == (2, 2)
    assert {claim.session_id for claim in authority.claim_next(limit=10)} == {sid, sibling}


def test_cross_session_budget_denial_rolls_back_losing_job(operation_store):
    engine, repository, authority, _service, sid = operation_store
    sibling = another_session(repository)
    admission = quota(engine)
    barrier = threading.Barrier(2)

    def submit(target):
        barrier.wait(timeout=5)
        try:
            return admit(authority, target, quota=admission)[0].session_id
        except ComposerQuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(submit, (sid, sibling)))
    accepted = [target for target in outcomes if target is not None]
    assert len(accepted) == 1
    assert counts(engine) == (1, 1)
    assert {claim.session_id for claim in authority.claim_next(limit=10)} == set(accepted)


def test_quota_sql_fault_rolls_back_both_effects_and_retry(operation_store, monkeypatch):
    engine, _repo, authority, _service, sid = operation_store
    admission = quota(engine)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Rollback")
    actual = RepositoryRateLimitAuthority.admit_on_connection
    original = OperationalError("private quota SQL", {}, RuntimeError("private-subject"))

    def fail_after_charge(self, conn, **kwargs):
        decision = actual(self, conn, **kwargs)
        assert decision.allowed
        assert conn.execute(select(func.count()).select_from(rate_limit_events_table)).scalar_one() == 1
        raise original

    with monkeypatch.context() as patch:
        patch.setattr(RepositoryRateLimitAuthority, "admit_on_connection", fail_after_charge)
        with pytest.raises(OperationalError) as caught:
            admit(authority, sid, request, quota=admission)
        assert caught.value is original
    assert counts(engine) == (0, 0)
    assert authority.claim_next(limit=1) == ()
    assert admit(authority, sid, request, quota=admission)[1]
    assert counts(engine) == (1, 1)


def test_full_bucket_replay_and_changed_binding_never_charge(operation_store):
    engine, _repo, authority, _service, sid = operation_store
    admission = quota(engine)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Original")
    row, _fresh = admit(authority, sid, request, quota=admission)
    assert admit(authority, sid, request, quota=admission) == (row, False)
    with pytest.raises(ComposerOperationConflictError):
        admit(authority, sid, request.model_copy(update={"content": "Changed"}), quota=admission)
    assert counts(engine) == (1, 1)


def test_quota_connection_must_be_exact_selected_engine(operation_store, tmp_path):
    engine, _repo, authority, _service, sid = operation_store
    foreign = create_session_engine(f"sqlite:///{tmp_path / 'foreign.db'}")
    try:
        with pytest.raises(ValueError, match="same sessions engine"):
            admit(authority, sid, quota=quota(foreign))
        assert counts(engine) == (0, 0)
    finally:
        foreign.dispose()


def test_received_connection_rejects_inactive_closed_and_foreign_transactions(operation_store, tmp_path):
    engine, _repo, _authority, _service, _sid = operation_store
    admission = quota(engine)
    with engine.connect() as conn:
        assert not conn.in_transaction()
        with pytest.raises(ValueError, match="active transaction"):
            admission.check_on_connection(conn, "alice")
    with pytest.raises(ValueError, match="active transaction"):
        admission.check_on_connection(conn, "alice")
    foreign = create_session_engine(f"sqlite:///{tmp_path / 'foreign-connection.db'}")
    try:
        with foreign.begin() as conn, pytest.raises(ValueError, match="same sessions engine"):
            admission.check_on_connection(conn, "alice")
    finally:
        foreign.dispose()
    assert counts(engine) == (0, 0)


def test_received_connection_writer_cannot_open_or_commit_another_transaction(operation_store, monkeypatch):
    engine, _repo, _authority, _service, _sid = operation_store
    admission = quota(engine)
    commits = []

    def committed(conn):
        commits.append(conn)

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("quota opened a second connection under the caller transaction")

    event.listen(engine, "commit", committed)
    try:
        with pytest.raises(RuntimeError, match="caller rollback"), engine.begin() as conn, monkeypatch.context() as patch:
            patch.setattr(engine, "connect", forbidden_connect)
            admission.check_on_connection(conn, "alice")
            assert conn.in_transaction()
            assert commits == []
            raise RuntimeError("caller rollback")
        assert commits == []
        assert counts(engine) == (0, 0)
    finally:
        event.remove(engine, "commit", committed)


def test_commit_ack_loss_preserves_job_charge_pair_and_exact_retry(operation_store, monkeypatch):
    engine, _repo, authority, _service, sid = operation_store
    admission = quota(engine)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Uncertain acknowledgement")
    actual = engine.dialect.do_commit
    original = OperationalError("commit acknowledgement", {}, RuntimeError("private driver"))
    armed = False

    def arm(conn, cursor, statement, parameters, context, executemany):
        nonlocal armed
        if context.isinsert and context.compiled.statement.table is rate_limit_events_table:
            armed = True

    def committed_then_raised(connection):
        nonlocal armed
        actual(connection)
        if armed:
            armed = False
            raise original

    event.listen(engine, "after_cursor_execute", arm)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(engine.dialect, "do_commit", committed_then_raised)
            with pytest.raises(OperationalError) as caught:
                admit(authority, sid, request, quota=admission)
            assert caught.value is original
    finally:
        event.remove(engine, "after_cursor_execute", arm)
    assert counts(engine) == (1, 1)
    assert not admit(authority, sid, request, quota=admission)[1]
    assert counts(engine) == (1, 1)


@pytest.mark.asyncio
async def test_cancelled_awaiter_leaves_worker_owned_atomic_pair(operation_store, monkeypatch):
    engine, _repo, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Cancelled HTTP awaiter")
    admission = quota(engine)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    actual = RepositoryRateLimitAuthority.admit_on_connection

    def hold(self, conn, **kwargs):
        decision = actual(self, conn, **kwargs)
        entered.set()
        assert release.wait(5)
        return decision

    actual_admit = type(authority).admit

    def observe_completion(self, **kwargs):
        try:
            return actual_admit(self, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(RepositoryRateLimitAuthority, "admit_on_connection", hold)
    monkeypatch.setattr(type(authority), "admit", observe_completion)
    task = asyncio.create_task(
        admit_composer_operation(
            authority,
            quota=admission,
            session_id=sid,
            operation_id=request.operation_id,
            kind="compose_message",
            request_hash=composer_operation_request_hash(session_id=sid, kind="compose_message", request=request),
            actor_user_id="alice",
            request_id="cancelled",
            base_state_id=None,
            request_json=request.model_dump_json(),
            deadline_seconds=180,
            max_nonterminal=64,
            auth_provider_type="local",
        )
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(5):
            while not exited.is_set() or counts(engine) != (1, 1):
                await asyncio.sleep(0.001)
        assert not admit(authority, sid, request, quota=admission)[1]
        assert counts(engine) == (1, 1)
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_standalone_composer_consumer_and_operation_share_one_bucket(operation_store):
    engine, _repo, authority, _service, sid = operation_store
    limiter = SharedRateLimiter(1, authority=RepositoryRateLimitAuthority(engine, signing_key=_KEY), scope="composer")
    await limiter.check("alice")
    with pytest.raises(ComposerQuotaExceeded):
        admit(authority, sid, quota=limiter.composer_admission)
    assert counts(engine) == (0, 1)
    with pytest.raises(HTTPException) as caught:
        await limiter.check("alice")
    assert caught.value.status_code == 429
    assert 1 <= int(caught.value.headers["Retry-After"]) <= 60


def test_nonquota_refusals_preserve_budget(operation_store):
    engine, repository, authority, _service, sid = operation_store
    admission = quota(engine, 2)
    missing = SendMessageRequest(operation_id=str(uuid4()), content="Missing", state_id=uuid4())
    with pytest.raises(ComposerOperationPreconditionRefused):
        admit(authority, sid, missing, quota=admission)
    with pytest.raises(ComposerOperationPreconditionRefused):
        admit(authority, sid, actor="another-owner", quota=admission)
    assert counts(engine) == (0, 0)
    admit(authority, sid, quota=admission)
    with pytest.raises(ComposerOperationActiveError):
        admit(authority, sid, quota=admission)
    sibling = another_session(repository)
    with pytest.raises(ComposerOperationCapacityError):
        admit(authority, sibling, maximum=1, quota=admission)
    assert counts(engine) == (1, 1)
    assert admit(authority, sibling, quota=admission)[1]
    assert counts(engine) == (2, 2)


def test_distinct_subjects_have_independent_composer_budgets(operation_store):
    engine, repository, authority, _service, sid = operation_store
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="bob")
    bob_sid = another_session(repository, actor="bob")
    admission = quota(engine)
    assert admit(authority, sid, quota=admission)[1]
    assert admit(authority, bob_sid, actor="bob", quota=admission)[1]
    assert counts(engine) == (2, 2)


def test_reopened_engine_preserves_budget_and_replays_existing_action(operation_store):
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    engine, repository, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Persist this budget")
    row, _fresh = admit(authority, sid, request, quota=quota(engine))
    sibling = another_session(repository)
    reopened = create_session_engine(engine.url.render_as_string(hide_password=False))
    try:
        successor = ComposerAsyncOperationAuthority(reopened, owner_instance_id="test-owner", claim_lease_seconds=30)
        admission = quota(reopened)
        assert admit(successor, sid, request, quota=admission) == (row, False)
        with pytest.raises(ComposerQuotaExceeded):
            admit(successor, sibling, quota=admission)
        assert counts(reopened) == (1, 1)
    finally:
        reopened.dispose()


def _process_submit(database_url, session_id, operation_id, limit):
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    engine = create_session_engine(database_url)
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    try:
        request = SendMessageRequest(operation_id=operation_id, content="Independent engines")
        try:
            row, fresh = admit(authority, UUID(session_id), request, quota=quota(engine, limit))
        except ComposerQuotaExceeded:
            return None, False
        return row.operation_id, fresh
    finally:
        engine.dispose()


@pytest.mark.parametrize("same_id", [True, False])
def test_independent_processes_share_composer_sql_budget(operation_store, same_id):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    engine, repository, _authority, _service, sid = operation_store
    first_id = str(uuid4())
    other_sid = sid if same_id else another_session(repository)
    other_id = first_id if same_id else str(uuid4())
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(
            pool.map(
                _process_submit,
                [engine.url.render_as_string(hide_password=False)] * 2,
                [str(sid), str(other_sid)],
                [first_id, other_id],
                [1, 1],
            )
        )
    assert sum(fresh for _op, fresh in results) == 1
    assert sum(op is not None for op, _fresh in results) == (2 if same_id else 1)
    assert counts(engine) == (1, 1)
