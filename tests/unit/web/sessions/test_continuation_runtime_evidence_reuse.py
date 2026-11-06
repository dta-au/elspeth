"""Runtime reads retain their one real SQL evidence result and late live guard."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_message_families as families
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer.continuation_message_family_builders import family_message_wire
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.sessions import continuation_next_iteration as post_batch
from elspeth.web.sessions import continuation_runtime as source_owner
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.coordination.test_continuation_p4_recovery_start import recovery_rows
from tests.unit.web.sessions import test_continuation_runtime as runtime_cases
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case, stored_rows

operation_store = runtime_cases.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "evidence_hash"))
async def test_runtime_pairs_actual_metadata_results_before_callbacks_and_keeps_final_proof(operation_store, tmp_path, monkeypatch, change):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        # The current metadata tail commits both results and P4 together.
        # Restore that real applied cut before observing another cold restore.
        await _advance(owner)
        assert owner.latest_snapshot is not None and owner.runtime is None
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        assert post_batch.is_applied_batch_cut(selected)
        runtime, _inventory = await owner.current(selected)
        assert runtime is owner.runtime and runtime.source.selected is selected
        expected = tuple(map(family_message_wire, runtime.live.llm_messages))
        assert expected == runtime.source.evidence.transcript.messages_json
        before = stored_rows(operation_store[0])
        paired, cohorts, assemblies, final_reads, recipe_reads = [], [], [], [], []
        actual_pair = source_owner._read_current_inputs_with_tool_sources
        actual_final = source_owner._read_current_inputs
        actual_recipes = families._admit_selected_tool_recipes_on_connection
        actual_cohort = families.CurrentContinuationMessageFamilies._assemble_paired_cohort_on_connection
        actual_assemble = producers.CurrentContinuationMessageProducers._assemble_shelf
        restore_updates = []

        def acquisition(conn, **values):
            assert conn.in_transaction()
            result = actual_pair(conn, **values)
            assert len(result.result_sources) == 2 and result.feedback_sources == ()
            paired.append((conn, result))
            return result

        def recipes(conn, *args, **values):
            recipe_reads.append(conn)
            return actual_recipes(conn, *args, **values)

        def cohort(self, conn, requests, **values):
            assert len(paired) == 1 and conn is paired[0][0] and conn.in_transaction()
            actual = paired[0][1]
            assert values["admitted"] is actual.inputs.admitted_evidence
            assert values["result_sources"] is actual.result_sources
            assert values["feedback_sources"] is actual.feedback_sources
            result = actual_cohort(self, conn, requests, **values)
            cohorts.append(result)
            return result

        def assemble(self, snapshot, **values):
            assert paired[0][0].closed and len(cohorts) == 1
            assert values["family_cohort"] is cohorts[0]
            result = actual_assemble(self, snapshot, **values)
            assemblies.append(result)
            if change == "evidence_hash":
                reference = runtime.source.selected.stack.frames[0].evidence
                where = (
                    chunks.c.session_id == str(running.claim.session_id),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                )
                with operation_store[0].begin() as conn:
                    original = conn.execute(select(chunks.c.sha256).where(*where)).scalar_one()
                    changed = conn.execute(update(chunks).where(*where).values(sha256="0" * 64))
                    assert changed.rowcount == 1
                restore_updates.append(update(chunks).where(*where).values(sha256=original))
            return result

        def final(conn, **values):
            assert len(assemblies) == 1 and paired[0][0].closed
            assert values["_prior_evidence"] is paired[0][1].inputs.admitted_evidence
            final_reads.append(conn)
            return actual_final(conn, **values)

        arguments = {
            "service": owner.service,
            "authority": owner.authority,
            "selected": runtime.source.selected,
            "inventory": runtime.materialization.inventory,
            "required_work": owner.work,
            "provider_owner": owner.provider_owner,
        }
        try:
            with monkeypatch.context() as patch:
                patch.setattr(source_owner, "_read_current_inputs_with_tool_sources", acquisition)
                patch.setattr(source_owner, "_read_current_inputs", final)
                patch.setattr(families, "_admit_selected_tool_recipes_on_connection", recipes)
                patch.setattr(families.CurrentContinuationMessageFamilies, "_assemble_paired_cohort_on_connection", cohort)
                patch.setattr(producers.CurrentContinuationMessageProducers, "_assemble_shelf", assemble)
                if change == "none":
                    restored = await restore_continuation_runtime(**arguments)
                    assert tuple(map(family_message_wire, restored.live.llm_messages)) == expected
                    assert restored.source.admitted_evidence is paired[0][1].inputs.admitted_evidence
                    assert len(recipe_reads) == 2
                else:
                    with pytest.raises(AuditIntegrityError):
                        await restore_continuation_runtime(**arguments)
                assert len(paired) == len(cohorts) == len(assemblies) == len(final_reads) == 1
        finally:
            with operation_store[0].begin() as conn:
                for statement in restore_updates:
                    conn.execute(statement)
        fresh = await restore_continuation_runtime(**arguments)
        assert tuple(map(family_message_wire, fresh.live.llm_messages)) == expected
        if change == "none":
            assert fresh.graph is not restored.graph and fresh.graph.messages is not restored.graph.messages
            assert all(new is not old for new, old in zip(fresh.live.llm_messages, restored.live.llm_messages, strict=True))
        assert fresh.graph is not runtime.graph and fresh.graph.messages is not runtime.graph.messages
        assert all(new is not old for new, old in zip(fresh.live.llm_messages, runtime.live.llm_messages, strict=True))
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_runtime_stop_after_paired_acquisition_refuses_before_shelf_reconstruction(operation_store, monkeypatch):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    authority = arguments["authority"]
    running = arguments["inventory"].running
    actual = authority.read_continuation_runtime_restoration
    assemble = producers.CurrentContinuationMessageProducers._assemble_shelf
    acquired, assembled, cancelled_rows = [], [], []

    def stop_after_read(*args, **values):
        result = actual(*args, **values)
        assert result.paired_shelf is not None
        acquired.append(result)
        authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        cancelled_rows.append(recovery_rows(operation_store[0]))
        return result

    def observe_assembly(self, *args, **values):
        assembled.append(self)
        return assemble(self, *args, **values)

    monkeypatch.setattr(authority, "read_continuation_runtime_restoration", stop_after_read)
    monkeypatch.setattr(producers.CurrentContinuationMessageProducers, "_assemble_shelf", observe_assembly)
    try:
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await restore_continuation_runtime(**arguments)
        assert len(acquired) == 1 and not assembled
        assert recovery_rows(operation_store[0]) == cancelled_rows[0]
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_restore_final_read_reuses_only_prior_snapshot_with_fresh_source_admission(operation_store, monkeypatch):
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime

    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    authority = arguments["authority"]
    original_read = authority.read_continuation_runtime
    original_restoration = authority.read_continuation_runtime_restoration
    reads = []

    def observed_read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        reads.append((kwargs.get("_prior_evidence"), result))
        return result

    def observed_restoration(*args, **kwargs):
        result = original_restoration(*args, **kwargs)
        reads.append((None, result.source))
        return result

    monkeypatch.setattr(authority, "read_continuation_runtime", observed_read)
    monkeypatch.setattr(authority, "read_continuation_runtime_restoration", observed_restoration)
    try:
        restored = await restore_continuation_runtime(**arguments)
        repeated = [(prior, result) for prior, result in reads if prior is not None]
        assert len(repeated) == 1
        prior, final_source = repeated[0]
        assert prior is restored.source.admitted_evidence
        assert final_source is not restored.source
        assert final_source.admitted_evidence is not prior
        assert final_source.evidence is prior.snapshot
        assert final_source.admitted_evidence._graph_lock is not prior._graph_lock
        assert len(reads) == 2 and reads[0] == (None, restored.source)
        with pytest.raises(AuditIntegrityError, match="runtime read requires its actual current and selected owners"):
            await asyncio.to_thread(
                original_read,
                replace(running),
                selected=arguments["selected"],
                inventory=arguments["inventory"],
                _prior_evidence=prior,
            )
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_runtime_source_retains_its_actual_validated_evidence_and_rejects_another_running_owner(operation_store, monkeypatch):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    selected = arguments["selected"]
    original = producers.read_turn_evidence_on_connection
    original_constructor = SelectedContinuationState.__post_init__
    observed = []
    constructors = []

    def capture(*args, **kwargs):
        evidence = original(*args, **kwargs)
        observed.append(evidence)
        return evidence

    def validate_selection(self):
        constructors.append(self)
        original_constructor(self)

    monkeypatch.setattr(producers, "read_turn_evidence_on_connection", capture)
    monkeypatch.setattr(SelectedContinuationState, "__post_init__", validate_selection)
    try:
        calibrated = replace(selected)
        assert calibrated == selected and calibrated is not selected
        assert len(constructors) == 1 and constructors[0] is calibrated
        before = recovery_rows(operation_store[0])
        result = await asyncio.to_thread(
            arguments["authority"].read_continuation_runtime,
            running,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
        )
        assert len(observed) == 1 and result.evidence is observed[0].snapshot
        assert result.selected is arguments["selected"]
        assert all(owner is not selected for owner in constructors)
        assert recovery_rows(operation_store[0]) == before
        another = replace(running)
        assert another == running and another is not running
        with pytest.raises(AuditIntegrityError, match="runtime read requires its actual current and selected owners"):
            await asyncio.to_thread(
                arguments["authority"].read_continuation_runtime,
                another,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
            )
        assert all(owner is not selected for owner in constructors)

        # Supported changes construct a new owner and still run its real guard.
        with pytest.raises(AuditIntegrityError, match="continuation snapshot violates its owned codec"):
            replace(selected, stack=selected.stack.model_copy(update={"cursor": -1}))
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
