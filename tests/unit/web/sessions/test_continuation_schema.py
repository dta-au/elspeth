"""Continuation child metadata and actual SQLite preserve/refuse boundaries."""

from hashlib import sha256

import pytest
from sqlalchemy import inspect

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
from elspeth.web.sessions.schema import SessionSchemaError, initialize_session_schema


def test_continuation_children_bind_every_parent_key(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'continuation.db'}")
    try:
        initialize_session_schema(engine)
        inspector = inspect(engine)
        expected = {
            "composer_operation_segments",
            "composer_segment_attempts",
            "composer_continuation_chunks",
            "composer_continuation_events",
            "composer_continuation_checkpoints",
            "composer_continuation_effects",
            "composer_continuation_provider_ordinals",
        }
        assert expected <= set(inspector.get_table_names())
        for name in expected:
            parent_fks = [fk for fk in inspector.get_foreign_keys(name) if fk["referred_table"] == "composer_async_operations"]
            assert len(parent_fks) == 1
            assert parent_fks[0]["constrained_columns"] == ["session_id", "operation_id"]
            assert parent_fks[0]["referred_columns"] == ["session_id", "operation_id"]
    finally:
        engine.dispose()


def test_parent_owns_no_total_deadline_or_claim_and_has_resume_pointer():
    """Epoch 80: one lifecycle. Control: re-add a dropped V1 column or make a pointer nullable -> red."""
    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    assert not {"lifecycle_version", "deadline_at", "claim_token", "claim_expires_at"} & set(jobs.c.keys())
    pointer = (
        "phase",
        "status_revision",
        "current_segment_sequence",
        "ownership_generation",
        "checkpoint_id",
        "journal_cursor",
        "ingress_phase",
        "reserved_user_message_id",
    )
    assert set(pointer) <= set(jobs.c.keys())
    assert not any(jobs.c[name].nullable for name in pointer)
    assert "ix_composer_async_operations_claimable" not in {index.name for index in jobs.indexes}


@pytest.mark.parametrize("delta", [-1, 1])
def test_wrong_epoch_is_preserved_without_startup_upgrade(tmp_path, delta):
    path = tmp_path / "preserve.db"
    engine = create_session_engine(f"sqlite:///{path}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {SESSION_SCHEMA_EPOCH + delta}")
    engine.dispose()
    before = path.read_bytes()
    path.with_suffix(".before.sha256").write_text(sha256(before).hexdigest())
    with pytest.raises(SessionSchemaError):
        create_session_engine(f"sqlite:///{path}")
    assert path.read_bytes() == before
    # The read-only inspection handle skips writable schema admission, so the
    # initializer must produce its own refusal (house form, as in
    # test_quota_schema_epoch_contract.py).
    refused = create_session_engine(f"sqlite:///{path}", read_only=True)
    try:
        with pytest.raises(SessionSchemaError):
            initialize_session_schema(refused)
    finally:
        refused.dispose()
    path.with_suffix(".after.sha256").write_text(sha256(path.read_bytes()).hexdigest())
    assert path.read_bytes() == before


def test_current_epoch_with_missing_continuation_table_is_not_repaired(tmp_path):
    path = tmp_path / "missing-table.db"
    engine = create_session_engine(f"sqlite:///{path}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE composer_continuation_events")
    engine.dispose()
    before = path.read_bytes()
    path.with_suffix(".before.sha256").write_text(sha256(before).hexdigest())
    with pytest.raises(SessionSchemaError):
        create_session_engine(f"sqlite:///{path}")
    assert path.read_bytes() == before
    # The read-only inspection handle skips writable schema admission, so the
    # initializer must produce its own refusal (house form, as in
    # test_quota_schema_epoch_contract.py).
    refused = create_session_engine(f"sqlite:///{path}", read_only=True)
    try:
        with pytest.raises(SessionSchemaError):
            initialize_session_schema(refused)
    finally:
        refused.dispose()
    path.with_suffix(".after.sha256").write_text(sha256(path.read_bytes()).hexdigest())
    assert path.read_bytes() == before


@pytest.fixture
def continuation_parent(tmp_path):
    """Explicit SQL fixture only; no V2 runtime admission authority is enabled."""
    from uuid import uuid4

    from sqlalchemy import insert

    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from tests.fixtures.identities import ensure_test_identity

    engine = create_session_engine(f"sqlite:///{tmp_path / 'parent.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    repository = SQLiteLocalSessionOperationAuthority(engine)
    session = repository.create_session_with_initial_fence(
        user_id="alice", title="Continuation SQL fixture", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    sid, operation_id = str(session.id), str(uuid4())
    with engine.begin() as connection:
        now = database_now(connection)
        connection.execute(
            insert(jobs).values(
                session_id=sid,
                operation_id=operation_id,
                kind="compose_message",
                status="queued",
                phase="queued",
                current_segment_sequence=1,
                ownership_generation=1,
                checkpoint_id="initial-checkpoint",
                ingress_phase="pending",
                reserved_user_message_id="original",
                request_hash="a" * 64,
                actor_user_id="alice",
                request_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield engine, sid, operation_id
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", ["phase", "ingress_phase"])
def test_v2_parent_refuses_missing_owned_lifecycle_fields(continuation_parent, field):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    engine, sid, operation_id = continuation_parent
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id).values({field: None}))


def test_v2_parent_preserves_immutable_identity_and_monotonic_cursor(continuation_parent):
    from sqlalchemy import select, update
    from sqlalchemy.exc import IntegrityError

    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    engine, sid, operation_id = continuation_parent
    with engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.session_id == sid).values(journal_cursor=3, status_revision=2))
    for change in ({"reserved_user_message_id": "replacement"}, {"journal_cursor": 2}, {"status_revision": 1}):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.session_id == sid).values(**change))
    with engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
        assert row.reserved_user_message_id == "original"
        assert row.journal_cursor == 3 and row.status_revision == 2


@pytest.mark.parametrize("invalid", ["byte_length", "digest", "parent"])
def test_chunk_sql_constraints_refuse_invalid_rows(continuation_parent, invalid):
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    engine, sid, operation_id = continuation_parent
    values = {
        "session_id": sid,
        "operation_id": operation_id,
        "chunk_key": "invalid",
        "codec": "event.v2",
        "payload": b"exact",
        "sha256": sha256(b"exact").hexdigest(),
        "byte_length": 5,
    }
    if invalid == "byte_length":
        values["byte_length"] = 4
    elif invalid == "digest":
        values["sha256"] = "A" * 64
    else:
        values["operation_id"] = "00000000-0000-4000-8000-000000000000"
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(chunks).values(**values))


def test_v2_parent_committed_ingress_requires_actual_message(continuation_parent):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    engine, sid, _operation_id = continuation_parent
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.session_id == sid).values(ingress_phase="committed"))


def test_segment_fence_bundle_refuses_missing_epoch(continuation_parent):
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    engine, sid, operation_id = continuation_parent
    with pytest.raises(IntegrityError), engine.begin() as connection:
        now = database_now(connection)
        connection.execute(
            insert(segments).values(
                session_id=sid,
                operation_id=operation_id,
                sequence=1,
                state="queued",
                generation=1,
                session_operation_id="incomplete-fence",
                session_operation_lease_token="incomplete-lease",
                session_operation_epoch=None,
                enqueued_at=now,
                next_eligible_at=now,
                checkpoint_id="initial-checkpoint",
                next_action_json="{}",
                policy_sha256="a" * 64,
            )
        )


def test_v2_terminal_failure_requires_result_schema(continuation_parent):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    engine, sid, _operation_id = continuation_parent
    with pytest.raises(IntegrityError, match="ck_composer_operations_terminal_schema"), engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.session_id == sid)
            .values(
                status="failed",
                phase="settled",
                request_json=None,
                failure_code="worker_lost",
                settled_by="settle_lost",
                settled_at=database_now(connection),
                result_schema=None,
                result_json="{}",
                result_sha256="a" * 64,
            )
        )


@pytest.fixture
def snapshot_store(continuation_parent):
    """Seed complete owned C1 bytes under a real parent; C3 admission is absent."""
    from sqlalchemy import insert, update

    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.continuation import put_chunk
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from tests.unit.web.composer.test_continuation_frames import initial_stack

    engine, sid, operation_id = continuation_parent
    original = initial_stack()
    stack = original.model_copy(
        update={
            "logical": original.logical.model_copy(update={"session_id": sid, "operation_id": operation_id}),
            "mutation_ceiling": original.mutation_ceiling.model_copy(update={"session_id": sid}),
        }
    )
    encoded = encode_continuation_snapshot(stack)
    with engine.begin() as connection:
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key="initial-snapshot",
            payload=encoded.encode("utf-8"),
            codec="snapshot.v2",
        )
        connection.execute(
            insert(checkpoints).values(
                session_id=sid,
                operation_id=operation_id,
                checkpoint_id="initial-checkpoint",
                kind="transition",
                cursor=0,
                snapshot_chunk_key="initial-snapshot",
                frame_stack_version=1,
                created_at=database_now(connection),
            )
        )
        connection.execute(update(jobs).where(jobs.c.session_id == sid).values(request_hash=stack.logical.request_hash))
    return engine, sid, operation_id, encoded


def _snapshot_rows(engine):
    from sqlalchemy import select

    from elspeth.web.sessions.models import sessions_table

    with engine.connect() as connection:
        return {
            table.name: tuple(connection.execute(select(table).order_by(*table.primary_key.columns)).all())
            for table in sorted(sessions_table.metadata.tables.values(), key=lambda table: table.name)
        }


def test_complete_snapshot_appends_exact_owned_bytes_and_attributed_events(snapshot_store):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, read_chunk

    engine, sid, operation_id, encoded = snapshot_store
    with engine.begin() as connection:
        original = load_snapshot(connection, session_id=sid, operation_id=operation_id)
        assert original.payload_json == encoded and original.cursor == 0
        actual = append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id=original.checkpoint_id,
            expected_cursor=0,
            snapshot_json=encoded,
            event_batch=(ContinuationEvent("checkpoint", '{"exact":"café"}'), ContinuationEvent("input_prepared", "{}")),
            handoff=False,
        )
        assert actual.cursor == 2
        assert read_chunk(connection, session_id=sid, operation_id=operation_id, key="event:1") == '{"exact":"café"}'.encode()
        assert load_snapshot(connection, session_id=sid, operation_id=operation_id) == actual
    assert decode_continuation_snapshot(actual.payload_json).cursor == 2


@pytest.mark.parametrize("change", ["logical", "policy", "cursor", "ceiling"])
def test_snapshot_append_refuses_immutable_substitution_without_writes(snapshot_store, change):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, append_complete_snapshot, load_snapshot

    engine, sid, operation_id, encoded = snapshot_store
    stack = decode_continuation_snapshot(encoded)
    if change == "logical":
        stack = stack.model_copy(update={"logical": stack.logical.model_copy(update={"request_hash": "b" * 64})})
    elif change == "policy":
        # Policy revalidation belongs to the owned codec; use a valid policy
        # with a changed admitted descriptor rather than an invalid hash.
        stack = stack.model_copy(update={"admission_policy_json": stack.admission_policy_json + " "})
    elif change == "cursor":
        stack = stack.model_copy(update={"cursor": 4})
    else:
        stack = stack.model_copy(
            update={"mutation_ceiling": stack.mutation_ceiling.model_copy(update={"initial_trust_mode": "explicit_approve"})}
        )
    changed = encode_continuation_snapshot(stack)
    before = _snapshot_rows(engine)
    with engine.begin() as connection:
        current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
        with pytest.raises(ContinuationCASConflict, match="immutable"):
            append_complete_snapshot(
                connection,
                session_id=sid,
                operation_id=operation_id,
                expected_checkpoint_id=current.checkpoint_id,
                expected_cursor=current.cursor,
                snapshot_json=changed,
                event_batch=(ContinuationEvent("checkpoint", "{}"),),
                handoff=False,
            )
    assert _snapshot_rows(engine) == before


@pytest.mark.parametrize("changed", ["checkpoint", "cursor"])
def test_snapshot_cas_refuses_stale_pointer_without_writes(snapshot_store, changed):
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, append_complete_snapshot

    engine, sid, operation_id, encoded = snapshot_store
    before = _snapshot_rows(engine)
    with engine.begin() as connection, pytest.raises(ContinuationCASConflict, match="compare-and-swap"):
        append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id="stale" if changed == "checkpoint" else "initial-checkpoint",
            expected_cursor=1 if changed == "cursor" else 0,
            snapshot_json=encoded,
            event_batch=(ContinuationEvent("checkpoint", "{}"),),
            handoff=False,
        )
    assert _snapshot_rows(engine) == before


def test_snapshot_failed_final_cas_rolls_back_inserted_chunks_events_and_checkpoint(snapshot_store, monkeypatch):
    from sqlalchemy import update

    import elspeth.web.sessions.continuation as storage
    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    engine, sid, operation_id, encoded = snapshot_store
    before = _snapshot_rows(engine)
    actual_update = storage.update

    def lose_final_compare_and_swap(table):
        if table is jobs:
            return update(table).where(table.c.operation_id == "not-current")
        return actual_update(table)

    monkeypatch.setattr(storage, "update", lose_final_compare_and_swap)
    with pytest.raises(storage.ContinuationCASConflict, match="compare-and-swap"), engine.begin() as connection:
        storage.append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id="initial-checkpoint",
            expected_cursor=0,
            snapshot_json=encoded,
            event_batch=(storage.ContinuationEvent("checkpoint", "{}"),),
            handoff=False,
        )
    assert _snapshot_rows(engine) == before


@pytest.mark.parametrize("change", ["cursor", "identity", "digest"])
def test_complete_snapshot_load_refuses_corrupted_persisted_binding(snapshot_store, change):
    from sqlalchemy import update

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    engine, sid, operation_id, encoded = snapshot_store
    with engine.begin() as connection:
        if change == "cursor":
            connection.execute(update(jobs).where(jobs.c.session_id == sid).values(journal_cursor=1))
        elif change == "digest":
            connection.execute(update(chunks).where(chunks.c.session_id == sid).values(sha256="b" * 64))
        else:
            stack = decode_continuation_snapshot(encoded)
            changed = encode_continuation_snapshot(
                stack.model_copy(update={"logical": stack.logical.model_copy(update={"operation_id": "another-operation"})})
            )
            payload = changed.encode("utf-8")
            connection.execute(
                update(chunks)
                .where(chunks.c.session_id == sid)
                .values(payload=payload, byte_length=len(payload), sha256=sha256(payload).hexdigest())
            )
        with pytest.raises(AuditIntegrityError):
            load_snapshot(connection, session_id=sid, operation_id=operation_id)


def test_owned_event_preserves_shared_closed_vocabulary():
    from typing import cast

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import ContinuationEvent
    from elspeth.web.sessions.continuation_events import CONTINUATION_EVENT_TAGS, ContinuationEventTag

    assert "checkpoint" in CONTINUATION_EVENT_TAGS
    assert tuple(ContinuationEvent(tag, "{}").tag for tag in CONTINUATION_EVENT_TAGS) == CONTINUATION_EVENT_TAGS
    for invalid in ("future_unowned_event", 1):
        with pytest.raises(AuditIntegrityError, match="unfamiliar owned tag"):
            ContinuationEvent(cast(ContinuationEventTag, invalid), "{}")
    with pytest.raises(AuditIntegrityError, match="owned JSON text"):
        ContinuationEvent("checkpoint", cast(str, None))


def _retained_payload_rows(connection, session_id, operation_id):
    """Observe actual operation bytes without selecting every payload."""
    from sqlalchemy import LargeBinary, cast, func, select

    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    if connection.dialect.name == "sqlite":
        actual_bytes = func.length(cast(chunks.c.payload, LargeBinary))
    elif connection.dialect.name == "postgresql":
        actual_bytes = func.octet_length(chunks.c.payload)
    else:
        raise AssertionError("retained-byte observation requires a supported database")
    rows = connection.execute(
        select(chunks.c.chunk_key, chunks.c.codec, chunks.c.byte_length, actual_bytes.label("actual_bytes")).where(
            chunks.c.session_id == session_id,
            chunks.c.operation_id == operation_id,
        )
    ).all()
    assert all(type(row.actual_bytes) is int and row.byte_length == row.actual_bytes for row in rows)
    return {row.chunk_key: (row.codec, row.actual_bytes) for row in rows}


def _seed_snapshot_cursor_boundary(snapshot_store, cursor):
    """Seed a C2 numeric edge, without a live C3 or history-count claim."""
    from sqlalchemy import insert, update

    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.continuation import put_chunk
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from elspeth.web.sessions.models import composer_continuation_events_table as events

    engine, sid, operation_id, encoded = snapshot_store
    stack = decode_continuation_snapshot(encoded).model_copy(update={"cursor": cursor})
    boundary_json = encode_continuation_snapshot(stack)
    checkpoint_id = "journal-numeric-boundary"
    snapshot_key, event_key = f"snapshot:{checkpoint_id}", f"event:{cursor}"
    with engine.begin() as connection:
        now = database_now(connection)
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key=snapshot_key,
            payload=boundary_json.encode("utf-8"),
            codec="snapshot.v2",
        )
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key=event_key,
            payload=b"{}",
            codec="event.v2",
        )
        connection.execute(
            insert(events).values(
                session_id=sid,
                operation_id=operation_id,
                sequence=cursor,
                tag="checkpoint",
                payload_chunk_key=event_key,
                created_at=now,
            )
        )
        connection.execute(
            insert(checkpoints).values(
                session_id=sid,
                operation_id=operation_id,
                checkpoint_id=checkpoint_id,
                kind="transition",
                cursor=cursor,
                snapshot_chunk_key=snapshot_key,
                frame_stack_version=1,
                created_at=now,
            )
        )
        connection.execute(
            update(jobs)
            .where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)
            .values(
                checkpoint_id=checkpoint_id,
                journal_cursor=cursor,
                status_revision=jobs.c.status_revision + 1,
            )
        )


@pytest.mark.parametrize("previous_events", (0, 9))
def test_complete_snapshot_byte_measurement_uses_final_root_and_raw_event_utf8(snapshot_store, previous_events):
    from sqlalchemy import select

    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, read_chunk
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints

    engine, sid, operation_id, encoded = snapshot_store
    payload_json = '{"exact":"café 雪\\n"}'
    with engine.begin() as connection:
        current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
        if previous_events:
            current = append_complete_snapshot(
                connection,
                session_id=sid,
                operation_id=operation_id,
                expected_checkpoint_id=current.checkpoint_id,
                expected_cursor=current.cursor,
                snapshot_json=current.payload_json,
                event_batch=tuple(ContinuationEvent("checkpoint", "{}") for _ in range(previous_events)),
                handoff=False,
            )
        before = _retained_payload_rows(connection, sid, operation_id)
        final_cursor = current.cursor + 1
        expected_root = encode_continuation_snapshot(
            decode_continuation_snapshot(current.payload_json).model_copy(update={"cursor": final_cursor})
        )
        actual = append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id=current.checkpoint_id,
            expected_cursor=current.cursor,
            snapshot_json=current.payload_json,
            event_batch=(ContinuationEvent("checkpoint", payload_json),),
            handoff=False,
        )
        after = _retained_payload_rows(connection, sid, operation_id)
        root_key = connection.execute(
            select(checkpoints.c.snapshot_chunk_key).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation_id,
                checkpoints.c.checkpoint_id == actual.checkpoint_id,
            )
        ).scalar_one()
        event_key = f"event:{final_cursor}"
        assert actual.payload_json == expected_root and actual.cursor == final_cursor
        assert read_chunk(connection, session_id=sid, operation_id=operation_id, key=root_key) == expected_root.encode("utf-8")
        assert read_chunk(connection, session_id=sid, operation_id=operation_id, key=event_key) == payload_json.encode("utf-8")
        assert after[root_key] == ("snapshot.v2", len(expected_root.encode("utf-8")))
        assert after[event_key] == ("event.v2", len(payload_json.encode("utf-8")))
        assert after[root_key][1] > len(expected_root)
        assert after[event_key][1] > len(payload_json)
        assert after[root_key][1] != len(canonical_json(expected_root).encode("utf-8"))
        assert after[event_key][1] != len(canonical_json(payload_json).encode("utf-8"))
        assert after.keys() - before.keys() == {root_key, event_key}
        assert sum(size for _codec, size in after.values()) == (
            sum(size for _codec, size in before.values()) + after[root_key][1] + after[event_key][1]
        )
        assert read_chunk(connection, session_id=sid, operation_id=operation_id, key="initial-snapshot") == encoded.encode("utf-8")
        assert load_snapshot(connection, session_id=sid, operation_id=operation_id) == actual
        if previous_events == 9:
            assert current.cursor == 9 and actual.cursor == 10
            assert after[root_key][1] == len(current.payload_json.encode("utf-8")) + 1


def test_retained_payload_measurement_includes_history_and_off_head_keys(snapshot_store):
    from sqlalchemy import select

    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, put_chunk
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints

    engine, sid, operation_id, _encoded = snapshot_store
    payload = '"café"'.encode()
    with engine.begin() as connection:
        original = _retained_payload_rows(connection, sid, operation_id)
        put_chunk(connection, session_id=sid, operation_id=operation_id, key="off-head-original", payload=payload, codec="event.v2")
        first = _retained_payload_rows(connection, sid, operation_id)
        assert sum(size for _codec, size in first.values()) == sum(size for _codec, size in original.values()) + len(payload)
        put_chunk(connection, session_id=sid, operation_id=operation_id, key="off-head-original", payload=payload, codec="event.v2")
        assert _retained_payload_rows(connection, sid, operation_id) == first
        put_chunk(connection, session_id=sid, operation_id=operation_id, key="off-head-distinct-key", payload=payload, codec="event.v2")
        second = _retained_payload_rows(connection, sid, operation_id)
        assert sum(size for _codec, size in second.values()) == sum(size for _codec, size in first.values()) + len(payload)
        current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
        actual = append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id=current.checkpoint_id,
            expected_cursor=current.cursor,
            snapshot_json=current.payload_json,
            event_batch=(ContinuationEvent("checkpoint", "{}"),),
            handoff=False,
        )
        after = _retained_payload_rows(connection, sid, operation_id)
        head_key = connection.execute(
            select(checkpoints.c.snapshot_chunk_key).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation_id,
                checkpoints.c.checkpoint_id == actual.checkpoint_id,
            )
        ).scalar_one()
        assert all(after[key] == value for key, value in original.items())
        assert {"initial-snapshot", "off-head-original", "off-head-distinct-key", head_key, "event:1"} == set(after)
        latest_only = after[head_key][1] + after["event:1"][1]
        assert sum(size for _codec, size in after.values()) > latest_only
        assert after["off-head-original"] == after["off-head-distinct-key"] == ("event.v2", len(payload))


def test_journal_last_canonical_cursor_accepts_the_complete_append(snapshot_store):
    from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, read_chunk

    _seed_snapshot_cursor_boundary(snapshot_store, _CANONICAL_SAFE_INTEGER_MAX - 1)
    engine, sid, operation_id, _encoded = snapshot_store
    with engine.begin() as connection:
        current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
        actual = append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id=current.checkpoint_id,
            expected_cursor=current.cursor,
            snapshot_json=current.payload_json,
            event_batch=(ContinuationEvent("checkpoint", "{}"),),
            handoff=False,
        )
        assert actual.cursor == _CANONICAL_SAFE_INTEGER_MAX
        assert load_snapshot(connection, session_id=sid, operation_id=operation_id) == actual
        assert read_chunk(connection, session_id=sid, operation_id=operation_id, key=f"event:{_CANONICAL_SAFE_INTEGER_MAX}") == b"{}"


@pytest.mark.parametrize("batch_size", (1, 2))
def test_journal_exhaustion_refuses_the_whole_batch_before_dml(snapshot_store, batch_size):
    from rfc8785 import IntegerDomainError
    from sqlalchemy import event, select

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, put_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    _seed_snapshot_cursor_boundary(snapshot_store, _CANONICAL_SAFE_INTEGER_MAX - (batch_size - 1))
    engine, sid, operation_id, _encoded = snapshot_store
    writes = []

    def observe_dml(_connection, _cursor, statement, _parameters, context, _executemany):
        if context.isinsert or context.isupdate or context.isdelete:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", observe_dml)
    try:
        with engine.begin() as connection:
            put_chunk(
                connection,
                session_id=sid,
                operation_id=operation_id,
                key="dml-observer-positive",
                payload=b"{}",
                codec="event.v2",
            )
        assert writes, "DML observation must see the actual positive INSERT"
        writes.clear()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(chunks.c.chunk_key).where(
                        chunks.c.session_id == sid,
                        chunks.c.operation_id == operation_id,
                        chunks.c.chunk_key == "dml-observer-positive",
                    )
                ).scalar_one()
                == "dml-observer-positive"
            )
        assert writes == [], "DML observation must not classify a SELECT as a write"
        before = _snapshot_rows(engine)
        with pytest.raises((AuditIntegrityError, IntegerDomainError)), engine.begin() as connection:
            current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
            append_complete_snapshot(
                connection,
                session_id=sid,
                operation_id=operation_id,
                expected_checkpoint_id=current.checkpoint_id,
                expected_cursor=current.cursor,
                snapshot_json=current.payload_json,
                event_batch=tuple(ContinuationEvent("checkpoint", "{}") for _ in range(batch_size)),
                handoff=False,
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe_dml)
    assert _snapshot_rows(engine) == before
    assert writes == [], "journal exhaustion must refuse the complete batch before its first DML"
