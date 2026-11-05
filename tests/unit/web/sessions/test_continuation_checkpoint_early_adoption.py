"""Committed EARLY graph adoption preserves physical owners and action bounds."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_checkpoint_early_adoption as adoption
from elspeth.web.composer.continuation_checkpoint_early_live import EarlyCheckpointLiveAdvanced
from elspeth.web.sessions import continuation_checkpoint_early_owed_overflow as completion
from tests.unit.web.sessions.test_continuation_checkpoint_early_owed_overflow import _original_h
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize(("malformed", "stopped"), [(False, False), (True, False), (False, True)])
async def test_committed_early_cost_and_graph_keep_original_owners(operation_store, tmp_path, monkeypatch, malformed, stopped):
    async with _original_h(operation_store, tmp_path, monkeypatch, malformed=malformed) as case:
        service, source, prepared, owed, returned, projected, sdk = case
        phases = source.phase_owner.phases
        old = phases.runtime
        assert old is not None
        primary, provider, work, lease = phases.primary, phases.provider_owner, phases.work, phases.lease
        progress, p4, clock = phases.progress, phases.p4, phases.action_clock
        budget = provider.cost_budget
        assert budget is not None and clock is not None
        before = budget.snapshot()
        assert before.physical_calls == len(before.charges) + 1
        original_projection = adoption.project_committed_early_cost
        projected_calls = []

        def project_with_opponents(actual_source, **kwargs):
            assert actual_source is source
            for changed in (
                {**kwargs, "proof": replace(kwargs["proof"])},
                {**kwargs, "readback": replace(kwargs["readback"])},
                {**kwargs, "advanced": replace(kwargs["advanced"])},
            ):
                with pytest.raises(AuditIntegrityError):
                    original_projection(actual_source, **changed)
                assert budget.snapshot() == before
            original_projection(actual_source, **kwargs)
            projected_calls.append(kwargs["advanced"])
            charged_once = budget.snapshot()
            with pytest.raises(AuditIntegrityError):
                original_projection(actual_source, **kwargs)
            assert budget.snapshot() == charged_once

        monkeypatch.setattr(adoption, "project_committed_early_cost", project_with_opponents)
        result = await completion.complete_early_checkpoint_or_overflow(
            service=service, prepared=prepared, owed=owed, returned=returned, projected=projected
        )
        assert type(result) is EarlyCheckpointLiveAdvanced
        assert projected_calls == [result.advanced]
        assert result.source is source and result.returned is returned
        assert (result.advanced.retry is not None) is malformed
        assert phases.latest_snapshot is result.advanced.completed
        assert phases.runtime is old
        charged = budget.snapshot()
        assert charged.physical_calls == before.physical_calls
        assert len(charged.charges) == len(before.charges) + 1
        assert sum(charge.call_id == returned.audit_call.call_id for charge in charged.charges) == 1
        with pytest.raises(AuditIntegrityError, match="original settled runtime owners"):
            await adoption.adopt_committed_early(source, advanced=replace(result.advanced), returned=returned)
        assert phases.runtime is old and budget.snapshot() == charged

        restores = []
        original_restore = adoption.restore_continuation_runtime

        async def observe_restore(**kwargs):
            restores.append(kwargs)
            return await original_restore(**kwargs)

        monkeypatch.setattr(adoption, "restore_continuation_runtime", observe_restore)
        if stopped:
            phases.observe_stop()
            with pytest.raises(asyncio.CancelledError, match="adoption stopped"):
                await adoption.adopt_committed_early(source, advanced=result.advanced, returned=returned)
            assert not restores and phases.runtime is old
        else:
            restored = await adoption.adopt_committed_early(source, advanced=result.advanced, returned=returned)
            assert len(restores) == 1 and phases.runtime is restored and restored is not old
            assert restored.live.llm_messages is restored.graph.messages
            assert restored.live.llm_messages is not old.live.llm_messages
            assert restored.live.recorder is restored.graph.recorder
            assert restored.live.recorder is not old.live.recorder
            assert restored.live.turn_evidence is restored.graph.evidence
            assert restored.bindings.deadline <= old.bindings.deadline
            assert restored.bindings.provider_owner is provider and restored.bindings.required_work is work
            assert restored.bindings.progress is progress
            assert restored.source.selected.checkpoint_id == result.advanced.completed.checkpoint_id
            data = restored.graph.batch
            finished = phases.finished_batch
            assert data is not None and finished is not None
            assert finished[0].state is data.batch.state is restored.live.state
            assert finished[0].assistant_tool_calls is data.calls
            assert finished[2] is data.batch.entry_state
        assert phases.primary is primary and phases.p4 is p4
        assert phases.provider_owner is provider and provider.cost_budget is budget
        assert phases.work is work and phases.lease is lease and phases.progress is progress
        assert phases.action_clock is clock and budget.snapshot() == charged and sdk == [1]
