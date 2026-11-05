"""Current evidence keeps pure repeated scans local to one real SQL owner."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from hashlib import sha256
from threading import Event

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
from elspeth.web.composer import continuation_current_evidence as bridge
from elspeth.web.composer.continuation_codec import verify_private_chunk
from elspeth.web.composer.continuation_current_evidence import restore_current_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _finish_copied_current_evidence
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.continuation_retention_memo import (
    RetentionStageMemo,
    borrow_v4_preparation_memo_scope,
    current_retention_stage_memo,
    current_v4_preparation_memo,
    v4_preparation_memo_scope,
)
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_case
from tests.unit.web.sessions.test_continuation_current_evidence import guarded_case
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_current_evidence_reuses_pure_checks_only_within_each_complete_read(operation_store, monkeypatch) -> None:
    from elspeth.web.composer import continuation_codec as codec

    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    original = CurrentContinuationMessageProducers._require_current
    original_env = kwargs["authority"].read_continuation_env_selection
    original_graph = bridge._finish_copied_current_evidence
    original_proof = codec._private_chunk_canonical_text_bytes
    observations = []
    proofs = []
    phase = "before"

    def observed_proof(key):
        proofs.append((current_retention_stage_memo(), key.canonical))
        return original_proof(key)

    def observed_graph(*args, **values):
        nonlocal phase
        assert phase == "before"
        phase = "graph"
        try:
            return original_graph(*args, **values)
        finally:
            phase = "after"

    def observed(self):
        assert self is materialization
        memo = current_retention_stage_memo()
        assert memo is not None and memo.active
        before = memo.cached_entry_count
        result = original(self)
        observations.append((phase, memo, before, memo.cached_entry_count))
        source = result.admitted_evidence.source_chunk
        source_proofs = [(owner, text) for owner, text in proofs if owner is memo and text == source.canonical]
        assert source_proofs == [(memo, source.canonical)]
        cached = memo.cached_entry_count
        proof_count = len(proofs)
        verify_private_chunk(source.reference, source.canonical)
        assert len(proofs) == proof_count
        assert memo.cached_entry_count == cached
        return result

    def observed_env(*args, **values):
        assert phase == "before"
        memo = current_retention_stage_memo()
        assert memo is not None and memo.active
        before = memo.cached_entry_count
        result = original_env(*args, **values)
        assert result == materialization.inventory.selection
        observations.append((phase, memo, before, memo.cached_entry_count))
        return result

    monkeypatch.setattr(bridge, "_finish_copied_current_evidence", observed_graph)
    monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", observed)
    monkeypatch.setattr(kwargs["authority"], "read_continuation_env_selection", observed_env)
    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", observed_proof)
    try:
        assert current_retention_stage_memo() is None
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert len(observations) == 2
        assert [entry[0] for entry in observations] == ["before", "after"]
        first_memo = observations[0][1]
        assert all(entry[1] is first_memo for entry in observations)
        assert observations[-1][3] > 0
        assert not first_memo.active and first_memo.cached_entry_count == 0
        assert current_retention_stage_memo() is None

        observations.clear()
        phase = "before"
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert len(observations) == 2
        assert [entry[0] for entry in observations] == ["before", "after"]
        assert all(entry[1] is observations[0][1] for entry in observations)
        assert observations[0][1] is not first_memo
        assert observations[-1][3] > 0
        assert not observations[0][1].active and current_retention_stage_memo() is None
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_evidence_second_sql_proof_refuses_changed_chunk_bytes(operation_store, monkeypatch) -> None:
    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    frame = kwargs["selected"].stack.frames[-1]
    reference = frame.evidence
    original = CurrentContinuationMessageProducers._require_current
    original_graph = bridge._finish_copied_current_evidence
    original_env = kwargs["authority"].read_continuation_env_selection
    calls = []
    graph_done = False
    changed = b"{}"

    def observed(self):
        assert self is materialization and current_retention_stage_memo() is not None
        calls.append(graph_done)
        return original(self)

    def observed_env(*args, **values):
        calls.append(graph_done)
        result = original_env(*args, **values)
        assert result == materialization.inventory.selection
        return result

    def change_after_graph(*args, **values):
        nonlocal graph_done
        restored = original_graph(*args, **values)
        with operation_store[0].begin() as conn:
            result = conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(running.claim.session_id),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                )
                .values(payload=changed, sha256=sha256(changed).hexdigest(), byte_length=len(changed))
            )
            assert result.rowcount == 1
        graph_done = True
        return restored

    monkeypatch.setattr(bridge, "_finish_copied_current_evidence", change_after_graph)
    monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", observed)
    monkeypatch.setattr(kwargs["authority"], "read_continuation_env_selection", observed_env)
    try:
        with pytest.raises(AuditIntegrityError, match="cold evidence storage differs from its selected bytes"):
            await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert calls == [False, True]
        assert current_retention_stage_memo() is None
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_evidence_memo_does_not_clear_changed_credential_policy(operation_store, monkeypatch) -> None:
    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    original = CurrentContinuationMessageProducers._require_current
    observed_policy = []

    def observed(self):
        assert self is materialization and current_retention_stage_memo() is not None
        if not observed_policy:
            value = {"operator_note": "ordinary text"}
            require_no_credential_material(value, surface="current-evidence-memo-policy")
            with pytest.raises(CredentialMaterialRefused):
                require_no_credential_material(
                    value, surface="current-evidence-memo-policy", additional_credential_fields={"operator_note"}
                )
            observed_policy.append(True)
        return original(self)

    monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", observed)
    try:
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert observed_policy == [True] and current_retention_stage_memo() is None
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_v4_current_evidence_borrows_text_parent_but_restores_fresh_sql_and_mutable_owners(
    operation_store, tmp_path, monkeypatch
) -> None:
    async with _early_case(operation_store, tmp_path, monkeypatch) as (store, phases, _running, selected, primary_sdk):
        runtime, clock = phases.runtime, phases.action_clock
        assert runtime is not None and clock is not None
        materialization = runtime.materialization
        actual_scope = borrow_v4_preparation_memo_scope
        actual_read = CurrentContinuationMessageProducers._require_current
        actual_graph = _finish_copied_current_evidence
        actual_env = phases.authority.read_continuation_env_selection
        children, closed, observed, transferred = [], [], [], []
        prechecks = []
        phase = "before"

        @contextmanager
        def observed_scope():
            with actual_scope() as child:
                assert child._text_parent is parent and current_v4_preparation_memo(clock) is parent
                assert child.cached_entry_count == 0
                assert not child._snapshots and not child._chunks and not child._credentials and not child._states
                children.append(child)
                yield child
            assert not child.active and child.cached_entry_count == 0
            closed.append(child)

        def read(self):
            assert self is materialization and current_retention_stage_memo() is children[-1]
            value = actual_read(self)
            observed.append((phase, children[-1], value))
            return value

        def read_env(*args, **values):
            assert phase == "before" and current_retention_stage_memo() is children[-1]
            value = actual_env(*args, **values)
            assert value == materialization.inventory.selection
            prechecks.append(children[-1])
            return value

        def graph(admitted, **values):
            nonlocal phase
            assert phase == "before" and prechecks[-1] is children[-1]
            assert admitted is materialization.inputs.admitted_evidence
            phase = "graph"
            try:
                result = actual_graph(admitted, **values)
                transferred.append(admitted)
                return result
            finally:
                phase = "after"

        monkeypatch.setattr(bridge, "borrow_v4_preparation_memo_scope", observed_scope)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", read)
        monkeypatch.setattr(phases.authority, "read_continuation_env_selection", read_env)
        monkeypatch.setattr(bridge, "_finish_copied_current_evidence", graph)
        before = stored_rows(store[0])
        with v4_preparation_memo_scope(clock) as parent:
            first = await restore_current_turn_evidence(
                service=phases.service,
                authority=phases.authority,
                selected=selected,
                inventory=materialization.inventory,
                materialization=materialization,
                required_work=phases.work,
            )
            first_messages = canonical_json(detach_prompt_guard_evidence(first.messages).messages)
            first_recorder = first.recorder.snapshot_for_continuation(drained=first.drained)
            assert first.messages and first_recorder.llm_calls
            first.messages[0]["content"] = "Changed only on the first restored owner."
            # Remove an actual retained audit from this isolated mutable owner;
            # no replacement provider audit or source authority is invented.
            with first.recorder._lock:
                assert first.recorder._llm_calls.pop() == first_recorder.llm_calls[-1]
            phase = "before"
            second = await restore_current_turn_evidence(
                service=phases.service,
                authority=phases.authority,
                selected=selected,
                inventory=materialization.inventory,
                materialization=materialization,
                required_work=phases.work,
            )
            assert canonical_json(detach_prompt_guard_evidence(second.messages).messages) == first_messages
            assert second.recorder.snapshot_for_continuation(drained=second.drained) == first_recorder
            assert first.messages is not second.messages and first.recorder is not second.recorder
            assert first.evidence is not second.evidence and first.evidence.transcript is not second.evidence.transcript
            assert not {id(message) for message in first.messages} & {id(message) for message in second.messages}
            assert len(children) == 2 and children[0] is not children[1] and closed == children
            assert prechecks == children
            for child in children:
                reads = [(stage, value) for stage, owner, value in observed if owner is child]
                assert len(reads) == 1
                assert [stage for stage, _value in reads] == ["after"]
                assert all(value == materialization.inputs for _stage, value in reads)
                assert all(value.evidence is value.admitted_evidence.snapshot for _stage, value in reads)
                assert reads[0][1].admitted_evidence is not materialization.inputs.admitted_evidence
            assert observed[0][2].admitted_evidence is not observed[1][2].admitted_evidence
            assert len(transferred) == 2 and all(value is materialization.inputs.admitted_evidence for value in transferred)
            assert parent.active and parent.cached_entry_count > 0 and current_retention_stage_memo() is parent
        assert (parent.active, parent.cached_entry_count, current_retention_stage_memo()) == (False, 0, None)
        assert stored_rows(store[0]) == before and len(primary_sdk) == 1
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_v4_current_evidence_parent_never_clears_changed_selected_chunk(operation_store, tmp_path, monkeypatch) -> None:
    from elspeth.web.composer import continuation_message_families as families

    async with _early_case(operation_store, tmp_path, monkeypatch) as (store, phases, running, selected, primary_sdk):
        runtime, clock = phases.runtime, phases.action_clock
        assert runtime is not None and clock is not None
        materialization = runtime.materialization
        reference = selected.stack.frames[0].evidence
        actual_graph = _finish_copied_current_evidence
        actual_read = CurrentContinuationMessageProducers._require_current
        actual_family_read = families._read_current_sources
        graph_calls, reads = [], []
        read_returns, read_errors = [], []
        family_attempts, family_returns, family_errors = [], [], []
        children: list[RetentionStageMemo] = []
        graph_done = False

        def child_memo():
            child = current_retention_stage_memo()
            assert child is not None and child.active and child._text_parent is parent
            if not children or children[-1] is not child:
                children.append(child)
            return child

        def read(self):
            assert self is materialization
            child = child_memo()
            reads.append((child, graph_done))
            try:
                result = actual_read(self)
            except AuditIntegrityError as error:
                read_errors.append((child, error))
                raise
            read_returns.append((child, result))
            return result

        def read_family(*args, **values):
            child = child_memo()
            family_attempts.append((child, graph_done))
            try:
                result = actual_family_read(*args, **values)
            except AuditIntegrityError as error:
                family_errors.append((child, error))
                raise
            family_returns.append((child, result))
            return result

        def change_after_graph(*args, **values):
            nonlocal graph_done
            result = actual_graph(*args, **values)
            graph_calls.append(result)
            changed = b"{}"
            with store[0].begin() as conn:
                mutation = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(running.claim.session_id),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                    )
                    .values(payload=changed, sha256=sha256(changed).hexdigest(), byte_length=len(changed))
                )
                assert mutation.rowcount == 1
            graph_done = True
            return result

        monkeypatch.setattr(bridge, "_finish_copied_current_evidence", change_after_graph)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", read)
        monkeypatch.setattr(families, "_read_current_sources", read_family)
        with v4_preparation_memo_scope(clock) as parent:
            with pytest.raises(AuditIntegrityError, match="cold evidence storage differs from its selected bytes") as first_refusal:
                await restore_current_turn_evidence(
                    service=phases.service,
                    authority=phases.authority,
                    selected=selected,
                    inventory=materialization.inventory,
                    materialization=materialization,
                    required_work=phases.work,
                )
            assert reads == [(children[0], True)] and len(graph_calls) == 1
            assert not read_returns and read_errors == [(children[0], first_refusal.value)]
            assert family_attempts == [(children[0], False)]
            assert len(family_returns) == 1 and family_returns[0][0] is children[0] and not family_errors
            assert not children[0].active and children[0].cached_entry_count == 0 and parent.active
            after_mutation = stored_rows(store[0])
            with pytest.raises(AuditIntegrityError, match="cold evidence storage differs from its selected bytes") as retry_refusal:
                await restore_current_turn_evidence(
                    service=phases.service,
                    authority=phases.authority,
                    selected=selected,
                    inventory=materialization.inventory,
                    materialization=materialization,
                    required_work=phases.work,
                )
            assert len(children) == 2 and children[1] is not children[0]
            assert family_attempts == [(children[0], False), (children[1], True)]
            assert len(family_returns) == 1 and family_errors == [(children[1], retry_refusal.value)]
            assert reads == [(children[0], True)] and not read_returns
            assert read_errors == [(children[0], first_refusal.value)] and len(graph_calls) == 1
            assert not children[1].active and children[1].cached_entry_count == 0
            assert stored_rows(store[0]) == after_mutation
        assert (parent.active, parent.cached_entry_count, current_retention_stage_memo()) == (False, 0, None)
        assert len(primary_sdk) == 1
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_v4_current_evidence_final_read_survives_parent_close_without_late_publication(
    operation_store, tmp_path, monkeypatch
) -> None:
    async with _early_case(operation_store, tmp_path, monkeypatch) as (store, phases, _running, selected, primary_sdk):
        runtime, clock = phases.runtime, phases.action_clock
        assert runtime is not None and clock is not None
        materialization = runtime.materialization
        actual_graph = _finish_copied_current_evidence
        actual_read = CurrentContinuationMessageProducers._require_current
        actual_env = phases.authority.read_continuation_env_selection
        entered, release = Event(), Event()
        children, admissions, transferred = [], [], []
        prechecks = []
        graph_done = False

        def read(self):
            assert self is materialization
            child = current_retention_stage_memo()
            assert child is not None and child.active
            result = actual_read(self)
            admissions.append((graph_done, result.admitted_evidence, parent.active))
            return result

        def read_env(*args, **values):
            assert not graph_done and parent.active
            value = actual_env(*args, **values)
            assert value == materialization.inventory.selection
            prechecks.append(value)
            return value

        def held_graph(admitted, **values):
            nonlocal graph_done
            assert len(prechecks) == 1 and not admissions
            assert admitted is materialization.inputs.admitted_evidence
            result = actual_graph(admitted, **values)
            child = current_retention_stage_memo()
            assert child is not None and child.active and child._text_parent is parent
            children.append(child)
            transferred.append(result)
            graph_done = True
            entered.set()
            assert release.wait(10)
            return result

        monkeypatch.setattr(bridge, "_finish_copied_current_evidence", held_graph)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", read)
        monkeypatch.setattr(phases.authority, "read_continuation_env_selection", read_env)
        before = stored_rows(store[0])
        with v4_preparation_memo_scope(clock) as parent:
            task = asyncio.create_task(
                restore_current_turn_evidence(
                    service=phases.service,
                    authority=phases.authority,
                    selected=selected,
                    inventory=materialization.inventory,
                    materialization=materialization,
                    required_work=phases.work,
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 60)
                assert parent.active and parent.cached_entry_count > 0 and children[0].active
                parent.close()
                assert (parent.active, parent.cached_entry_count, children[0].active) == (False, 0, True)
            finally:
                release.set()
                result = await task
            assert result is transferred[0]
            assert len(admissions) == 1
            assert admissions[-1][0] is True and admissions[-1][2] is False
            assert admissions[-1][1] is not materialization.inputs.admitted_evidence
            assert not children[0].active and children[0].cached_entry_count == 0
            assert (parent.active, parent.cached_entry_count) == (False, 0)
        assert current_retention_stage_memo() is None and stored_rows(store[0]) == before and len(primary_sdk) == 1
        phases.work.coordinator.assert_completed()
