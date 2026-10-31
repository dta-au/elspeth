"""Real selected shelf sources share one locked family window, never clearance.

The native fixture runs the actual primary provider/audit/SQL path with only
external SDK/guard HTTP controlled, then captures the real selected shelf.
The failure fixture uses the actual failed mutation validator at its existing
completed-result input boundary. Neither fixture claims a full worker driver.
"""

import asyncio

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
from elspeth.web.composer import continuation_message_families as families
from elspeth.web.composer.continuation_message_family_recipes import NativeThinkingMessageRecipe
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from tests.unit.web.sessions import test_continuation_message_families as cases
from tests.unit.web.sessions import test_continuation_runtime_provider_history as history
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_rows

operation_store = cases.operation_store


async def actual_native_case(store, tmp_path, monkeypatch):
    arguments, runtime, sdk_calls = await history.prepared_runtime(store, tmp_path, monkeypatch)
    published = await history.publish(store, arguments, runtime)
    current, cold = await history.append_actual_native_and_capture(store, arguments, runtime, published)
    assert len(sdk_calls) == 1
    assert cold.source.admitted_evidence.recorder_chunk.canonical == cold.materialization.inputs.admitted_evidence.recorder_chunk.canonical
    return {
        "kwargs": {name: current[name] for name in ("service", "authority", "selected", "inventory", "required_work")},
        "shelf": cold.source.evidence.transcript,
        "original_messages": cold.live.llm_messages,
    }


def native_request(case):
    matches = tuple(
        (recipe, text)
        for recipe, text in zip(case["shelf"].producer_recipes, case["shelf"].messages_json, strict=True)
        if type(recipe) is NativeThinkingMessageRecipe
    )
    assert len(matches) == 1
    return matches[0]


def current_families(case):
    kwargs = case["kwargs"]
    return families.CurrentContinuationMessageFamilies(kwargs["service"], kwargs["authority"], kwargs["selected"], kwargs["inventory"])


async def renew(store, case):
    context = case["kwargs"]["inventory"].running.session_operation_context
    assert await asyncio.to_thread(store[1].renew, context, lease_seconds=30) is context


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("native", "failure"))
async def test_real_complete_shelf_uses_one_locked_family_window_without_changing_wire_or_rows(
    operation_store, tmp_path, monkeypatch, family
):
    if family == "native":
        case = await actual_native_case(operation_store, tmp_path, monkeypatch)
    else:
        case = await cases.failure_current_case(operation_store)
    connections = []
    original = families._read_current_sources

    def observe(conn, **kwargs):
        assert conn.in_transaction()
        connections.append(conn)
        return original(conn, **kwargs)

    monkeypatch.setattr(families, "_read_current_sources", observe)
    try:
        await renew(operation_store, case)
        production = await cases.restore_case(operation_store, case)
        assert detach_prompt_guard_evidence(production.messages) == detach_prompt_guard_evidence(case["original_messages"])
        # Failure policy acquisition performs one separate original current
        # read before invoking the real snapshot supplier outside the lock.
        assert len(connections) == (3 if family == "failure" else 2)
        assert connections[-2] is connections[-1]
        assert all(not conn.in_transaction() for conn in connections)
        # Every interned message, including observed history, is rebuilt and
        # re-registered at its actual slot; current_indexes only selects output.
        restored = tuple(message for message, _recipe, _wire in production.producers._produced)
        assert len(restored) == sum(recipe is not None for recipe in case["shelf"].producer_recipes)
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_repeated_actual_recipe_has_distinct_messages_and_no_cross_call_source_cache(operation_store, tmp_path, monkeypatch):
    case = await actual_native_case(operation_store, tmp_path, monkeypatch)
    owner = current_families(case)
    request = native_request(case)
    original = families._read_current_sources
    connections = []

    def observe(conn, **kwargs):
        connections.append(conn)
        return original(conn, **kwargs)

    monkeypatch.setattr(families, "_read_current_sources", observe)
    try:
        await renew(operation_store, case)
        before = stored_rows(operation_store[0])
        first = await asyncio.to_thread(owner.restore_messages, (request, request))
        assert first[0] is not first[1]
        assert detach_prompt_guard_evidence([first[0]]) == detach_prompt_guard_evidence([first[1]])
        assert len(connections) == 2 and connections[0] is connections[1]
        second = await asyncio.to_thread(owner.restore_messages, (request,))
        assert second[0] is not first[0]
        assert len(connections) == 4 and connections[2] is connections[3] and connections[2] is not connections[0]
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_final_actual_current_read_refuses_stop_inside_the_same_transaction_after_positive(operation_store, tmp_path, monkeypatch):
    case = await actual_native_case(operation_store, tmp_path, monkeypatch)
    owner = current_families(case)
    request = native_request(case)
    original_read = families._read_current_sources
    original_build = families.rebuild_native_message
    connections = []

    def observe(conn, **kwargs):
        connections.append(conn)
        return original_read(conn, **kwargs)

    def stop_after_build(**kwargs):
        message = original_build(**kwargs)
        assert len(connections) == 1
        conn = connections[0]
        changed = conn.execute(
            update(jobs)
            .where(jobs.c.operation_id == case["kwargs"]["inventory"].running.claim.operation_id)
            .values(cancel_requested_at=database_now(conn))
        )
        assert changed.rowcount == 1
        return message

    try:
        await asyncio.to_thread(owner.restore_messages, (request,))
        await renew(operation_store, case)
        before = stored_rows(operation_store[0])
        monkeypatch.setattr(families, "_read_current_sources", observe)
        monkeypatch.setattr(families, "rebuild_native_message", stop_after_build)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await asyncio.to_thread(owner.restore_messages, (request,))
        assert len(connections) == 2 and connections[0] is connections[1]
        # The deliberate in-transaction mutation rolls back with original Stop.
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_complete_family_wire_check_survives_batching_after_real_positive(operation_store, tmp_path, monkeypatch):
    case = await actual_native_case(operation_store, tmp_path, monkeypatch)
    owner = current_families(case)
    request = native_request(case)
    original = families.rebuild_native_message

    def changed_wire(**kwargs):
        message = original(**kwargs)
        message["content"] += " changed after the actual builder"
        return message

    try:
        await asyncio.to_thread(owner.restore_messages, (request,))
        await renew(operation_store, case)
        before = stored_rows(operation_store[0])
        monkeypatch.setattr(families, "rebuild_native_message", changed_wire)
        with pytest.raises(AuditIntegrityError, match="family cohort producer differs"):
            await asyncio.to_thread(owner.restore_messages, (request,))
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)
