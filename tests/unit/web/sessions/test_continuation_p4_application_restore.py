"""Actual finish-once SQL/projection ownership around cold P4 application."""

import asyncio
from threading import Event

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.composer import continuation_p4_application_restore as application_restore
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_transcript_blob_reader import current_service
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_rows
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case
from tests.unit.web.sessions.test_continuation_p4_application_store import continuation_rows, read_current, second_pending, selected_now
from tests.unit.web.sessions.test_continuation_p4_owner import rows

operation_store = test_composer_operation_authority.operation_store


async def apply_cold(store, selected, inventory, work, *, service=None):
    return await application_restore.apply_selected_p4_to_cold_evidence(
        authority=store[2], selected=selected, inventory=inventory, required_work=work, service=service
    )


@pytest.mark.asyncio
async def test_actual_cold_application_finishes_both_tickets_and_restores_the_shared_graph(operation_store):
    engine, repository, _authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        business = rows(engine)
        work = inventory_work(running)
        actual = await apply_cold(operation_store, selected, inventory, work)
        work.coordinator.assert_completed()
        assert rows(engine) == business
        assert actual.restored.batch is actual.restored.evidence._portable_p4_graph.selected
        assert actual.restored.evidence.effects is actual.restored.evidence._portable_p4_graph.effects
        assert actual.restored.applications.applications[0].effect_key == commit.effect_key
        assert actual.stored.snapshot.cursor == selected.stack.cursor + 1
        assert not decode_continuation_snapshot(actual.stored.snapshot.payload_json).pending_owner_results
        before = stored_rows(engine), rows(engine), continuation_rows(engine)
        await read_current(operation_store, running)
        assert (stored_rows(engine), rows(engine), continuation_rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_cold_application_admission_cancellation_keeps_original_and_submits_nothing(operation_store, monkeypatch):
    engine, repository, authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        positive = inventory_work(running)
        await apply_cold(operation_store, selected, inventory, positive)
        positive.coordinator.assert_completed()
        _running, selected, inventory, _snapshot, _commit = await second_pending(operation_store, running)
        before = stored_rows(engine), rows(engine), continuation_rows(engine)
        entered = asyncio.Event()
        cancellations, submissions = [], []
        admission, apply = async_workers._acquire_admission, authority.apply_continuation_p4_application

        async def observe_admission():
            entered.set()
            try:
                await admission()
            except asyncio.CancelledError as original:
                cancellations.append(original)
                raise

        def observe_apply(*args, **kwargs):
            submissions.append((args, kwargs))
            return apply(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
            patch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
            patch.setattr(async_workers, "_acquire_admission", observe_admission)
            patch.setattr(authority, "apply_continuation_p4_application", observe_apply)
            work = inventory_work(running)
            task = asyncio.create_task(apply_cold(operation_store, selected, inventory, work))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
            finally:
                task.cancel("cold application actual admission cancellation")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert len(cancellations) == 1 and caught.value is cancellations[0]
            assert submissions == []
            work.coordinator.assert_completed()
            sql, _projection = work.coordinator.tickets
            assert sql._future is None and sql._no_submission
        assert (stored_rows(engine), rows(engine), continuation_rows(engine)) == before
        retry = inventory_work(running)
        await apply_cold(operation_store, selected, inventory, retry)
        retry.coordinator.assert_completed()
        assert rows(engine) == before[1]
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("projection_fails", (False, True))
async def test_cancel_after_actual_commit_drains_projection_and_retains_every_original_error(
    operation_store, monkeypatch, projection_fails
):
    engine, repository, authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        business = rows(engine)
        committed, release = Event(), Event()
        apply, restore, finish_once = (
            authority.apply_continuation_p4_application,
            application_restore._restore_current_evidence,
            application_restore.run_required_sql_finish_once,
        )
        submissions, projections, outcomes = [], [], []
        original = ValueError("actual cold projection failure")
        cause = RuntimeError("actual original projection cause")
        original.__cause__ = cause

        def observe_apply(*args, **kwargs):
            submissions.append((args, kwargs))
            result = apply(*args, **kwargs)
            committed.set()
            if not release.wait(timeout=30):
                raise RuntimeError("test did not release committed worker")
            return result

        def observe_restore(*args, **kwargs):
            projections.append((args, kwargs))
            if projection_fails:
                raise original
            return restore(*args, **kwargs)

        async def observe_finish(*args, **kwargs):
            outcome = await finish_once(*args, **kwargs)
            outcomes.append(outcome)
            return outcome

        with monkeypatch.context() as patch:
            patch.setattr(authority, "apply_continuation_p4_application", observe_apply)
            patch.setattr(application_restore, "_restore_current_evidence", observe_restore)
            patch.setattr(application_restore, "run_required_sql_finish_once", observe_finish)
            work = inventory_work(running)
            task = asyncio.create_task(apply_cold(operation_store, selected, inventory, work))
            try:
                assert await asyncio.to_thread(committed.wait, 10)
                task.cancel("after actual application commit")
            finally:
                release.set()
            with pytest.raises(BaseExceptionGroup if projection_fails else asyncio.CancelledError) as caught:
                await task
            assert len(submissions) == len(projections) == len(outcomes) == 1
            deferred = outcomes[0].deferred_cancellations
            assert len(deferred) == 1
            if projection_fails:
                assert caught.value.exceptions == (original, deferred[0])
                assert caught.value.exceptions[0] is original and original.__cause__ is cause
            else:
                assert caught.value is deferred[0]
            assert deferred[0].args == ("after actual application commit",)
            work.coordinator.assert_completed()
        assert rows(engine) == business
        selected_after, _inventory, restored = await read_current(operation_store, running)
        assert not selected_after.stack.pending_owner_results
        assert len(restored.applications.applications) == 1
        assert rows(engine) == business
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_missing_projection_reader_leaves_one_applied_root_recoverable_with_actual_service(operation_store, tmp_path):
    engine, repository, authority, _sessions, _sid = operation_store
    service = current_service(operation_store, tmp_path)
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(
        operation_store, graph=True, joint=True, reader_service=service
    )
    try:
        business = rows(engine)
        work = inventory_work(running)
        with pytest.raises(AuditIntegrityError, match="actual current reader service"):
            await apply_cold(operation_store, selected, inventory, work)
        work.coordinator.assert_completed()
        assert rows(engine) == business
        selected, inventory = await selected_now(operation_store, running)
        assert not selected.stack.pending_owner_results
        before = stored_rows(engine), rows(engine), continuation_rows(engine)
        recovery = inventory_work(running)
        actual = await restore_turn_evidence(
            authority=authority, selected=selected, inventory=inventory, required_work=recovery, service=service
        )
        recovery.coordinator.assert_completed()
        assert actual.evidence.transcript._blob_reader.service is service
        assert actual.evidence.transcript._blob_reader.context is running.session_operation_context
        assert len(actual.applications.applications) == 1
        assert actual.batch is actual.evidence._portable_p4_graph.selected
        assert (stored_rows(engine), rows(engine), continuation_rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)
