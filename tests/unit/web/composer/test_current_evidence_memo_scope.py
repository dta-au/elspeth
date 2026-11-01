"""Current evidence keeps pure repeated scans local to one real SQL owner."""

from __future__ import annotations

from hashlib import sha256

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_current_evidence as bridge
from elspeth.web.composer.continuation_current_evidence import restore_current_turn_evidence
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.continuation_retention_memo import current_retention_stage_memo
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_current_evidence import guarded_case

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_current_evidence_reuses_pure_checks_only_within_each_complete_read(operation_store, monkeypatch) -> None:
    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    original = CurrentContinuationMessageProducers._require_current
    original_graph = bridge._restore_current_evidence
    observations = []
    phase = "before"

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
        return result

    monkeypatch.setattr(bridge, "_restore_current_evidence", observed_graph)
    monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", observed)
    try:
        assert current_retention_stage_memo() is None
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert len(observations) >= 3
        assert [entry[0] for entry in observations] == ["before", *(["graph"] * (len(observations) - 2)), "after"]
        first_memo = observations[0][1]
        assert all(entry[1] is first_memo for entry in observations)
        assert observations[0][3] > observations[0][2]
        assert observations[-1][3] == observations[-1][2]
        assert not first_memo.active and first_memo.cached_entry_count == 0
        assert current_retention_stage_memo() is None

        observations.clear()
        phase = "before"
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert len(observations) >= 3
        assert [entry[0] for entry in observations] == ["before", *(["graph"] * (len(observations) - 2)), "after"]
        assert all(entry[1] is observations[0][1] for entry in observations)
        assert observations[0][1] is not first_memo
        assert observations[-1][3] == observations[-1][2]
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
    original_graph = bridge._restore_current_evidence
    calls = []
    graph_done = False
    changed = b"{}"

    def observed(self):
        assert self is materialization and current_retention_stage_memo() is not None
        calls.append(graph_done)
        return original(self)

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

    monkeypatch.setattr(bridge, "_restore_current_evidence", change_after_graph)
    monkeypatch.setattr(CurrentContinuationMessageProducers, "_require_current", observed)
    try:
        with pytest.raises(AuditIntegrityError, match="cold evidence storage differs from its selected bytes"):
            await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert len(calls) >= 3 and calls[0] is False and calls[-1] is True
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
