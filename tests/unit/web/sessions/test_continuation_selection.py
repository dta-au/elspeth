"""Real selected SQL read, finish-once custody and names-only current inventory."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot,
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_frontier_chunk,
)
from elspeth.web.composer.continuation_env_inventory import (
    materialize_continuation_env_inventory,
    require_current_env_inventory_on_connection,
)
from elspeth.web.composer.continuation_progress import initial_continuation_frontier
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import session_operation_fences_table as fences
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

operation_store = test_composer_operation_authority.operation_store


def selected_case(store):
    engine, repository, authority, _sessions, sid = store
    record, _seed, _action = admit_continuation(store)
    running = start_continuation(repository, authority)
    original = authority.load_continuation_snapshot(running)
    root = decode_continuation_snapshot(original.payload_json)
    frontier = initial_continuation_frontier(
        request="Preserve café and exact context", original_message_id=root.logical.original_user_message_id, graph_hash="a" * 64
    )
    chunk = encode_frontier_chunk(frontier)
    root = root.model_copy(update={"semantic_frontier": chunk.reference})
    with engine.begin() as conn:
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            expected_checkpoint_id=original.checkpoint_id,
            expected_cursor=original.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"selected_read":true}'),),
            handoff=False,
        )
    return running, stored, chunk


def rows(store):
    engine = store[0]
    with engine.connect() as conn:
        return tuple(tuple(tuple(row) for row in conn.execute(select(table))) for table in (jobs, segments, chunks, fences))


def test_complete_private_read_preserves_actual_frontier_and_all_sql_rows(operation_store):
    _engine, _repository, authority, _sessions, _sid = operation_store
    running, stored, chunk = selected_case(operation_store)
    before = rows(operation_store)
    actual = authority.load_continuation_chunks(running, stored)
    assert actual == (chunk,)
    assert decode_frontier_chunk(actual[0]) == decode_frontier_chunk(chunk)
    selected = SelectedContinuationState(stored.checkpoint_id, decode_continuation_snapshot(stored.payload_json), actual)
    assert selected.chunks is actual
    assert rows(operation_store) == before
    with pytest.raises(AuditIntegrityError, match="complete ordered chunk cohort"):
        replace(selected, chunks=())


@pytest.mark.parametrize("change", ("checkpoint", "cursor", "payload", "claim"))
def test_read_refuses_substituted_selection_or_current_claim_after_positive(operation_store, change):
    _engine, _repository, authority, _sessions, _sid = operation_store
    running, stored, chunk = selected_case(operation_store)
    assert authority.load_continuation_chunks(running, stored) == (chunk,)
    before = rows(operation_store)
    selected = stored
    if change == "checkpoint":
        selected = replace(stored, checkpoint_id="other-checkpoint")
    elif change == "cursor":
        selected = replace(stored, cursor=stored.cursor + 1)
    elif change == "payload":
        selected = replace(stored, payload_json=stored.payload_json + " ")
    else:
        running = replace(running, claim=replace(running.claim, claim_token="foreign-token"))
    expected = AuditIntegrityError if change == "claim" else ContinuationCASConflict
    with pytest.raises(expected):
        authority.load_continuation_chunks(running, selected)
    assert rows(operation_store) == before


@pytest.mark.parametrize("change", ("missing", "codec", "digest", "bounds", "reference"))
def test_read_refuses_actual_sql_chunk_tamper_after_positive(operation_store, change):
    engine, _repository, authority, _sessions, _sid = operation_store
    running, stored, chunk = selected_case(operation_store)
    assert authority.load_continuation_chunks(running, stored) == (chunk,)
    predicate = chunks.c.chunk_key == private_chunk_key(chunk)
    if change == "bounds":
        before = rows(operation_store)
        with pytest.raises(IntegrityError, match="ck_composer_chunks_exact_bytes"), engine.begin() as conn:
            conn.execute(update(chunks).where(predicate).values(byte_length=chunk.reference.byte_length + 1))
        assert rows(operation_store) == before
        assert authority.load_continuation_chunks(running, stored) == (chunk,)
        return
    with engine.begin() as conn:
        if change == "missing":
            conn.execute(delete(chunks).where(predicate))
        elif change == "codec":
            conn.execute(update(chunks).where(predicate).values(codec="composer.messages.v1"))
        elif change == "digest":
            conn.execute(update(chunks).where(predicate).values(sha256="b" * 64))
        else:
            payload = b"{}"
            conn.execute(
                update(chunks).where(predicate).values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
    before = rows(operation_store)
    with pytest.raises(AuditIntegrityError):
        authority.load_continuation_chunks(running, stored)
    assert rows(operation_store) == before


@pytest.mark.parametrize("cut", ("cancel", "segment", "lease"))
def test_reconciliation_read_keeps_live_lease_and_ordinary_inventory_strict(operation_store, cut):
    engine, _repository, authority, _sessions, _sid = operation_store
    running, stored, chunk = selected_case(operation_store)
    assert authority.load_continuation_chunks(running, stored) == (chunk,)
    with engine.begin() as conn:
        now = database_now(conn)
        if cut == "cancel":
            conn.execute(update(jobs).values(cancel_requested_at=now))
        elif cut == "segment":
            conn.execute(
                update(segments).values(
                    started_at=now - timedelta(seconds=3), cooperative_at=now - timedelta(seconds=2), deadline_at=now - timedelta(seconds=1)
                )
            )
        else:
            conn.execute(update(fences).values(lease_expires_at=now - timedelta(seconds=1)))
    before = rows(operation_store)
    if cut == "lease":
        with pytest.raises(ComposerOperationFenceLost):
            authority.load_continuation_chunks(running, stored)
    else:
        assert authority.load_continuation_chunks(running, stored) == (chunk,)
        with pytest.raises((ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost)):
            authority.read_continuation_env_selection(running, expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    assert rows(operation_store) == before


@pytest.mark.asyncio
async def test_finish_once_returns_exact_cohort_and_completes_both_registered_owners(operation_store):
    authority = operation_store[2]
    running, stored, chunk = selected_case(operation_store)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    assert selected.chunks == (chunk,)
    assert selected.stack == decode_continuation_snapshot(stored.payload_json)
    work.coordinator.assert_completed()
    assert tuple(ticket.key.source for ticket in work.coordinator.tickets) == (
        RequiredWorkSource.CONTINUATION_READ_SQL,
        RequiredWorkSource.CONTINUATION_READ_PROJECTION,
    )
    assert all(not ticket.permits_process_drain for ticket in work.coordinator.tickets)


@pytest.mark.asyncio
async def test_joined_sql_carrier_cannot_replace_actual_result_identity(operation_store):
    authority = operation_store[2]
    running, stored, _chunk = selected_case(operation_store)
    work = inventory_work(running)
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    outcome = await run_required_sql_finish_once(sql, authority.load_continuation_chunks, running, stored)
    assert type(outcome) is RequiredSQLReturned
    work.coordinator.validate_continuation_sql_outcome(
        ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
    )
    copied = tuple(item for item in outcome.value)
    assert copied == outcome.value and copied is not outcome.value
    with pytest.raises(AuditIntegrityError, match="replaced its actual SQL result"):
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=RequiredSQLReturned(copied, ())
        )
    projection.complete_without_submission()
    work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_validation_failure_closes_projection_and_preserves_original_exception(operation_store, monkeypatch):
    authority = operation_store[2]
    running, stored, _chunk = selected_case(operation_store)
    work = inventory_work(running)
    original = RequiredWorkCoordinator.validate_continuation_sql_outcome
    failure = AuditIntegrityError("controlled post-join validation failure")

    def reject(self, **kwargs):
        original(self, **kwargs)
        raise failure

    monkeypatch.setattr(RequiredWorkCoordinator, "validate_continuation_sql_outcome", reject)
    with pytest.raises(AuditIntegrityError) as caught:
        await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    assert caught.value is failure
    work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_cancelled_wait_joins_actual_read_once_before_raising(operation_store, monkeypatch):
    authority = operation_store[2]
    running, stored, _chunk = selected_case(operation_store)
    entered, release = Event(), Event()
    actual = authority.load_continuation_chunks
    reads = []

    def held(*args):
        entered.set()
        assert release.wait(5), "selected read was not released"
        value = actual(*args)
        reads.append(value)
        return value

    monkeypatch.setattr(authority, "load_continuation_chunks", held)
    work = inventory_work(running)
    task = asyncio.create_task(load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work))
    try:
        await test_composer_operation_authority._wait_for_thread_event(entered)
        task.cancel("selected read cancellation")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="selected read cancellation"):
        await task
    assert len(reads) == 1
    work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", (False, True))
async def test_real_current_inventory_consumes_actual_selected_owner(operation_store, monkeypatch, configured):
    engine, _repository, authority, sessions, _sid = operation_store
    running, stored, _chunk = selected_case(operation_store)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    resolver = None
    if configured:
        monkeypatch.setenv("INVENTORY_TEST_NAME", "offline-test-value")
        resolver = ScopedSecretResolver(
            WebSecretService(
                user_store=UserSecretStore(engine=engine, master_key="inventory-test-master-key-32chars!"),
                server_store=ServerSecretStore(allowlist=("INVENTORY_TEST_NAME",)),
            ),
            "local",
        )

        def forbidden_resolution(*args, **kwargs):
            raise AssertionError("names-only inventory resolved a secret")

        monkeypatch.setattr(resolver, "resolve", forbidden_resolution)
        monkeypatch.setattr(resolver, "resolve_scoped", forbidden_resolution)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions, secret_service=resolver), authority=authority, selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    assert inventory.running is running
    assert inventory.names == (frozenset({"INVENTORY_TEST_NAME"}) if configured else frozenset())
    with engine.begin() as conn:
        assert (
            require_current_env_inventory_on_connection(
                conn, running, inventory=inventory, expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor
            )
            is inventory.names
        )
        with pytest.raises(AuditIntegrityError, match="actual current inventory owner"):
            require_current_env_inventory_on_connection(
                conn, replace(running), inventory=inventory, expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor
            )


@pytest.mark.asyncio
async def test_inventory_rechecks_checkpoint_after_actual_names_lookup(operation_store, monkeypatch):
    engine, _repository, authority, sessions, sid = operation_store
    running, stored, _chunk = selected_case(operation_store)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    resolver = ScopedSecretResolver(
        WebSecretService(
            user_store=UserSecretStore(engine=engine, master_key="inventory-test-master-key-32chars!"),
            server_store=ServerSecretStore(allowlist=()),
        ),
        "local",
    )
    advisor = inventory_advisor(sessions, secret_service=resolver)
    positive = await materialize_continuation_env_inventory(advisor=advisor, authority=authority, selected=selected, required_work=work)
    assert positive.names == frozenset()
    original = sessions.get_session
    lookups = []

    async def observed(session_id):
        result = await original(session_id)
        lookups.append(result)
        with engine.begin() as conn:
            append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=stored.checkpoint_id,
                expected_cursor=stored.cursor,
                snapshot_json=stored.payload_json,
                event_batch=(ContinuationEvent("checkpoint", '{"inventory_advanced":true}'),),
                handoff=False,
            )
        return result

    monkeypatch.setattr(sessions, "get_session", observed)
    with pytest.raises(ContinuationCASConflict):
        await materialize_continuation_env_inventory(advisor=advisor, authority=authority, selected=selected, required_work=work)
    assert len(lookups) == 1
    work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ("selection", "inventory"))
@pytest.mark.parametrize("field", ("durable_operation_id", "claim_attempt"))
async def test_same_context_wrong_required_work_claim_refuses_before_reservation(operation_store, owner, field):
    _engine, _repository, authority, sessions, _sid = operation_store
    running, stored, _chunk = selected_case(operation_store)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    work.coordinator.assert_completed()
    changes = {"durable_operation_id": str(uuid4())} if field == "durable_operation_id" else {"claim_attempt": running.claim.attempt + 1}
    foreign = replace(work, coordinator=RequiredWorkCoordinator(replace(work.coordinator.authority, **changes)))
    with pytest.raises(AuditIntegrityError, match="required-work logical claim"):
        if owner == "selection":
            await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=foreign)
        else:
            await materialize_continuation_env_inventory(
                advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=foreign
            )
    assert foreign.coordinator.tickets == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ("selection", "inventory"))
async def test_actual_admission_wait_cancellation_is_one_original_with_no_submission(operation_store, monkeypatch, owner):
    from elspeth.web import async_workers

    _engine, _repository, authority, sessions, _sid = operation_store
    running, stored, _chunk = selected_case(operation_store)
    positive = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=positive)
    advisor = inventory_advisor(sessions)
    await materialize_continuation_env_inventory(advisor=advisor, authority=authority, selected=selected, required_work=positive)
    positive.coordinator.assert_completed()
    work = inventory_work(running)
    entered = asyncio.Event()
    cancellations = []
    calls = []
    actual_admission = async_workers._acquire_admission
    actual_read = authority.load_continuation_chunks if owner == "selection" else authority.read_continuation_env_selection

    async def observe_admission():
        entered.set()
        try:
            await actual_admission()
        except asyncio.CancelledError as original:
            cancellations.append(original)
            raise

    def observe_sql(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_read(*args, **kwargs)

    monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
    monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(async_workers, "_acquire_admission", observe_admission)
    if owner == "selection":
        monkeypatch.setattr(authority, "load_continuation_chunks", observe_sql)
        pending = load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    else:
        monkeypatch.setattr(authority, "read_continuation_env_selection", observe_sql)
        pending = materialize_continuation_env_inventory(advisor=advisor, authority=authority, selected=selected, required_work=work)
    task = asyncio.create_task(pending)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel("actual admission-wait cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert len(cancellations) == 1 and caught.value is cancellations[0]
    assert calls == []
    work.coordinator.assert_completed()
    sql, projection = work.coordinator.tickets
    assert sql.key.source is RequiredWorkSource.CONTINUATION_READ_SQL
    assert projection.key.source is RequiredWorkSource.CONTINUATION_READ_PROJECTION
    assert sql._future is None and sql._no_submission


@pytest.mark.asyncio
async def test_coherent_invalid_utf8_is_owned_integrity_failure_with_original_cause(operation_store):
    engine, _repository, authority, _sessions, _sid = operation_store
    running, stored, chunk = selected_case(operation_store)
    assert authority.load_continuation_chunks(running, stored) == (chunk,)
    payload = bytes((255,))
    with engine.begin() as conn:
        conn.execute(
            update(chunks)
            .where(chunks.c.chunk_key == private_chunk_key(chunk))
            .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
        )
    before = rows(operation_store)
    work = inventory_work(running)
    with pytest.raises(AuditIntegrityError, match="strict UTF-8") as caught:
        await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    assert type(caught.value.__cause__) is UnicodeDecodeError
    assert caught.value.__cause__.object == payload
    assert caught.value.__cause__.encoding == "utf-8"
    assert rows(operation_store) == before
    work.coordinator.assert_completed()
    assert any(receipt.original_root is caught.value for receipt in work.coordinator.failure_receipts())
