"""Paired semantic shelf DATA retains fresh final proof and worker custody."""

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import app as app_module
from elspeth.web.composer import continuation_message_families as families
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer import continuation_semantic_advisor as semantic_advisor
from elspeth.web.composer import continuation_semantic_advisor_phase as semantic_phase
from elspeth.web.composer import tool_batch
from elspeth.web.composer.continuation_message_family_builders import family_message_wire
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.continuation_retention_memo import current_v4_preparation_memo
from elspeth.web.sessions import locking
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    end_admit,
    install_sdk,
    observe_supported_path,
    semantic_primary_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case, stored_rows
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_message_families import failure_current_case, restore_case
from tests.unit.web.sessions.test_continuation_tool_failure import _issued_argument_feedback_case

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "receipt", "evidence_hash"))
async def test_paired_semantic_shelf_reuses_actual_recipes_and_keeps_final_sql_proof(operation_store, tmp_path, monkeypatch, change):
    async with _phase_case(operation_store, tmp_path, monkeypatch, following_tool_call=("feedback", "get_pipeline_state", {})) as (
        owner,
        running,
        primary,
        sdk,
    ):
        await _advance(owner)
        await _advance(owner)
        original_error = ToolArgumentError(argument="arguments", expected="an object", actual_type="invalid value")

        def refuse(*args, **kwargs):
            raise original_error

        with monkeypatch.context() as patch:
            patch.setattr(tool_batch, "execute_tool", refuse)
            await _advance(owner)
        runtime = owner.runtime
        materialization = runtime.materialization
        snapshot = runtime.source.evidence.transcript
        baseline = await asyncio.to_thread(materialization.restore_shelf, snapshot)
        expected = tuple(map(family_message_wire, baseline))
        assert expected == snapshot.messages_json
        before = stored_rows(operation_store[0])
        paired_reads, final_reads, recipe_reads, cohorts, assemblies = [], [], [], [], []
        restore_updates = []
        actual_pair = producers._read_current_inputs_with_tool_sources
        actual_read = producers._read_current_inputs
        actual_recipes = families._admit_selected_tool_recipes_on_connection
        actual_cohort = families.CurrentContinuationMessageFamilies._assemble_paired_cohort_on_connection
        actual_assemble = producers.CurrentContinuationMessageProducers._assemble_shelf

        def paired(conn, **values):
            assert conn.in_transaction()
            actual = actual_pair(conn, **values)
            paired_reads.append((conn, actual))
            assert len(actual.result_sources) == 2 and len(actual.feedback_sources) == 1
            assert actual.inputs.admitted_evidence is not runtime.source.admitted_evidence
            assert all(source.call is not call for source in actual.result_sources for call in runtime.graph.batch.calls)
            return actual

        def final(conn, **values):
            final_reads.append(conn)
            assert assemblies and paired_reads[0][0].closed
            return actual_read(conn, **values)

        def recipes(conn, *args, **values):
            recipe_reads.append(conn)
            return actual_recipes(conn, *args, **values)

        def cohort(self, conn, requests, **values):
            assert paired_reads and conn is paired_reads[0][0]
            actual = paired_reads[0][1]
            assert values["admitted"] is actual.inputs.admitted_evidence
            assert values["result_sources"] is actual.result_sources
            assert values["feedback_sources"] is actual.feedback_sources
            result = actual_cohort(self, conn, requests, **values)
            cohorts.append(result)
            assert result.sources is actual.feedback_sources
            return result

        def assemble(self, supplied, **values):
            assert paired_reads[0][0].closed and cohorts
            assert values["family_cohort"] is cohorts[0]
            result = actual_assemble(self, supplied, **values)
            assemblies.append(result)
            sid, operation = str(running.claim.session_id), running.claim.operation_id
            if change == "receipt":
                key = f"tool:{primary.response.reference.digest}:1"
                where = (effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key)
                with operation_store[0].begin() as conn:
                    original = conn.execute(select(effects.c.receipt_key).where(*where)).scalar_one()
                    conn.execute(update(effects).where(*where).values(receipt_key="tool-result:" + "0" * 64))
                restore_updates.append(update(effects).where(*where).values(receipt_key=original))
            elif change == "evidence_hash":
                reference = runtime.source.selected.stack.frames[0].evidence
                key = f"value:{reference.codec}:{reference.digest}"
                where = (chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
                with operation_store[0].begin() as conn:
                    original = conn.execute(select(chunks.c.sha256).where(*where)).scalar_one()
                    conn.execute(update(chunks).where(*where).values(sha256="0" * 64))
                restore_updates.append(update(chunks).where(*where).values(sha256=original))
            return result

        try:
            with monkeypatch.context() as patch:
                patch.setattr(producers, "_read_current_inputs_with_tool_sources", paired)
                patch.setattr(producers, "_read_current_inputs", final)
                patch.setattr(families, "_admit_selected_tool_recipes_on_connection", recipes)
                patch.setattr(families.CurrentContinuationMessageFamilies, "_assemble_paired_cohort_on_connection", cohort)
                patch.setattr(producers.CurrentContinuationMessageProducers, "_assemble_shelf", assemble)
                if change == "none":
                    result = await asyncio.to_thread(materialization._restore_shelf_for_semantic_request, snapshot)
                    assert tuple(map(family_message_wire, result)) == expected
                    assert len(recipe_reads) == 2  # one acquisition, one fresh final proof
                else:
                    with pytest.raises(AuditIntegrityError):
                        await asyncio.to_thread(materialization._restore_shelf_for_semantic_request, snapshot)
                assert len(paired_reads) == len(final_reads) == len(cohorts) == len(assemblies) == 1
        finally:
            with operation_store[0].begin() as conn:
                for statement in restore_updates:
                    conn.execute(statement)
        restored = await asyncio.to_thread(materialization._restore_shelf_for_semantic_request, snapshot)
        assert tuple(map(family_message_wire, restored)) == expected
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        assert runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_shelf_keeps_actual_owed_feedback_fallback(operation_store, tmp_path, monkeypatch):
    async with _issued_argument_feedback_case(operation_store, tmp_path, monkeypatch) as values:
        owner, running, _primary, sdk, _original, _cause, _dispatches = values
        materialization = owner.runtime.materialization
        snapshot = owner.runtime.source.evidence.transcript
        owner.authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        baseline = await asyncio.to_thread(materialization.restore_shelf, snapshot)
        assert materialization.finish_read is not None
        fallback = []
        actual = producers.CurrentContinuationMessageProducers._rebuild_shelf

        def rebuild(self, supplied, *, inputs):
            fallback.append(inputs)
            return actual(self, supplied, inputs=inputs)

        with monkeypatch.context() as patch:
            patch.setattr(producers.CurrentContinuationMessageProducers, "_rebuild_shelf", rebuild)
            restored = await asyncio.to_thread(materialization._restore_shelf_for_semantic_request, snapshot)
        assert len(fallback) == 1
        assert tuple(map(family_message_wire, restored)) == tuple(map(family_message_wire, baseline))
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_semantic_shelf_keeps_actual_failure_schema_policy_fallback(operation_store, monkeypatch):
    case = await failure_current_case(operation_store)
    running = case["kwargs"]["inventory"].running
    try:
        current = await restore_case(operation_store, case)
        materialization, snapshot = current.producers, case["shelf"]
        baseline = await asyncio.to_thread(materialization.restore_shelf, snapshot)
        fallback = []
        actual = producers.CurrentContinuationMessageProducers._rebuild_shelf

        def rebuild(self, supplied, *, inputs):
            fallback.append(inputs)
            return actual(self, supplied, inputs=inputs)

        with monkeypatch.context() as patch:
            patch.setattr(producers.CurrentContinuationMessageProducers, "_rebuild_shelf", rebuild)
            restored = await asyncio.to_thread(materialization._restore_shelf_for_semantic_request, snapshot)
        assert len(fallback) == 1
        assert tuple(map(family_message_wire, restored)) == tuple(map(family_message_wire, baseline))
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_real_semantic_builder_uses_registered_worker_and_original_sql_owner(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    operation = str(uuid4())
    observed = []
    current_parents, parents, finished = [], [], []
    actual_capture = semantic_phase.capture_semantic_advisor_source
    actual_prepare = semantic_phase.prepare_semantic_advisor_request
    actual_current = semantic_phase.ContinuationLivePhases.current
    actual_finish = semantic_advisor.finish_v4_preparation_memo

    async def current(self, *args, **values):
        parent = current_v4_preparation_memo(self.action_clock)
        if parent is not None:
            current_parents.append(parent)
        return await actual_current(self, *args, **values)

    def finish(clock):
        parent = current_v4_preparation_memo(clock)
        assert parent is parents[-1] and parent.active
        actual_finish(clock)
        assert not parent.active and parent.cached_entry_count == 0
        assert current_v4_preparation_memo() is None
        finished.append(parent)

    def capture(runtime):
        work = runtime.bindings.required_work
        assert work is not None
        scope = locking._CONTINUATION_SQL_DEADLINE.get()
        assert scope == (work.coordinator.sql_deadline_owner, False) and scope[0] is not None
        assert threading.get_ident() != loop_thread
        parent = current_v4_preparation_memo()
        assert parent is not None and parent is current_parents[-1]
        assert all(parent is not previous for previous in parents)
        parents.append(parent)
        result = actual_capture(runtime)
        observed.append(("capture", runtime.source.selected.checkpoint_id, threading.get_ident(), scope[0]))
        return result

    def prepare(**values):
        runtime = values["runtime"]
        scope = locking._CONTINUATION_SQL_DEADLINE.get()
        assert scope is not None and scope[1] is False and threading.get_ident() != loop_thread
        assert observed[-1] == ("capture", runtime.source.selected.checkpoint_id, threading.get_ident(), scope[0])
        assert current_v4_preparation_memo() is parents[-1]
        result = actual_prepare(**values)
        result.verify()
        observed.append(("request", runtime.source.selected.checkpoint_id, threading.get_ident(), scope[0]))
        return result

    def policy(settings):
        result = continuation_policy_v4_from_settings(settings)
        assert result is not None and result.actions.advisor.preparation == 5.0
        return result

    monkeypatch.setattr(app_module, "continuation_policy_from_settings", policy)
    monkeypatch.setattr(semantic_phase, "capture_semantic_advisor_source", capture)
    monkeypatch.setattr(semantic_phase, "prepare_semantic_advisor_request", prepare)
    monkeypatch.setattr(semantic_phase.ContinuationLivePhases, "current", current)
    monkeypatch.setattr(semantic_advisor, "finish_v4_preparation_memo", finish)
    app_ref = []
    steps = [
        ("primary", lambda app: ("Inspecting the current graph.", [tool_call(app, "paired_read", "get_pipeline_state", {})])),
        ("semantic", semantic_primary_response),
        ("primary", ("The current pipeline has no configured nodes.", None)),
        ("semantic", semantic_review_response),
        ("end", end_admit()),
        ("semantic", semantic_stop_response),
    ]
    sdk = ScriptedSDK(steps, app_ref=app_ref)

    async def checked_sdk(**values):
        if sdk.steps[0][0] == "semantic":
            assert finished[-1] is parents[-1] and not parents[-1].active
            assert current_v4_preparation_memo() is None
        return await sdk(**values)

    install_sdk(monkeypatch, checked_sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        app_ref.append(harness.app)
        with observe_supported_path(
            monkeypatch,
            operation_id=operation,
            session_id=harness.session_id,
            artifact=tmp_path / "semantic-builder-driver.json",
        ):
            result = await submit_and_settle(
                harness, message_body("Inspect the pipeline and explain its current state.", operation_id=operation), rounds=720
            )
            assert result.status == "completed", result.error
            assert sdk.roles == ["primary", "semantic", "primary", "semantic", "end", "semantic"] and not sdk.steps
    assert [kind for kind, *_rest in observed] == ["capture", "request"] * 3
    assert finished == parents and len(parents) == 3
    assert all(not parent.active and parent.cached_entry_count == 0 for parent in parents)
    assert current_v4_preparation_memo() is None
    assert locking._CONTINUATION_SQL_DEADLINE.get() is None
