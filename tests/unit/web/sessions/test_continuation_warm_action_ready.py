"""Warm action-ready refresh keeps two current proofs in one physical memo."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from threading import Event

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.continuation_retention_memo import RetentionStageMemo, current_retention_stage_memo, retention_stage_memo_scope
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions import continuation_runtime as sql_owner
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.unit.web.sessions import test_continuation_runtime as runtime_cases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = runtime_cases.operation_store


@asynccontextmanager
async def _warm_cut(store):
    arguments, _history, _attachments = await runtime_cases.runtime_case(store)
    runtime = await restore_continuation_runtime(**arguments)
    running = arguments["inventory"].running
    work = arguments["required_work"]
    adoption = ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
    )
    lease = await adoption.join()
    try:
        owner = ContinuationLivePhases(
            arguments["service"], arguments["authority"], work, arguments["provider_owner"], lease, progress=runtime.bindings.progress
        )
        owner.bind_initial(runtime)
        captured = await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        following = await runtime_cases.install_capture(store, arguments, captured)
        selected = following["selected"]
        # The existing complete owner-value writer, not an edited frame, made
        # this genuine later SQL checkpoint. The live owners still bind its
        # actual predecessor, as at the post-iteration warm refresh boundary.
        assert selected.stack.frames[-1].phase == "action_ready"
        assert selected.stack.cursor > runtime.source.selected.stack.cursor
        assert owner.runtime is runtime and runtime.source.selected is arguments["selected"]
        yield owner, runtime, selected, running
    finally:
        await lease.close()


def _observe_sql(monkeypatch):
    original = sql_owner.read_runtime_source_on_connection
    observed = []

    def read(conn, *args, **kwargs):
        memo = current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo and memo.active
        observed.append((memo, conn))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
    return observed


def _assert_closed(observed, count):
    assert len(observed) == count
    memo = observed[0][0]
    assert all(actual is memo and conn.closed for actual, conn in observed)
    assert not memo.active and memo.cached_entry_count == 0
    if count == 2:
        assert observed[0][1] is not observed[1][1]


@pytest.mark.asyncio
async def test_warm_changed_action_ready_keeps_two_sql_proofs_and_actual_live_owners(operation_store, monkeypatch):
    async with _warm_cut(operation_store) as (owner, original, selected, _running):
        observed = _observe_sql(monkeypatch)
        before = stored_rows(operation_store[0])
        with retention_stage_memo_scope() as outer:
            refreshed, inventory = await owner.current(selected)
            assert current_retention_stage_memo() is outer and outer.active
            assert observed[0][0] is not outer
        _assert_closed(observed, 2)
        assert refreshed is owner.runtime and refreshed is not original
        assert refreshed.source.selected is selected and refreshed.materialization.selected is selected
        assert refreshed.materialization.inventory is inventory
        assert refreshed.materialization is original.materialization
        assert refreshed.live is original.live and refreshed.bindings is original.bindings
        assert refreshed.graph is original.graph
        assert stored_rows(operation_store[0]) == before
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "sql-bytes", "cas", "timing"])
async def test_warm_action_ready_rechecks_current_source_before_install(operation_store, monkeypatch, change):
    async with _warm_cut(operation_store) as (owner, original, selected, running):
        observed = _observe_sql(monkeypatch)
        actual = owner.authority.read_continuation_runtime
        reads = []
        after_change = []
        old_bindings = (original.materialization.selected, original.materialization.inventory, original.materialization.inputs)

        def read(*args, **kwargs):
            result = actual(*args, **kwargs)
            reads.append(result)
            if len(reads) == 1:
                if change == "stop":
                    owner.authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
                elif change in ("sql-bytes", "cas"):
                    with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
                        if change == "sql-bytes":
                            reference = selected.stack.frames[0].caches
                            chunk = next(value for value in selected.chunks if value.reference == reference)
                            payload = chunk.canonical.encode("utf-8") + b" "
                            changed = conn.execute(
                                update(chunks)
                                .where(
                                    chunks.c.session_id == str(running.claim.session_id),
                                    chunks.c.operation_id == running.claim.operation_id,
                                    chunks.c.chunk_key == private_chunk_key(chunk),
                                )
                                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                            )
                            assert changed.rowcount == 1
                        else:
                            append_complete_snapshot(
                                conn,
                                session_id=str(running.claim.session_id),
                                operation_id=running.claim.operation_id,
                                expected_checkpoint_id=selected.checkpoint_id,
                                expected_cursor=selected.stack.cursor,
                                snapshot_json=encode_continuation_snapshot(selected.stack),
                                event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"next actual complete owner cut"}'),),
                                handoff=False,
                            )
                after_change.append(stored_rows(operation_store[0]))
            elif change == "timing":
                result = replace(result, timing=replace(result.timing, hard_remaining_seconds=result.timing.hard_remaining_seconds + 10))
            return result

        monkeypatch.setattr(owner.authority, "read_continuation_runtime", read)
        expected = ComposerOperationCancelledDuringTurn if change == "stop" else AuditIntegrityError
        with pytest.raises(expected):
            await owner.current(selected)
        _assert_closed(observed, 2)
        assert owner.runtime is original
        assert (original.materialization.selected, original.materialization.inventory, original.materialization.inputs) == old_bindings
        assert stored_rows(operation_store[0]) == after_change[0]
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("at_read", [1, 2])
async def test_warm_action_ready_closes_memo_and_keeps_original_read_failure(operation_store, monkeypatch, at_read):
    async with _warm_cut(operation_store) as (owner, original, selected, _running):
        observed = _observe_sql(monkeypatch)
        actual = owner.authority.read_continuation_runtime
        marker = RuntimeError("original actual warm current read failure")
        reads = []
        before = stored_rows(operation_store[0])

        def read(*args, **kwargs):
            result = actual(*args, **kwargs)
            reads.append(result)
            if len(reads) == at_read:
                raise marker
            return result

        monkeypatch.setattr(owner.authority, "read_continuation_runtime", read)
        with pytest.raises(RuntimeError) as caught:
            await owner.current(selected)
        assert caught.value is marker and owner.runtime is original
        _assert_closed(observed, at_read)
        assert stored_rows(operation_store[0]) == before
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_warm_action_ready_joins_cancelled_physical_read_and_retains_its_actual_bindings(operation_store, monkeypatch):
    async with _warm_cut(operation_store) as (owner, original, selected, _running):
        observed = _observe_sql(monkeypatch)
        actual = owner.authority.read_continuation_runtime
        entered, release = Event(), Event()
        reads = []
        before = stored_rows(operation_store[0])

        def read(*args, **kwargs):
            result = actual(*args, **kwargs)
            reads.append(result)
            if len(reads) == 2:
                entered.set()
                assert release.wait(5)
            return result

        monkeypatch.setattr(owner.authority, "read_continuation_runtime", read)
        task = asyncio.create_task(owner.current(selected))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel("actual warm current read cancellation")
            await asyncio.sleep(0)
            assert not task.done() and observed[-1][0].active
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError, match="actual warm current read cancellation"):
            await task
        _assert_closed(observed, 2)
        assert owner.runtime.source.selected is selected and owner.runtime.live is original.live
        assert owner.runtime.materialization is original.materialization
        assert owner.runtime.materialization.selected is selected
        assert stored_rows(operation_store[0]) == before
    owner.work.coordinator.assert_completed()
