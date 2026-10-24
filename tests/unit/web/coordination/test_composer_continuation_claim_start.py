from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination.test_composer_operation_authority import (
    _admission_rows,
    _admission_v2_authority,
    admit,
    failed,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    operation_store as _operation_store,
)

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_clock import size_segment
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationPreconditionRefused,
)
from elspeth.web.sessions.continuation import load_snapshot, require_continuation_mutation
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.time_normalization import restore_utc

operation_store = _operation_store


def queued(store, *, policy=None):
    authority = _admission_v2_authority(store, policy=policy)
    record, fresh = admit(authority, store[4], lifecycle_version=2, continuation_enabled=True)
    assert fresh
    return authority, record


def start_segment(repository, claim, **changes):
    args = {"owner_instance_id": "test-owner", "lease_seconds": 30, "auth_provider_type": "local"}
    args.update(changes)
    return repository.start_composer_segment(claim, **args)


def test_fresh_start_has_exact_history_snapshot_and_independent_clocks(operation_store, monkeypatch):
    from elspeth.web.coordination import composer_operation_authority as admission_module

    engine, repository, _, _, sid = operation_store
    policy = minimal_continuation_policy()
    real_clock = admission_module.database_now
    with monkeypatch.context() as old_admission_clock:
        old_admission_clock.setattr(admission_module, "database_now", lambda conn: real_clock(conn) - timedelta(days=2))
        authority, record = queued(operation_store, policy=policy)
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        oldest = database_now(conn) - timedelta(days=2)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        history = conn.execute(select(attempts)).one()
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == snapshot
        assert require_continuation_mutation(conn, context) is True
    assert parent.deadline_at is None and parent.attempt == history.claim_attempt == claim.attempt
    assert parent.current_segment_sequence == segment.sequence == history.sequence == 1
    assert parent.ownership_generation == segment.generation == history.generation == 1
    assert parent.session_operation_id == segment.session_operation_id == history.session_operation_id == context.fence.operation_id
    assert history.session_operation_lease_token == context.fence.lease_token
    assert history.session_operation_epoch == context.fence.operation_epoch
    assert history.owner_instance_id == segment.claim_owner_instance_id == "test-owner"
    assert (restore_utc(segment.deadline_at) - restore_utc(segment.started_at)).total_seconds() == size_segment(
        policy.actions.session_sql, policy.segment
    )
    assert (restore_utc(segment.cooperative_at) - restore_utc(segment.started_at)).total_seconds() == min(
        policy.segment.quantum, size_segment(policy.actions.session_sql, policy.segment) - policy.segment.watermark
    )
    assert restore_utc(segment.started_at) > oldest + timedelta(days=1)


@pytest.mark.parametrize("field", ["attempt", "claim_token", "segment_sequence", "ownership_generation", "session_id"])
def test_stale_claim_refuses_without_any_row_or_fence_change(operation_store, field):
    engine, repository, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    changes = {
        "attempt": claim.attempt + 1,
        "claim_token": "other-token",
        "segment_sequence": claim.segment_sequence + 1,
        "ownership_generation": claim.ownership_generation + 1,
        "session_id": uuid4(),
    }
    bad = replace(claim, **{field: changes[field]})
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        start_segment(repository, bad)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("change", ["owner", "provider", "archived", "transferred", "expired", "policy"])
def test_start_refuses_current_custody_or_policy_substitution(operation_store, change):
    engine, repository, _, _, sid = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    args = {}
    if change in ("archived", "transferred", "expired"):
        with engine.begin() as conn:
            now = database_now(conn)
            if change == "archived":
                conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=now))
            elif change == "transferred":
                from tests.fixtures.identities import ensure_test_identity

                ensure_test_identity(conn, identity_id="bob")
                conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(user_id="bob"))
            else:
                conn.execute(update(segments).values(claim_expires_at=now - timedelta(seconds=1)))
    elif change == "owner":
        args["owner_instance_id"] = "other-owner"
    elif change == "provider":
        args["auth_provider_type"] = "entra"
    else:
        args["segment_policy"] = minimal_continuation_policy().segment.model_copy(update={"nominal": 301.0})
    before = _admission_rows(engine)
    expected = (
        AuditIntegrityError
        if change == "policy"
        else ComposerOperationPreconditionRefused
        if change in ("provider", "archived", "transferred")
        else ComposerOperationFenceLost
    )
    with pytest.raises(expected):
        start_segment(repository, claim, **args)
    assert _admission_rows(engine) == before


def test_expired_unstarted_claim_is_replaced_but_old_claim_cannot_start(operation_store):
    engine, repository, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    (old,) = authority.claim_segment(limit=1)
    assert authority.claim_segment(limit=1) == ()
    with engine.begin() as conn:
        conn.execute(update(segments).values(claim_expires_at=database_now(conn) - timedelta(seconds=1)))
    (new,) = authority.claim_segment(limit=1)
    assert new.attempt == old.attempt + 1 and new.claim_token != old.claim_token
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        start_segment(repository, old)
    assert _admission_rows(engine) == before
    assert start_segment(repository, new).fence.session_id == str(new.session_id)


@pytest.mark.parametrize(
    "changed", ["logical_hash", "logical_kind", "logical_base", "logical_original", "action", "policy_pointer", "checkpoint"]
)
def test_valid_shaped_snapshot_and_segment_substitutions_are_refused(operation_store, changed):
    engine, repository, _, _, sid = operation_store
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    with engine.begin() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        if changed.startswith("logical_"):
            root = decode_continuation_snapshot(stored.payload_json)
            replacement_id = str(uuid4())
            values = {
                "logical_hash": {"request_hash": "b" * 64},
                "logical_kind": {"kind": "compose_recompose"},
                "logical_base": {"base_state_id": replacement_id},
                "logical_original": {"original_user_message_id": replacement_id},
            }
            bad = root.model_copy(
                update={
                    "logical": root.logical.model_copy(update=values[changed]),
                    "mutation_ceiling": root.mutation_ceiling.model_copy(update={"request_id": replacement_id})
                    if changed == "logical_original"
                    else root.mutation_ceiling,
                }
            )
            encoded = encode_continuation_snapshot(bad).encode("utf-8")
            assert decode_continuation_snapshot(encoded.decode("utf-8")) == bad
            conn.execute(
                update(chunks)
                .where(chunks.c.chunk_key == f"snapshot:{stored.checkpoint_id}")
                .values(payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest())
            )
        elif changed == "action":
            conn.execute(update(segments).values(next_action_json=minimal_continuation_policy().actions.primary.model_dump_json()))
        elif changed == "policy_pointer":
            conn.execute(update(segments).values(policy_sha256="b" * 64))
        else:
            conn.execute(update(segments).values(checkpoint_id=str(uuid4())))
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError):
        start_segment(repository, claim)
    assert _admission_rows(engine) == before


def test_cancelled_v2_can_start_cleanup_but_business_guard_still_refuses(operation_store):
    engine, repository, _, _, sid = operation_store
    authority, record = queued(operation_store)
    with engine.begin() as conn:
        conn.execute(update(jobs).values(cancel_requested_at=database_now(conn)))
    assert authority.claim_next(limit=1) == () and authority.list_expired_queued(limit=1) == ()
    failure = ComposerOperationError(
        http_status=500, failure_code="worker_lost", error_type=None, body={"detail": "controlled legacy settlement"}, diagnostic_id=None
    )
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        authority.settle_unstarted(None, session_id=sid, operation_id=record.operation_id, failure=failure)
    assert _admission_rows(engine) == before
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    with engine.begin() as conn:
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            require_continuation_mutation(conn, context)
        assert require_continuation_mutation(conn, context, allow_cancel=True) is True
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        authority.settle_unstarted(None, session_id=sid, operation_id=record.operation_id, failure=failure)
    with pytest.raises(ComposerOperationFenceLost):
        repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    assert authority.list_expired_running(limit=1) == ()
    assert _admission_rows(engine) == before
    repository.release(context)
    assert authority.list_expired_running(limit=1) == ()
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        authority._settle_recovery(
            session_id=sid,
            operation_id=record.operation_id,
            failure=failure,
            settled_by="settle_lost_inactive_session",
            recovery_context=None,
            authoritative_failure=False,
        )
    assert _admission_rows(engine) == before


def test_history_insert_failure_rolls_back_actual_fence_and_segment_start(operation_store):
    engine, repository, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    before = _admission_rows(engine)
    injected = RuntimeError("controlled post-history insert failure")

    def fail_after_insert(conn, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.isinsert and context.compiled.statement.table.name == attempts.name:
            raise injected

    event.listen(engine, "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(RuntimeError) as caught:
            start_segment(repository, claim)
        assert caught.value is injected
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_insert)
    assert _admission_rows(engine) == before
    assert start_segment(repository, claim).fence.session_id == str(claim.session_id)


def _claim_from_process(url: str, owner: str, rendezvous):
    from elspeth.web.sessions.engine import create_session_engine

    engine = create_session_engine(url)

    def after_hint(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and compiled.statement.is_select:
            selected = compiled.statement
            if tuple(column.name for column in selected.selected_columns) == (
                "session_id",
                "operation_id",
                "sequence",
            ) and selected.get_final_froms() == [segments]:
                rendezvous.wait(timeout=10)

    event.listen(engine, "after_cursor_execute", after_hint)
    try:
        return ComposerAsyncOperationAuthority(engine, owner_instance_id=owner, claim_lease_seconds=30).claim_segment(limit=1)
    finally:
        event.remove(engine, "after_cursor_execute", after_hint)
        engine.dispose()


def test_independent_processes_cannot_both_claim_one_segment(operation_store):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    engine, _, _, _, _ = operation_store
    queued(operation_store)
    url = engine.url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        rendezvous = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
            results = list(pool.map(_claim_from_process, [url, url], ["test-owner", "peer-owner"], [rendezvous, rendezvous]))
    assert sorted(len(result) for result in results) == [0, 1]
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        assert parent.attempt == 1
        assert segment.claim_token == next(result[0].claim_token for result in results if result)
        assert conn.execute(select(attempts)).all() == []


@pytest.mark.parametrize("changed", ["claimed", "delayed", "archived"])
def test_stale_hint_is_rechecked_under_the_actual_session_lock(operation_store, monkeypatch, changed):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    from elspeth.web.coordination import composer_operation_authority as module

    engine, _, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    reached, resume = Event(), Event()
    actual_lock = module.locked_session_transaction

    @contextmanager
    def pause_after_hint(selected_engine, session_id):
        if not reached.is_set():
            reached.set()
            assert resume.wait(10), "controlled hint was not released"
        with actual_lock(selected_engine, session_id) as conn:
            yield conn

    monkeypatch.setattr(module, "locked_session_transaction", pause_after_hint)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(authority.claim_segment, limit=1)
        try:
            assert reached.wait(10), "actual outer hint never reached the lock seam"
            if changed == "claimed":
                peer = ComposerAsyncOperationAuthority(engine, owner_instance_id="peer-owner", claim_lease_seconds=30)
                assert len(peer.claim_segment(limit=1)) == 1
            elif changed == "delayed":
                with engine.begin() as conn:
                    conn.execute(update(segments).values(next_eligible_at=database_now(conn) + timedelta(minutes=1)))
            else:
                with engine.begin() as conn:
                    conn.execute(update(sessions_table).values(archived_at=database_now(conn)))
            before = _admission_rows(engine)
        finally:
            resume.set()
        assert pending.result(timeout=10) == (), "current locked claim was incorrectly accepted"
    assert _admission_rows(engine) == before


def test_unexpired_oldest_claim_does_not_consume_limit_one(operation_store):
    engine, repository, _, _, sid = operation_store
    authority, first = queued(operation_store)
    (old_claim,) = authority.claim_segment(limit=1)
    other = repository.create_session_with_initial_fence(
        user_id="alice", title="Younger", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    second, fresh = admit(authority, other.id, lifecycle_version=2, continuation_enabled=True)
    assert fresh
    (new_claim,) = authority.claim_segment(limit=1)
    assert old_claim.operation_id == first.operation_id and new_claim.operation_id == second.operation_id
    assert new_claim.session_id == other.id and old_claim.session_id == sid
    with engine.connect() as conn:
        rows = conn.execute(select(segments).order_by(segments.c.operation_id)).all()
    assert {row.operation_id: row.claim_token for row in rows} == {
        first.operation_id: old_claim.claim_token,
        second.operation_id: new_claim.claim_token,
    }


@pytest.mark.parametrize("physical", [500.0, 1e308])
def test_retained_complete_action_sizes_or_atomically_refuses_overflow(operation_store, physical):
    engine, repository, _, _, _ = operation_store
    original = minimal_continuation_policy()
    action = original.actions.session_sql.model_copy(update={"physical": physical})
    policy = original.model_copy(update={"actions": original.actions.model_copy(update={"session_sql": action})})
    authority, _ = queued(operation_store, policy=policy)
    (claim,) = authority.claim_segment(limit=1)
    before = _admission_rows(engine)
    if physical == 1e308:
        with pytest.raises(OverflowError):
            start_segment(repository, claim)
        assert _admission_rows(engine) == before
    else:
        start_segment(repository, claim)
        with engine.connect() as conn:
            row = conn.execute(select(segments)).one()
        assert (restore_utc(row.deadline_at) - restore_utc(row.started_at)).total_seconds() == size_segment(action, policy.segment)
        assert size_segment(action, policy.segment) > policy.segment.nominal


def test_expired_physical_lease_preserves_distributed_old_owner_liveness(operation_store):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.sessions.models import web_instances_table

    engine, repository, _, _, sid = operation_store
    old = repository.acquire(session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30)
    queued(operation_store)
    claimant = ComposerAsyncOperationAuthority(engine, owner_instance_id="peer-owner", claim_lease_seconds=30)
    if engine.dialect.name == "postgresql":
        from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance

        _register_instance(engine, instance_id="peer-owner", lease_delta=timedelta(minutes=10))
    before = _admission_rows(engine)
    assert claimant.claim_segment(limit=1) == (), "live physical owner was incorrectly eligible"
    assert _admission_rows(engine) == before
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=now - timedelta(seconds=1)))
    before = _admission_rows(engine)
    if engine.dialect.name == "postgresql":
        assert claimant.claim_segment(limit=1) == (), "live registered old owner was incorrectly eligible"
        assert _admission_rows(engine) == before
        with engine.begin() as conn:
            changed = conn.execute(
                update(web_instances_table)
                .where(web_instances_table.c.instance_id == "test-owner")
                .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
            )
            assert changed.rowcount == 1
    (claim,) = claimant.claim_segment(limit=1)
    current = start_segment(repository, claim, owner_instance_id="peer-owner")
    assert current.fence.operation_epoch == old.fence.operation_epoch + 1
    assert current.fence.operation_id != old.fence.operation_id
    repository.release(current)


@pytest.mark.asyncio
async def test_recompose_start_retains_actual_original_ingress_without_inserting_again(operation_store):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.sessions.models import chat_messages_table
    from elspeth.web.sessions.schemas import RecomposeRequest

    engine, repository, _, service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    original = await service.add_message(sid, "user", "Build", writer_principal="route_user_message", session_operation_context=context)
    repository.release(context)
    authority = _admission_v2_authority(operation_store)
    request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=original.id)
    record, fresh = admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
    assert fresh and record.reserved_user_message_id == record.user_message_id == original.id
    with engine.connect() as conn:
        before = conn.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
    (claim,) = authority.claim_segment(limit=1)
    started = start_segment(repository, claim)
    with engine.connect() as conn:
        selected = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        assert decode_continuation_snapshot(selected.payload_json).logical.original_user_message_id == str(original.id)
        assert conn.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all() == before
        assert require_continuation_mutation(conn, started) is True


def test_logical_reservation_blocks_unrelated_writer_before_first_physical_start(operation_store):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.coordination.repository import SessionOperationConflictError

    engine, repository, _, _, sid = operation_store
    authority, _ = queued(operation_store)
    before = _admission_rows(engine)
    with pytest.raises(SessionOperationConflictError):
        repository.acquire(session_id=sid, operation_kind=SessionOperationKind.ARCHIVE, owner_instance_id="test-owner", lease_seconds=30)
    assert _admission_rows(engine) == before
    (claim,) = authority.claim_segment(limit=1)
    assert start_segment(repository, claim).fence.session_id == str(sid)


def test_first_start_requires_exact_complete_admitted_initializer(operation_store):
    engine, repository, _, _, sid = operation_store
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    with engine.begin() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        root = decode_continuation_snapshot(stored.payload_json)
        assert root.frames[0].ingress_phase == "reserved"
        assert root.frames[0].title_attempt_id is None
        changed = root.model_copy(update={"frames": (root.frames[0].model_copy(update={"title_attempt_id": str(uuid4())}),)})
        encoded = encode_continuation_snapshot(changed).encode("utf-8")
        assert decode_continuation_snapshot(encoded.decode("utf-8")) == changed
        updated = conn.execute(
            update(chunks)
            .where(
                chunks.c.session_id == str(sid),
                chunks.c.operation_id == record.operation_id,
                chunks.c.chunk_key == f"snapshot:{stored.checkpoint_id}",
            )
            .values(payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest())
        )
        assert updated.rowcount == 1
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError, match="exact admitted complete initializer"):
        start_segment(repository, claim)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("lifecycle", [1, 2])
@pytest.mark.parametrize("owner", ["claim_next", "get_start", "start", "expired_queued", "expired_running", "unstarted", "recovery"])
def test_each_legacy_owner_preserves_v1_and_refuses_actual_v2(operation_store, lifecycle, owner):
    from elspeth.web.coordination.composer_operation_authority import get_composer_operation_for_start_on_connection

    engine, repository, legacy_authority, _, sid = operation_store
    if lifecycle == 2:
        authority, record = queued(operation_store)
    else:
        authority = legacy_authority
        record, fresh = admit(authority, sid)
        assert fresh
    claim = None
    if owner in ("get_start", "start", "expired_running", "recovery"):
        (claim,) = authority.claim_segment(limit=1) if lifecycle == 2 else authority.claim_next(limit=1)
    if owner in ("expired_running", "recovery"):
        context = (
            start_segment(repository, claim)
            if lifecycle == 2
            else repository.start_composer_async_operation(
                claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local"
            )
        )
        repository.release(context)
    if owner == "expired_queued":
        with engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(cancel_requested_at=database_now(conn)))
    if owner == "recovery":
        with engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
    before = _admission_rows(engine)
    if owner in ("claim_next", "expired_queued", "expired_running"):
        if owner == "claim_next":
            selected = authority.claim_next(limit=1)
        elif owner == "expired_queued":
            selected = authority.list_expired_queued(limit=1)
        else:
            selected = authority.list_expired_running(limit=1)
        if lifecycle == 2:
            assert selected == (), "legacy discovery selected V2"
        else:
            assert len(selected) == 1
            assert selected[0].operation_id == record.operation_id
    else:

        def call_owner():
            if owner == "get_start":
                with engine.connect() as conn:
                    return get_composer_operation_for_start_on_connection(conn, claim)
            if owner == "start":
                return repository.start_composer_async_operation(
                    claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local"
                )
            if owner == "unstarted":
                return authority.settle_unstarted(
                    None, session_id=sid, operation_id=record.operation_id, failure=failed(), authoritative_failure=True
                )
            return authority._settle_recovery(
                session_id=sid,
                operation_id=record.operation_id,
                failure=failed(),
                settled_by="settle_lost_inactive_session",
                recovery_context=None,
                authoritative_failure=True,
            )

        if lifecycle == 2:
            with pytest.raises(ComposerOperationFenceLost):
                call_owner()
        else:
            result = call_owner()
            if owner == "start":
                assert result.fence.session_id == str(sid)
                assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
            else:
                assert result.operation_id == record.operation_id
                assert result.status == ("queued" if owner == "get_start" else "failed")
    if lifecycle == 2:
        assert _admission_rows(engine) == before


@pytest.mark.parametrize("guard", ["action", "initializer"])
@pytest.mark.parametrize("positive", [False, True])
def test_exact_selection_omission_has_real_refusal_witness_and_fresh_positive(operation_store, monkeypatch, guard, positive):
    import inspect
    from textwrap import dedent

    from elspeth.web.coordination import repository as repository_module
    from elspeth.web.sessions import continuation as module

    if guard == "action":
        original = module._select_segment_start_on_connection
        fragment = (
            '    if action != expected:\n        raise AuditIntegrityError("segment allowance differs from its selected retained action")\n'
        )
    else:
        original = module._initial_segment_action
        fragment = (
            "    if decode_continuation_snapshot(expected.snapshot_json) != stack:\n"
            '        raise AuditIntegrityError("first segment is not its exact admitted complete initializer")\n'
        )
    source = dedent(inspect.getsource(original))
    assert source.count(fragment) == 1
    namespace = vars(module).copy()
    exec(compile("from __future__ import annotations\n" + source.replace(fragment, ""), "<exact-selection-omission>", "exec"), namespace)
    if guard == "action":
        monkeypatch.setattr(module, "_select_segment_start_on_connection", namespace[original.__name__])
        monkeypatch.setattr(repository_module, "_select_segment_start_on_connection", namespace[original.__name__])
    else:
        monkeypatch.setattr(module, "_initial_segment_action", namespace[original.__name__])
    if positive:
        test_fresh_start_has_exact_history_snapshot_and_independent_clocks(operation_store, monkeypatch)
    else:
        # Native pytest is GREEN only because this precise missing-refusal failure is expected.
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            if guard == "action":
                test_valid_shaped_snapshot_and_segment_substitutions_are_refused(operation_store, "action")
            else:
                test_first_start_requires_exact_complete_admitted_initializer(operation_store)


@pytest.mark.parametrize("changed", ["claimed", "delayed", "archived"])
@pytest.mark.parametrize("positive", [False, True])
def test_claim_predicate_omission_has_actual_locked_hint_witness(operation_store, monkeypatch, changed, positive):
    import inspect
    from textwrap import dedent

    from elspeth.web.coordination import composer_operation_authority as module

    fragments = {
        "claimed": "        or_(segments.c.claim_token.is_(None), segments.c.claim_expires_at <= now),\n",
        "delayed": "        segments.c.next_eligible_at <= now,\n",
        "archived": "        parent,\n",
    }
    fragment = fragments[changed]
    source = dedent(inspect.getsource(module._continuation_claim_predicate))
    assert source.count(fragment) == 1
    namespace = vars(module).copy()
    exec(
        compile("from __future__ import annotations\n" + source.replace(fragment, ""), "<locked-claim-predicate-omission>", "exec"),
        namespace,
    )
    monkeypatch.setattr(module, "_continuation_claim_predicate", namespace["_continuation_claim_predicate"])
    if positive:
        test_fresh_start_has_exact_history_snapshot_and_independent_clocks(operation_store, monkeypatch)
    else:
        with pytest.raises(AssertionError, match="current locked claim was incorrectly accepted"):
            test_stale_hint_is_rechecked_under_the_actual_session_lock(operation_store, monkeypatch, changed)


@pytest.mark.parametrize("positive", [False, True])
def test_logical_reservation_omission_has_actual_writer_witness(operation_store, monkeypatch, positive):
    import inspect
    from textwrap import dedent

    from elspeth.web.coordination import repository as module

    original = module._SessionOperationAuthorityRepository._advance_exclusive_fence_on_connection
    fragment = (
        "    if parent is not None and (operation_kind is not SessionOperationKind.COMPOSE or composer_operation_id != parent):\n"
        "        raise SessionOperationConflictError\n"
    )
    source = dedent(inspect.getsource(original))
    assert source.count(fragment) == 1
    namespace = vars(module).copy()
    exec(
        compile("from __future__ import annotations\n" + source.replace(fragment, ""), "<logical-reservation-omission>", "exec"), namespace
    )
    monkeypatch.setattr(module._SessionOperationAuthorityRepository, "_advance_exclusive_fence_on_connection", namespace[original.__name__])
    if positive:
        test_fresh_start_has_exact_history_snapshot_and_independent_clocks(operation_store, monkeypatch)
    else:
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            test_logical_reservation_blocks_unrelated_writer_before_first_physical_start(operation_store)


@pytest.mark.testcontainer
@pytest.mark.parametrize("positive", [False, True])
def test_registered_old_owner_omission_has_actual_postgres_eligibility_witness(operation_store, monkeypatch, positive):
    import inspect
    from textwrap import dedent

    from elspeth.web.coordination import composer_operation_authority as module

    assert operation_store[0].dialect.name == "postgresql"
    fragment = "                web_instances_table.c.lease_expires_at <= now,\n"
    source = dedent(inspect.getsource(module._continuation_claim_predicate))
    assert source.count(fragment) == 1
    namespace = vars(module).copy()
    exec(
        compile("from __future__ import annotations\n" + source.replace(fragment, ""), "<registered-old-owner-omission>", "exec"), namespace
    )
    monkeypatch.setattr(module, "_continuation_claim_predicate", namespace["_continuation_claim_predicate"])
    if positive:
        test_fresh_start_has_exact_history_snapshot_and_independent_clocks(operation_store, monkeypatch)
    else:
        with pytest.raises(AssertionError, match="live registered old owner was incorrectly eligible"):
            test_expired_physical_lease_preserves_distributed_old_owner_liveness(operation_store)


@pytest.mark.parametrize("lifecycle", [1, 2])
@pytest.mark.parametrize("owner", ["claim_next", "get_start", "start", "expired_queued", "expired_running", "unstarted", "recovery"])
def test_each_legacy_isolation_omission_retains_its_actual_independent_backstops(operation_store, monkeypatch, lifecycle, owner):
    import inspect
    from textwrap import dedent

    from sqlalchemy.exc import IntegrityError

    from elspeth.web.coordination import composer_operation_authority as module
    from elspeth.web.coordination import repository as repository_module

    originals = {
        "claim_next": module.ComposerAsyncOperationAuthority.claim_next,
        "get_start": module.get_composer_operation_for_start_on_connection,
        "start": module.start_composer_operation_on_connection,
        "expired_queued": module.ComposerAsyncOperationAuthority.list_expired_queued,
        "expired_running": module.ComposerAsyncOperationAuthority.list_expired_running,
        "unstarted": module.ComposerAsyncOperationAuthority.settle_unstarted,
        "recovery": module.ComposerAsyncOperationAuthority._settle_recovery,
    }
    original = originals[owner]
    source = dedent(inspect.getsource(original))
    if owner == "claim_next":
        edits = (
            ("\n                jobs.c.lifecycle_version == 1,\n", "\n"),
            (
                '                .where(*_key(sid, candidate.operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "queued")\n',
                '                .where(*_key(sid, candidate.operation_id), jobs.c.status == "queued")\n',
            ),
            ("\n                    jobs.c.lifecycle_version == 1,\n", "\n"),
        )
    elif owner == "get_start":
        edits = (("        or row.lifecycle_version != 1\n        or claim.segment_sequence is not None\n", ""),)
    elif owner == "start":
        edits = (("            jobs.c.lifecycle_version == 1,\n", ""),)
    elif owner in ("expired_queued", "expired_running"):
        edits = (("                jobs.c.lifecycle_version == 1,\n", ""),)
    else:
        early_guard = (
            "        if row is not None and row.lifecycle_version != 1:\n"
            "            raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)\n"
        )
        if owner == "unstarted":
            sql_guard = '        predicates = [*_key(session_id, operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "queued"]\n'
            unguarded_sql = '        predicates = [*_key(session_id, operation_id), jobs.c.status == "queued"]\n'
        else:
            sql_guard = '            .where(*_key(session_id, operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "running")\n'
            unguarded_sql = '            .where(*_key(session_id, operation_id), jobs.c.status == "running")\n'
        edits = ((early_guard, ""), (sql_guard, unguarded_sql))
    for fragment, replacement in edits:
        assert source.count(fragment) == 1
        source = source.replace(fragment, replacement)
    namespace = vars(module).copy()
    exec(compile("from __future__ import annotations\n" + source, "<one-complete-legacy-isolation-omission>", "exec"), namespace)
    if owner == "get_start":
        monkeypatch.setattr(module, "get_composer_operation_for_start_on_connection", namespace[original.__name__])
        monkeypatch.setattr(repository_module, "get_composer_operation_for_start_on_connection", namespace[original.__name__])
    elif owner == "start":
        monkeypatch.setattr(module, "start_composer_operation_on_connection", namespace[original.__name__])
        monkeypatch.setattr(repository_module, "start_composer_operation_on_connection", namespace[original.__name__])
    else:
        monkeypatch.setattr(module.ComposerAsyncOperationAuthority, original.__name__, namespace[original.__name__])
    if lifecycle == 1 or owner in ("claim_next", "get_start", "start"):
        # These omissions survive on valid V2 rows: NULL deadline, parent claim
        # identity, and the earlier old-start guard remain authoritative.
        test_each_legacy_owner_preserves_v1_and_refuses_actual_v2(operation_store, lifecycle, owner)
    elif owner in ("expired_queued", "expired_running"):
        with pytest.raises(AssertionError, match="legacy discovery selected V2"):
            test_each_legacy_owner_preserves_v1_and_refuses_actual_v2(operation_store, lifecycle, owner)
    else:
        # Removing the complete application isolation does not bypass the
        # existing SQL terminal bundle CHECK. This is a DDL backstop, not an
        # application refusal or a claim that an unsafe settlement committed.
        with pytest.raises(IntegrityError, match="ck_composer_operations_status_bundle"):
            test_each_legacy_owner_preserves_v1_and_refuses_actual_v2(operation_store, lifecycle, owner)


@pytest.mark.asyncio
async def test_historical_effect_keeps_creator_and_refuses_forged_locator(operation_store):
    from sqlalchemy import insert

    from elspeth.web.composer.turn_effects import authored_state_hash
    from elspeth.web.sessions.continuation import put_chunk, validate_historical_effect_reference
    from elspeth.web.sessions.converters import state_from_record
    from elspeth.web.sessions.graph_recovery import checkpoint_data, empty_recovery_state
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects

    engine, repository, _authority, service, sid = operation_store
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    saved = await service.save_composition_state(
        sid, checkpoint_data(empty_recovery_state()), provenance="session_seed", session_operation_context=context
    )
    graph_hash = authored_state_hash(state_from_record(saved))
    with engine.begin() as conn:
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key="result:one",
            payload=b'{"success":true}',
            codec="tool-result.v1",
        )
        conn.execute(
            insert(effects).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                effect_key="response:0:tool:0",
                input_sha256="b" * 64,
                owner="session_sql",
                disposition="committed",
                receipt_key=str(saved.id),
                result_chunk_key="result:one",
                creator_sequence=claim.segment_sequence,
                creator_generation=claim.ownership_generation,
                committed_state_ids_json='["' + str(saved.id) + '"]',
                graph_sha256=graph_hash,
            )
        )
        args = {
            "session_id": sid,
            "operation_id": record.operation_id,
            "effect_key": "response:0:tool:0",
            "expected_input_sha256": "b" * 64,
            "expected_creator_operation_id": context.fence.operation_id,
            "expected_receipt_id": str(saved.id),
            "expected_inserted_state_ids": (saved.id,),
            "expected_authored_state_hash": graph_hash,
        }
        first = validate_historical_effect_reference(conn, **args)
        second = validate_historical_effect_reference(conn, **args)
        assert first == second and first.inserted_state_ids == (saved.id,)
        assert first.creator_operation_epoch == context.fence.operation_epoch
        with pytest.raises(AuditIntegrityError, match="creator"):
            validate_historical_effect_reference(conn, **(args | {"expected_creator_operation_id": str(uuid4())}))
        with pytest.raises(AuditIntegrityError, match="committed SQL owner"):
            validate_historical_effect_reference(conn, **(args | {"operation_id": str(uuid4())}))
    repository.release(context)


@pytest.mark.parametrize("changed", ("checkpoint", "cursor"))
def test_snapshot_cas_uses_the_callers_selected_head_without_writes(operation_store, changed):
    from elspeth.web.sessions.continuation import ContinuationCASConflict, require_snapshot_cas

    engine, _, _, _, sid = operation_store
    _, record = queued(operation_store)
    before = _admission_rows(engine)
    with engine.connect() as conn:
        selected = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        args = {
            "session_id": str(sid),
            "operation_id": record.operation_id,
            "expected_checkpoint_id": selected.checkpoint_id,
            "expected_cursor": selected.cursor,
        }
        assert require_snapshot_cas(conn, **args) == selected
        wrong = {"expected_checkpoint_id": str(uuid4())} if changed == "checkpoint" else {"expected_cursor": selected.cursor + 1}
        with pytest.raises(ContinuationCASConflict, match="continuation snapshot changed before effect admission"):
            require_snapshot_cas(conn, **(args | wrong))
        assert require_snapshot_cas(conn, **args) == selected
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("owner", ("primary_turn", "review", "read_tool"))
def test_bound_action_json_keeps_nominal_owner_and_strict_read_arguments(operation_store, owner):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.advisor_output import PrimaryContinuationAction, ReadContinuationAction, ReviewContinuationAction
    from elspeth.web.composer.continuation import ContinuationActionSource
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.sessions.continuation import bound_action_json

    engine, _, _, _, sid = operation_store
    _, record = queued(operation_store)
    with engine.connect() as conn:
        stack = decode_continuation_snapshot(load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id).payload_json)
    common = {
        "obligation_ids": ("request",),
        "evidence_ids": (),
        "input_hash": stack.logical.request_hash,
        "graph_hash": "b" * 64,
        "policy_hash": stack.policy_hash,
        "tool_manifest_hash": "c" * 64,
    }
    if owner == "primary_turn":
        action = PrimaryContinuationAction(kind="primary_turn", instruction="Inspect the request", **common)
    elif owner == "review":
        action = ReviewContinuationAction(kind="review", candidate_ref="candidate:" + "b" * 64, evidence_binding="pure-binding", **common)
    else:
        action = ReadContinuationAction(
            kind="read_tool", tool_name="get_plugin_schema", arguments_json='{"name":"csv","type":"source"}', **common
        )
    # This is an owned pure source value, not a claim that a provider executed.
    response = encode_private_chunk(codec="composer.response.v1", canonical='{"fixture":"pure action value"}')
    source = ContinuationActionSource(effect_key="provider:1:0", input_sha256=stack.logical.request_hash, response=response.reference)
    encoded_action = canonical_json(action.model_dump(mode="json"))
    selected = decode_continuation_snapshot(
        encode_continuation_snapshot(stack.model_copy(update={"active_action_json": encoded_action, "active_action_source": source}))
    )
    args = {"action_owner": owner, "read_tool_name": "get_plugin_schema", "read_tool_arguments_json": '{"type":"source", "name":"csv"}'}
    assert bound_action_json(selected, **args) == encoded_action
    assert bound_action_json(stack, action_owner=None, read_tool_name=None, read_tool_arguments_json=None) is None
    with pytest.raises(AuditIntegrityError, match="no actual retained Advisor action"):
        bound_action_json(stack, **args)
    wrong_owner = "review" if owner == "primary_turn" else "primary_turn"
    with pytest.raises(AuditIntegrityError, match="differs from its actual execution owner"):
        bound_action_json(selected, **(args | {"action_owner": wrong_owner}))
    if owner == "read_tool":
        for wrong in (
            {"read_tool_name": "get_other_schema"},
            {"read_tool_arguments_json": '{"name":"json","type":"source"}'},
            {"read_tool_arguments_json": '{"name":"csv","name":"csv","type":"source"}'},
            {"read_tool_arguments_json": None},
        ):
            with pytest.raises(AuditIntegrityError, match="differs from its actual execution owner"):
                bound_action_json(selected, **(args | wrong))
        malformed_action = action.model_copy(update={"arguments_json": '{"name":"csv","name":"csv"}'})
        malformed = selected.model_copy(update={"active_action_json": canonical_json(malformed_action.model_dump(mode="json"))})
        with pytest.raises(AuditIntegrityError, match="differs from its actual execution owner"):
            bound_action_json(malformed, **args)
    assert bound_action_json(selected, **args) == encoded_action


def test_effect_snapshot_preserves_other_effects_and_settlement_identity(operation_store):
    from elspeth.web.composer.continuation import EffectFrontierEntry
    from elspeth.web.sessions.continuation import _effect_snapshot

    engine, _, _, _, sid = operation_store
    _, record = queued(operation_store)
    with engine.connect() as conn:
        original = decode_continuation_snapshot(load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id).payload_json)
    selected = original.model_copy(
        update={
            "effect_frontier": (
                EffectFrontierEntry(effect_id="selected", disposition="prepared", receipt_id=None),
                EffectFrontierEntry(effect_id="retained", disposition="committed", receipt_id="retained-receipt"),
            ),
            "pending_settlement_ids": ("attempt-remove", "attempt-retain"),
        }
    )
    encoded = encode_continuation_snapshot(selected)
    args = {
        "effect_key": "selected",
        "disposition": "committed",
        "receipt_id": "selected-receipt",
        "remove_attempt_id": "attempt-remove",
        "add_attempt_id": "attempt-add",
    }
    changed = _effect_snapshot(encoded, **args)
    restored = decode_continuation_snapshot(changed)
    assert restored == selected.model_copy(
        update={
            "effect_frontier": (
                selected.effect_frontier[1],
                EffectFrontierEntry(effect_id="selected", disposition="committed", receipt_id="selected-receipt"),
            ),
            "pending_settlement_ids": ("attempt-retain", "attempt-add"),
        }
    )
    assert _effect_snapshot(changed, **args) == changed
    assert decode_continuation_snapshot(encoded) == selected
    with pytest.raises(ValueError, match="disposition"):
        _effect_snapshot(encoded, **(args | {"disposition": "future"}))
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        _effect_snapshot("[]", **args)


@pytest.mark.parametrize("terminal_disposition", ("committed", "refused"))
def test_action_receipt_uses_real_first_attempt_effect_and_retained_frontier(operation_store, terminal_disposition):
    from sqlalchemy import insert

    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.advisor_output import ReadContinuationAction
    from elspeth.web.composer.continuation import ContinuationActionSource
    from elspeth.web.composer.continuation_codec import encode_frontier_chunk, encode_private_chunk, verify_private_chunk
    from elspeth.web.composer.continuation_progress import (
        ActionExecutionReceipt,
        continuation_action_key,
        decode_continuation_frontier,
        initial_continuation_frontier,
    )
    from elspeth.web.sessions.continuation import private_chunk_key, put_chunk, read_chunk, retain_action_execution_receipt_on_connection
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import metadata
    from elspeth.web.sessions.schemas import SendMessageRequest

    engine, repository, _, _, sid = operation_store
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    with engine.begin() as conn:
        assert require_continuation_mutation(conn, context) is True
        selected = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        stack = decode_continuation_snapshot(selected.payload_json)
        request = SendMessageRequest.model_validate_json(stack.frames[0].request_json)
        frontier = initial_continuation_frontier(
            request=request.content, original_message_id=stack.logical.original_user_message_id, graph_hash="b" * 64
        )
        chunk = encode_frontier_chunk(frontier)
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
        action = ReadContinuationAction(
            kind="read_tool",
            obligation_ids=("request",),
            evidence_ids=(),
            input_hash=stack.logical.request_hash,
            graph_hash=frontier.graph_hash,
            policy_hash=stack.policy_hash,
            tool_manifest_hash="c" * 64,
            tool_name="get_plugin_schema",
            arguments_json='{"name":"csv","type":"source"}',
        )
        action_json = canonical_json(action.model_dump(mode="json"))
        # Pure Advisor-source value only; no provider or read-tool execution is asserted.
        response = encode_private_chunk(codec="composer.response.v1", canonical='{"fixture":"pure action value"}')
        source = ContinuationActionSource(effect_key="provider:1:0", input_sha256=stack.logical.request_hash, response=response.reference)
        proposed = stack.model_copy(
            update={"semantic_frontier": chunk.reference, "active_action_json": action_json, "active_action_source": source}
        )
        encoded = encode_continuation_snapshot(proposed)
        effect_key = "session-sql:action-fixture"
        conn.execute(
            insert(effects).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                effect_key=effect_key,
                input_sha256=action.input_hash,
                owner="session_sql",
                action_json=action_json,
                disposition="prepared",
                creator_sequence=claim.segment_sequence,
                creator_generation=claim.ownership_generation,
            )
        )
        actual_creator = conn.execute(
            select(attempts).where(
                attempts.c.session_id == str(sid),
                attempts.c.operation_id == record.operation_id,
                attempts.c.sequence == claim.segment_sequence,
                attempts.c.generation == claim.ownership_generation,
            )
        ).one()
        assert actual_creator.session_operation_id == context.fence.operation_id
        assert actual_creator.session_operation_epoch == context.fence.operation_epoch
        effect_where = (effects.c.session_id == str(sid), effects.c.operation_id == record.operation_id, effects.c.effect_key == effect_key)
        original_effect = tuple(conn.execute(select(effects).where(*effect_where)).one())

        def rows():
            return {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in metadata.tables.values()}

        args = {
            "session_id": str(sid),
            "operation_id": record.operation_id,
            "effect_key": effect_key,
            "disposition": "prepared",
            "snapshot_json": encoded,
        }
        prepared = retain_action_execution_receipt_on_connection(conn, **args)
        retained = decode_continuation_snapshot(prepared)
        ref = retained.semantic_frontier
        assert ref is not None
        canonical = read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key=f"value:{ref.codec}:{ref.digest}").decode(
            "utf-8"
        )
        verify_private_chunk(ref, canonical)
        prepared_frontier = decode_continuation_frontier(canonical)
        action_key = continuation_action_key(action, retained_evidence={})
        assert prepared_frontier.action_receipts == (ActionExecutionReceipt(action_key, effect_key, "prepared"),)
        assert retained == proposed.model_copy(update={"semantic_frontier": ref})
        assert tuple(conn.execute(select(effects).where(*effect_where)).one()) == original_effect
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        before_refusals = rows()
        for wrong, reason in (
            (
                {
                    "snapshot_json": encode_continuation_snapshot(
                        proposed.model_copy(update={"logical": proposed.logical.model_copy(update={"operation_id": str(uuid4())})})
                    )
                },
                "actual request/effect creator",
            ),
            ({"disposition": "dispatched"}, "actual request/effect creator"),
            (
                {"snapshot_json": encode_continuation_snapshot(proposed.model_copy(update={"semantic_frontier": None}))},
                "lost its retained semantic frontier",
            ),
            (
                {
                    "snapshot_json": encode_continuation_snapshot(
                        proposed.model_copy(
                            update={
                                "semantic_frontier": chunk.reference.model_copy(update={"byte_length": chunk.reference.byte_length + 1})
                            }
                        )
                    )
                },
                "bytes differ from their binding",
            ),
        ):
            with pytest.raises(AuditIntegrityError, match=reason):
                retain_action_execution_receipt_on_connection(conn, **(args | wrong))
            assert rows() == before_refusals
        assert retain_action_execution_receipt_on_connection(conn, **args) == prepared
        assert rows() == before_refusals
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key="result:action-fixture",
            payload=b'{"success":true}',
            codec="tool-result.v1",
        )
        conn.execute(
            update(effects)
            .where(*effect_where)
            .values(disposition=terminal_disposition, receipt_key="fixture-receipt", result_chunk_key="result:action-fixture")
        )
        terminal = retain_action_execution_receipt_on_connection(
            conn, **(args | {"disposition": terminal_disposition, "snapshot_json": prepared})
        )
        completed = decode_continuation_snapshot(terminal)
        assert completed.active_action_json is None and completed.active_action_source is None
        final_ref = completed.semantic_frontier
        assert final_ref is not None
        final_canonical = read_chunk(
            conn, session_id=str(sid), operation_id=record.operation_id, key=f"value:{final_ref.codec}:{final_ref.digest}"
        ).decode("utf-8")
        verify_private_chunk(final_ref, final_canonical)
        assert decode_continuation_frontier(final_canonical).action_receipts == (
            ActionExecutionReceipt(action_key, effect_key, "prepared"),
            ActionExecutionReceipt(action_key, effect_key, terminal_disposition),
        )
        before_replay = rows()
        assert (
            retain_action_execution_receipt_on_connection(conn, **(args | {"disposition": terminal_disposition, "snapshot_json": terminal}))
            == terminal
        )
        assert rows() == before_replay
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        conn.execute(
            insert(effects).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                effect_key="session-sql:no-action-fixture",
                input_sha256=action.input_hash,
                owner="session_sql",
                action_json=None,
                disposition="prepared",
                creator_sequence=claim.segment_sequence,
                creator_generation=claim.ownership_generation,
            )
        )
        before_no_action = rows()
        assert (
            retain_action_execution_receipt_on_connection(
                conn, **(args | {"effect_key": "session-sql:no-action-fixture", "snapshot_json": terminal})
            )
            == terminal
        )
        assert rows() == before_no_action
    repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    (
        "no_history_context",
        "released_context",
        "expired_fence",
        "revoked_attempt",
        "checkpoint",
        "cursor",
        "archived_principal",
        "transferred_principal",
        "non_nominal_locator",
        "missing_effect",
        "creator_locator",
        "input_locator",
        "receipt_locator",
        "state_locator",
        "hash_locator",
        "cancelled_parent",
        "expired_segment",
    ),
)
async def test_historical_reference_facet_requires_current_sql_history_and_preserves_rows(operation_store, monkeypatch, change):
    from contextlib import contextmanager

    from sqlalchemy import insert
    from tests.fixtures.identities import ensure_test_identity

    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.composer.turn_effects import authored_state_hash
    from elspeth.web.coordination import composer_operation_authority as authority_module
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation import (
        ContinuationCASConflict,
        HistoricalEffectLocator,
        ValidatedHistoricalEffectReference,
        put_chunk,
    )
    from elspeth.web.sessions.converters import state_from_record
    from elspeth.web.sessions.graph_recovery import checkpoint_data, empty_recovery_state
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import metadata

    engine, repository, _, service, sid = operation_store
    ordinary = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    with engine.connect() as conn:
        physical = conn.execute(
            select(fences).where(
                fences.c.session_id == str(sid),
                fences.c.operation_id == ordinary.fence.operation_id,
                fences.c.operation_epoch == ordinary.fence.operation_epoch,
                fences.c.lease_token == ordinary.fence.lease_token,
                fences.c.released_at.is_(None),
                fences.c.lease_expires_at > database_now(conn),
            )
        ).one()
        assert physical.operation_kind == SessionOperationKind.COMPOSE.value
        assert (
            conn.execute(
                select(attempts).where(
                    attempts.c.session_id == str(sid),
                    attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                )
            ).one_or_none()
            is None
        )
        assert require_continuation_mutation(conn, ordinary, allow_cancel=True, allow_expired_segment=True) is False
    repository.release(ordinary)
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    saved = await service.save_composition_state(
        sid, checkpoint_data(empty_recovery_state()), provenance="session_seed", session_operation_context=context
    )
    graph_hash = authored_state_hash(state_from_record(saved))
    with engine.begin() as conn:
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key="result:facet",
            payload=b'{"success":true}',
            codec="tool-result.v1",
        )
        conn.execute(
            insert(effects).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                effect_key="response:0:tool:0",
                input_sha256="b" * 64,
                owner="session_sql",
                disposition="committed",
                receipt_key=str(saved.id),
                result_chunk_key="result:facet",
                creator_sequence=claim.segment_sequence,
                creator_generation=claim.ownership_generation,
                committed_state_ids_json='["' + str(saved.id) + '"]',
                graph_sha256=graph_hash,
            )
        )
        selected = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        assert require_continuation_mutation(conn, context, allow_cancel=True, allow_expired_segment=True) is True
    locator = HistoricalEffectLocator(
        effect_key="response:0:tool:0",
        input_sha256="b" * 64,
        creator_operation_id=context.fence.operation_id,
        receipt_id=str(saved.id),
        inserted_state_ids=(saved.id,),
        authored_state_hash=graph_hash,
    )
    args = {"expected_checkpoint_id": selected.checkpoint_id, "expected_cursor": selected.cursor, "locators": (locator,)}
    actual_transaction = authority_module.locked_session_transaction
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    observed = []

    @contextmanager
    def observe_live_transaction(selected_engine, session_id):
        with actual_transaction(selected_engine, session_id) as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            try:
                yield conn
            finally:
                after = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
                assert after == before, "historical reference facet changed rows inside its live SQL transaction"
                observed.append(True)

    monkeypatch.setattr(authority_module, "locked_session_transaction", observe_live_transaction)
    first = authority.validate_historical_effect_references(running, **args)
    assert len(first) == 1 and type(first[0]) is ValidatedHistoricalEffectReference
    reference = first[0]
    assert reference.session_id == sid and reference.operation_id == record.operation_id
    assert reference.effect_key == locator.effect_key and reference.input_sha256 == locator.input_sha256
    assert reference.creator_operation_id == context.fence.operation_id
    assert reference.creator_operation_epoch == context.fence.operation_epoch
    assert reference.receipt_id == str(saved.id) and reference.inserted_state_ids == (saved.id,)
    assert reference.authored_state_hash == graph_hash and reference.result_bytes == b'{"success":true}'
    assert observed == [True]
    refused_running = running
    expected_error = AuditIntegrityError
    expected_message = None
    if change == "no_history_context":
        assert ordinary.fence.session_id == context.fence.session_id
        assert ordinary.fence.operation_epoch != context.fence.operation_epoch
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(attempts).where(
                        attempts.c.session_id == str(sid),
                        attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                    )
                ).one_or_none()
                is None
            )
            assert require_continuation_mutation(conn, ordinary, allow_cancel=True, allow_expired_segment=True) is False
        refused_running = ComposerOperationRunning(claim=claim, session_operation_context=ordinary)
        expected_message = "historical reference restore requires current continuation authority"
    elif change in ("released_context", "expired_fence", "revoked_attempt", "archived_principal", "transferred_principal"):
        if change == "released_context":
            repository.release(context)
        else:
            with engine.begin() as conn:
                now = database_now(conn)
                if change == "expired_fence":
                    conn.execute(update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=now - timedelta(seconds=1)))
                elif change == "revoked_attempt":
                    conn.execute(
                        update(attempts)
                        .where(attempts.c.session_id == str(sid), attempts.c.session_operation_epoch == context.fence.operation_epoch)
                        .values(revoked_at=now, outcome="interrupted")
                    )
                elif change == "archived_principal":
                    conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=now))
                else:
                    ensure_test_identity(conn, identity_id="bob")
                    conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(user_id="bob"))
        with engine.connect() as conn:
            history = conn.execute(
                select(attempts).where(
                    attempts.c.session_id == str(sid),
                    attempts.c.session_operation_epoch == context.fence.operation_epoch,
                )
            ).one()
            assert history.session_operation_id == context.fence.operation_id
            with pytest.raises(ComposerOperationFenceLost):
                require_continuation_mutation(conn, context, allow_cancel=True, allow_expired_segment=True)
        expected_error = ComposerOperationFenceLost
    elif change == "checkpoint":
        args["expected_checkpoint_id"] = str(uuid4())
        expected_error = ContinuationCASConflict
        expected_message = "continuation snapshot changed before effect admission"
    elif change == "cursor":
        args["expected_cursor"] = selected.cursor + 1
        expected_error = ContinuationCASConflict
        expected_message = "continuation snapshot changed before effect admission"
    elif change == "non_nominal_locator":
        args["locators"] = (object(),)
        expected_error = TypeError
        expected_message = "historical validation requires nominal retained locators"
    elif change in ("cancelled_parent", "expired_segment"):
        with engine.begin() as conn:
            now = database_now(conn)
            if change == "cancelled_parent":
                conn.execute(
                    update(jobs)
                    .where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)
                    .values(cancel_requested_at=now)
                )
            else:
                conn.execute(
                    update(segments)
                    .where(segments.c.session_id == str(sid), segments.c.operation_id == record.operation_id)
                    .values(
                        started_at=now - timedelta(seconds=3),
                        cooperative_at=now - timedelta(seconds=2),
                        deadline_at=now - timedelta(seconds=1),
                    )
                )
            assert require_continuation_mutation(conn, context, allow_cancel=True, allow_expired_segment=True) is True
        assert authority.validate_historical_effect_references(running, **args) == first
        assert observed == [True, True]
        repository.release(context)
        return
    else:
        changes = {
            "missing_effect": ({"effect_key": "missing-effect"}, "historical graph effect lacks a committed SQL owner"),
            "creator_locator": ({"creator_operation_id": str(uuid4())}, "historical graph effect creator mismatch"),
            "input_locator": ({"input_sha256": "c" * 64}, "historical graph effect input or receipt mismatch"),
            "receipt_locator": ({"receipt_id": str(uuid4())}, "historical graph effect input or receipt mismatch"),
            "state_locator": ({"inserted_state_ids": (uuid4(),)}, "historical graph effect state identities differ"),
            "hash_locator": ({"authored_state_hash": "c" * 64}, "historical graph effect lost its state frontier"),
        }
        replacement, expected_message = changes[change]
        args["locators"] = (replace(locator, **replacement),)
    with pytest.raises(expected_error, match=expected_message):
        authority.validate_historical_effect_references(refused_running, **args)
    assert observed == ([True] if change == "non_nominal_locator" else [True, True])
    if change not in ("released_context", "expired_fence", "revoked_attempt", "archived_principal", "transferred_principal"):
        repository.release(context)


@pytest.mark.parametrize(
    "change",
    (
        "no_history_context",
        "claim_token",
        "claim_attempt",
        "claim_operation",
        "claim_sequence",
        "claim_generation",
        "nominal_commit",
        "nominal_running",
        "nominal_context",
        "context_mismatch",
        "checkpoint",
        "cursor",
        "wrong_input",
        "nonprepared",
        "wrong_owner",
        "nonread_completed_read",
        "released_context",
        "expired_fence",
        "revoked_attempt",
        "archived_principal",
        "transferred_principal",
        "cancelled_parent",
        "expired_segment",
    ),
)
def test_owner_commit_requires_actual_current_claim_history_without_writes(operation_store, change):
    from sqlalchemy import insert
    from tests.fixtures.identities import ensure_test_identity

    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationOwnerCommit, require_owner_commit
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import metadata

    engine, repository, _, _, sid = operation_store
    ordinary = None
    if change in ("no_history_context", "context_mismatch"):
        ordinary = repository.acquire(
            session_id=sid,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="test-owner",
            lease_seconds=30,
        )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(fences).where(
                        fences.c.session_id == str(sid),
                        fences.c.operation_id == ordinary.fence.operation_id,
                        fences.c.operation_epoch == ordinary.fence.operation_epoch,
                        fences.c.lease_token == ordinary.fence.lease_token,
                        fences.c.released_at.is_(None),
                        fences.c.lease_expires_at > database_now(conn),
                    )
                )
                .one()
                .operation_kind
                == SessionOperationKind.COMPOSE.value
            )
            assert (
                conn.execute(
                    select(attempts).where(
                        attempts.c.session_id == str(sid),
                        attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                    )
                ).one_or_none()
                is None
            )
            assert require_continuation_mutation(conn, ordinary) is False
        repository.release(ordinary)
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    with locked_session_transaction(engine, str(sid)) as conn:
        assert require_continuation_mutation(conn, context) is True
        history = conn.execute(
            select(attempts).where(
                attempts.c.session_id == str(sid),
                attempts.c.session_operation_epoch == context.fence.operation_epoch,
            )
        ).one()
        segment = conn.execute(
            select(segments).where(
                segments.c.session_id == history.session_id,
                segments.c.operation_id == history.operation_id,
                segments.c.sequence == history.sequence,
                segments.c.generation == history.generation,
            )
        ).one()
        assert (
            history.session_id,
            history.operation_id,
            history.sequence,
            history.generation,
            history.claim_attempt,
            segment.claim_token,
        ) == (
            str(claim.session_id),
            claim.operation_id,
            claim.segment_sequence,
            claim.ownership_generation,
            claim.attempt,
            claim.claim_token,
        )
        assert history.session_operation_id == context.fence.operation_id
        assert history.session_operation_lease_token == context.fence.lease_token
        selected = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        # Ordinary FK-valid C2 fixture setup; production prepare_effect_on_connection was not called.
        conn.execute(
            insert(effects).values(
                session_id=str(sid),
                operation_id=record.operation_id,
                effect_key="session-sql:owner-boundary",
                input_sha256="b" * 64,
                owner="session_sql",
                disposition="prepared",
                creator_sequence=claim.segment_sequence,
                creator_generation=claim.ownership_generation,
            )
        )
        commit = ContinuationOwnerCommit(running, "session-sql:owner-boundary", "b" * 64, selected.checkpoint_id, selected.cursor)
        before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
        assert require_owner_commit(conn, commit, context) == selected
        assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
    refused_commit = commit
    refused_context = context
    allow_completed_read = False
    expected_error = AuditIntegrityError
    expected_message = "SQL owner commit changed its current claim/history binding"
    try:
        if change == "no_history_context":
            assert ordinary is not None and ordinary.fence.operation_epoch != context.fence.operation_epoch
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        select(attempts).where(
                            attempts.c.session_id == str(sid),
                            attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                        )
                    ).one_or_none()
                    is None
                )
                assert require_continuation_mutation(conn, ordinary) is False
            refused_context = ordinary
            refused_commit = replace(commit, running=ComposerOperationRunning(claim=claim, session_operation_context=ordinary))
            expected_message = "SQL owner commit requires current continuation authority"
        elif change in ("claim_token", "claim_attempt", "claim_operation", "claim_sequence", "claim_generation"):
            alterations = {
                "claim_token": {"claim_token": claim.claim_token + "-changed"},
                "claim_attempt": {"attempt": claim.attempt + 1},
                "claim_operation": {"operation_id": str(uuid4())},
                "claim_sequence": {"segment_sequence": claim.segment_sequence + 1},
                "claim_generation": {"ownership_generation": claim.ownership_generation + 1},
            }
            changed_claim = replace(claim, **alterations[change])
            refused_commit = replace(commit, running=ComposerOperationRunning(claim=changed_claim, session_operation_context=context))
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, context) is True
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
                effect = conn.execute(
                    select(effects).where(
                        effects.c.session_id == str(sid),
                        effects.c.operation_id == record.operation_id,
                        effects.c.effect_key == commit.effect_key,
                    )
                ).one()
                assert effect.disposition == "prepared" and effect.owner == "session_sql" and effect.input_sha256 == commit.input_sha256
                assert (effect.creator_sequence, effect.creator_generation) == (claim.segment_sequence, claim.ownership_generation)
                if change in ("claim_token", "claim_attempt"):
                    assert (effect.creator_sequence, effect.creator_generation) == (
                        changed_claim.segment_sequence,
                        changed_claim.ownership_generation,
                    )
        elif change in ("nominal_commit", "nominal_running", "nominal_context", "context_mismatch"):
            expected_message = "SQL owner commit changed its actual execution context"
            if change == "nominal_commit":
                refused_commit = object()
            elif change == "nominal_running":
                refused_commit = replace(commit, running=object())
            elif change == "nominal_context":
                refused_context = object()
            else:
                assert ordinary is not None
                refused_context = ordinary
        elif change in ("checkpoint", "cursor", "wrong_input"):
            changes = {
                "checkpoint": {"expected_checkpoint_id": str(uuid4())},
                "cursor": {"expected_cursor": selected.cursor + 1},
                "wrong_input": {"input_sha256": "c" * 64},
            }
            refused_commit = replace(commit, **changes[change])
            expected_error = ContinuationCASConflict
            expected_message = (
                "SQL owner result is already retained or its prepared authority changed"
                if change == "wrong_input"
                else "continuation snapshot changed before effect admission"
            )
        elif change in ("nonprepared", "wrong_owner"):
            with engine.begin() as conn:
                values = {"disposition": "dispatched"} if change == "nonprepared" else {"owner": "unowned-test-effect"}
                conn.execute(
                    update(effects)
                    .where(
                        effects.c.session_id == str(sid),
                        effects.c.operation_id == record.operation_id,
                        effects.c.effect_key == commit.effect_key,
                    )
                    .values(**values)
                )
            expected_error = ContinuationCASConflict
            expected_message = "SQL owner result is already retained or its prepared authority changed"
        elif change == "nonread_completed_read":
            allow_completed_read = True
            expected_message = "owed read-result application cannot authorize a business SQL effect"
        else:
            if change == "released_context":
                repository.release(context)
            else:
                with engine.begin() as conn:
                    now = database_now(conn)
                    if change == "expired_fence":
                        conn.execute(
                            update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=now - timedelta(seconds=1))
                        )
                    elif change == "revoked_attempt":
                        conn.execute(
                            update(attempts)
                            .where(
                                attempts.c.session_id == str(sid),
                                attempts.c.session_operation_epoch == context.fence.operation_epoch,
                            )
                            .values(revoked_at=now, outcome="interrupted")
                        )
                    elif change == "archived_principal":
                        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=now))
                    elif change == "transferred_principal":
                        ensure_test_identity(conn, identity_id="bob")
                        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(user_id="bob"))
                    elif change == "cancelled_parent":
                        conn.execute(
                            update(jobs)
                            .where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)
                            .values(cancel_requested_at=now)
                        )
                    else:
                        assert change == "expired_segment"
                        conn.execute(
                            update(segments)
                            .where(
                                segments.c.session_id == str(sid),
                                segments.c.operation_id == record.operation_id,
                            )
                            .values(
                                started_at=now - timedelta(seconds=3),
                                cooperative_at=now - timedelta(seconds=2),
                                deadline_at=now - timedelta(seconds=1),
                            )
                        )
            expected_error = ComposerOperationCancelledDuringTurn if change == "cancelled_parent" else ComposerOperationFenceLost
            expected_message = None
            with engine.connect() as conn, pytest.raises(expected_error):
                require_continuation_mutation(conn, context)
        with locked_session_transaction(engine, str(sid)) as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            with pytest.raises(expected_error, match=expected_message):
                require_owner_commit(conn, refused_commit, refused_context, allow_completed_read=allow_completed_read)
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            if change in (
                "no_history_context",
                "claim_token",
                "claim_attempt",
                "claim_operation",
                "claim_sequence",
                "claim_generation",
                "nominal_commit",
                "nominal_running",
                "nominal_context",
                "context_mismatch",
                "checkpoint",
                "cursor",
                "wrong_input",
                "nonread_completed_read",
            ):
                assert require_owner_commit(conn, commit, context) == selected
    finally:
        if change not in (
            "released_context",
            "expired_fence",
            "revoked_attempt",
            "archived_principal",
            "transferred_principal",
            "expired_segment",
        ):
            repository.release(context)
