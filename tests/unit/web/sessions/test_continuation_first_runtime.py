"""Real first-cut cohort: cancellation cutoff, late read and live replay."""

import asyncio
from copy import deepcopy
from threading import Event

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_composition_initialization as initialization
from elspeth.web.composer import continuation_first_runtime as first
from elspeth.web.composer import continuation_runtime as runtime_owner
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers, _CurrentProducerInputs
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.models import sessions_table as sessions
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_composition_initialization import assert_first_graph, construct, ready_case
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_initialization import _reclaim_pretitle
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_first_cohort_refuses_genuine_ready_source_without_publication(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    try:
        source = await initialization._read_source(service=service, authority=operation_store[2], selected=ready, work=work)
        assert not source.initialized
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="first Compose consumption requires its actual committed initialization"):
            await first.restore_first_continuation_runtime(
                service=service,
                authority=operation_store[2],
                selected=source.selected,
                inventory=source.inventory,
                required_work=work,
                provider_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work),
            )
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 0
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_initial_producer_still_requires_complete_ordered_wire(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    original_initial = CurrentContinuationMessageProducers._initial
    try:
        positive = await construct(operation_store, running, service, work, ready)
        assert_first_graph(positive, service, running)
        selected = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])

        def changed_actual_role(self):
            messages = original_initial(self)
            assert messages[0]["role"] == "system"
            messages[0]["role"] = "assistant"
            return messages

        with monkeypatch.context() as patch:
            patch.setattr(CurrentContinuationMessageProducers, "_initial", changed_actual_role)
            with pytest.raises(AuditIntegrityError, match="current producer differs from complete retained ordered wire"):
                await construct(operation_store, running, service, work, selected)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 0
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_post_shelf_current_refusal_precedes_any_live_callback(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    original_initial, original_live = CurrentContinuationMessageProducers._initial, first._restore_live
    live_calls = []
    try:
        positive = await construct(operation_store, running, service, work, ready)
        assert_first_graph(positive, service, running)
        selected = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])

        def actual_initial_then_archive(self):
            messages = original_initial(self)
            with operation_store[0].begin() as conn:
                conn.execute(update(sessions).where(sessions.c.id == str(operation_store[4])).values(archived_at=database_now(conn)))
            return messages

        def live(**kwargs):
            live_calls.append(kwargs["graph"])
            return original_live(**kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(CurrentContinuationMessageProducers, "_initial", actual_initial_then_archive)
                patch.setattr(first, "_restore_live", live)
                with pytest.raises(ComposerOperationFenceLost):
                    await construct(operation_store, running, service, work, selected)
            assert live_calls == [] and len(sdk) == 0
            work.coordinator.assert_completed()
        finally:
            with operation_store[0].begin() as conn:
                conn.execute(update(sessions).where(sessions.c.id == str(operation_store[4])).values(archived_at=None))
        assert operation_store[2].load_continuation_snapshot(running) == selected
        assert recovery_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("source", "graph"))
async def test_cancelled_completed_graph_handoff_never_enters_live_callback(operation_store, tmp_path, monkeypatch, cut):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    reached, release = Event(), Event()
    original_graph, original_live = first._restore_first_graph, first._restore_live
    original_source, original_initial = first._read_first_sources, CurrentContinuationMessageProducers._initial
    live_calls, cancellations, graph_calls, initial_calls = [], [], [], []
    task = None
    try:
        positive = await construct(operation_store, running, service, work, ready)
        assert_first_graph(positive, service, running)
        selected = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])

        def source_then_wait(**kwargs):
            actual = original_source(**kwargs)
            if cut == "source":
                reached.set()
                assert release.wait(5), "test barrier was not released"
            return actual

        def graph_then_wait(**kwargs):
            graph_calls.append(True)
            actual = original_graph(**kwargs)
            if cut == "graph":
                reached.set()
                assert release.wait(5), "test barrier was not released"
            return actual

        def actual_initial(self):
            initial_calls.append(True)
            return original_initial(self)

        def live(**kwargs):
            live_calls.append(kwargs["graph"])
            return original_live(**kwargs)

        original_projection = first._finish_evidence_projection
        original_read = first._registered_read

        async def actual_source_handoff(*args, **kwargs):
            try:
                return await original_read(*args, **kwargs)
            except asyncio.CancelledError as original:
                cancellations.append(original)
                raise

        def actual_handoff(*args, **kwargs):
            try:
                return original_projection(*args, **kwargs)
            except asyncio.CancelledError as original:
                cancellations.append(original)
                raise

        with monkeypatch.context() as patch:
            patch.setattr(first, "_restore_first_graph", graph_then_wait)
            patch.setattr(first, "_read_first_sources", source_then_wait)
            patch.setattr(first, "_restore_live", live)
            patch.setattr(first, "_finish_evidence_projection", actual_handoff)
            patch.setattr(first, "_registered_read", actual_source_handoff)
            patch.setattr(CurrentContinuationMessageProducers, "_initial", actual_initial)
            task = asyncio.create_task(construct(operation_store, running, service, work, selected))
            async with asyncio.timeout(5):
                while not reached.is_set():
                    if task.done():
                        await task
                        raise AssertionError("graph handoff was not reached")
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert cancellations == [caught.value] and live_calls == []
            assert graph_calls == ([] if cut == "source" else [True])
            assert initial_calls == ([] if cut == "source" else [True])
        work.coordinator.assert_completed()
        assert operation_store[2].load_continuation_snapshot(running) == selected
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 0
        replay = await construct(operation_store, running, service, work, selected)
        assert_first_graph(replay, service, running)
        assert recovery_rows(operation_store[0]) == before
    finally:
        release.set()
        if task is not None and not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_evidence_handoff_keeps_original_failure_and_cancellation_group(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    reached, release = Event(), Event()
    marker = RuntimeError("original graph worker failure")
    original_graph, original_projection = first._restore_first_graph, first._finish_evidence_projection
    original_cancellations = []
    task = None
    try:
        positive = await construct(operation_store, running, service, work, ready)
        assert_first_graph(positive, service, running)
        selected = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])

        def graph_then_fail(**kwargs):
            original_graph(**kwargs)
            reached.set()
            assert release.wait(5), "test barrier was not released"
            raise marker

        def observe_projection(work, sql, projection, outcome, *, expected):
            original_cancellations.extend(outcome.deferred_cancellations)
            return original_projection(work, sql, projection, outcome, expected=expected)

        with monkeypatch.context() as patch:
            patch.setattr(first, "_restore_first_graph", graph_then_fail)
            patch.setattr(first, "_finish_evidence_projection", observe_projection)
            task = asyncio.create_task(construct(operation_store, running, service, work, selected))
            async with asyncio.timeout(5):
                while not reached.is_set():
                    if task.done():
                        await task
                        raise AssertionError("graph failure boundary was not reached")
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(BaseExceptionGroup) as caught:
                await task
            assert caught.value.message == "Current evidence original outcomes"
            assert len(original_cancellations) == 1
            assert caught.value.exceptions == (marker, original_cancellations[0])
            assert caught.value.exceptions[0] is marker and caught.value.exceptions[1] is original_cancellations[0]
        work.coordinator.assert_completed()
        assert operation_store[2].load_continuation_snapshot(running) == selected
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 0
    finally:
        release.set()
        if task is not None and not task.done():
            with pytest.raises(BaseExceptionGroup):
                await task
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_late_runtime_refusal_precedes_schema_disclosure_mutation(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    real_scope = runtime_owner.composition_scope_for_operation
    try:
        positive = await construct(operation_store, running, service, work, ready)
        assert_first_graph(positive, service, running)
        selected = operation_store[2].load_continuation_snapshot(running)
        service._schema_disclosure.mark_plugin_schema_loaded(str(operation_store[4]), "source", "csv")
        disclosure = deepcopy(service._schema_disclosure._schemas_loaded_by_session)
        observed = []

        def change_current_actor_after_scope(*args, **kwargs):
            actual = real_scope(*args, **kwargs)
            # Archival is actual mutable session source, not an immutable job edit.
            with operation_store[0].begin() as conn:
                conn.execute(update(sessions).where(sessions.c.id == str(operation_store[4])).values(archived_at=database_now(conn)))
            observed.append(actual)
            return actual

        real_disclosure = type(service._schema_disclosure).restore_session_for_continuation

        def disclosure_write(self, *args, **kwargs):
            observed.append("disclosure mutation")
            return real_disclosure(self, *args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(runtime_owner, "composition_scope_for_operation", change_current_actor_after_scope)
                patch.setattr(type(service._schema_disclosure), "restore_session_for_continuation", disclosure_write)
                with pytest.raises(ComposerOperationFenceLost):
                    await construct(operation_store, running, service, work, selected)
            assert len(observed) == 1 and observed[0] != "disclosure mutation"
            assert service._schema_disclosure._schemas_loaded_by_session == disclosure
            assert len(sdk) == 0
            work.coordinator.assert_completed()
        finally:
            with operation_store[0].begin() as conn:
                conn.execute(update(sessions).where(sessions.c.id == str(operation_store[4])).values(archived_at=None))
        replay = await construct(operation_store, running, service, work, selected)
        assert_first_graph(replay, service, running)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", (False, True))
async def test_postcommit_callback_failure_observes_live_cut_then_reclaims_without_republication(
    operation_store, tmp_path, monkeypatch, paid
):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=paid)
    marker = RuntimeError("original first-cut callback failure")
    real_initial = CurrentContinuationMessageProducers._initial
    current, now = operation_store, running
    transferred = False

    def original_then_fail(self):
        actual = real_initial(self)
        if type(self.inputs) is _CurrentProducerInputs:
            raise marker
        return actual

    try:
        with monkeypatch.context() as patch:
            patch.setattr(CurrentContinuationMessageProducers, "_initial", original_then_fail)
            with pytest.raises(RuntimeError) as caught:
                await construct(operation_store, running, service, work, ready)
            assert caught.value is marker
        work.coordinator.assert_completed()
        # This observation is inside live custody, before any release/recovery.
        committed = operation_store[2].load_continuation_snapshot(running)
        assert committed.cursor == ready.cursor + 1 and committed.checkpoint_id != ready.checkpoint_id
        assert len(sdk) == int(paid)
        transferred = True
        current, now = _reclaim_pretitle(operation_store, running)
        current_service_owner, _actors = current_service(current)
        fresh = inventory_work(now)
        selected = current[2].load_continuation_snapshot(now)
        before = recovery_rows(current[0])
        result = await construct(current, now, current_service_owner, fresh, selected)
        assert_first_graph(result, current_service_owner, now)
        assert result.source.selected.checkpoint_id == selected.checkpoint_id
        assert result.bindings.session_operation_context is now.session_operation_context
        assert recovery_rows(current[0]) == before and len(sdk) == int(paid)
        fresh.coordinator.assert_completed()
    finally:
        if not transferred or now is not running:
            current[1].release(now.session_operation_context)
