"""Actual END writer DATA advances warm owners without carrying authority."""

from dataclasses import replace
from datetime import timedelta

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_end_dispatch as dispatch
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import _read_current_env_names
from elspeth.web.sessions import continuation_checkpoint_paid as checkpoint_sql
from elspeth.web.sessions import continuation_end as end_sql
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot
from elspeth.web.sessions.continuation_end import _primary
from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.sessions.test_continuation_end_shared_execution import actual_shared_end_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_semantic_v4_live import _phases

operation_store = _operation_store


@pytest.mark.asyncio
async def test_actual_end_dispatch_data_matches_cold_source_and_keeps_live_owners_and_clock(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=True) as case:
        phases = _phases(case)
        selected = case["runtime"].source.selected
        runtime, primary = await _primary(phases, selected)
        inputs = runtime.materialization.inputs
        clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
        phases.bind_action_clock(clock)
        bounds = clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline
        read = dispatch._read_dispatched_end_source
        observations = []

        def compare(conn, running, **kwargs):
            fresh = read(conn, running, **kwargs)
            independent = read_runtime_source_on_connection(conn, running, selected=kwargs["selected"], inventory=kwargs["inventory"])
            # Only the DB observation time differs; original deadlines, policy,
            # costs, attachment sources and all immutable DATA remain compared.
            assert replace(independent, timing=fresh.timing) == fresh
            assert independent.timing.observed_at + timedelta(seconds=independent.timing.hard_remaining_seconds) == (
                fresh.timing.observed_at + timedelta(seconds=fresh.timing.hard_remaining_seconds)
            )
            assert independent.timing.observed_at + timedelta(seconds=independent.timing.cooperative_remaining_seconds) == (
                fresh.timing.observed_at + timedelta(seconds=fresh.timing.cooperative_remaining_seconds)
            )
            assert fresh.admitted_evidence is inputs.admitted_evidence
            assert fresh.admitted_evidence is not independent.admitted_evidence
            observations.append(fresh)
            return fresh

        async def forbidden_sdk(**kwargs):
            pytest.fail("END dispatch DATA handoff entered a provider")

        monkeypatch.setattr(dispatch, "_read_dispatched_end_source", compare)
        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        phase, source = await dispatch.dispatch_and_adopt_end_review(phases, selected, runtime)
        with locked_session_transaction(operation_store[0], str(case["running"].claim.session_id)) as conn:
            cold_phase = checkpoint_sql.checkpoint_phase_source_on_connection(
                conn,
                session_id=case["running"].claim.session_id,
                operation_id=case["running"].claim.operation_id,
                selected=source.current,
            )
        assert source == cold_phase and not source.completed_children and not source.terminal
        assert len(observations) == 1 and observations[0] is phase.runtime.source
        assert phase.selected.stack.frames == selected.stack.frames
        assert phase.runtime.live is runtime.live and phase.runtime.graph is runtime.graph
        assert phase.runtime.bindings is runtime.bindings and phase.runtime.materialization is runtime.materialization
        assert phases.primary is primary and phases.runtime is phase.runtime
        assert phases._require_phase_read(phase.selected, phase) is phase
        assert phases.action_clock is clock
        assert bounds == (clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("stop", "head", "environment", "closure", "ledger", "effect", "writer_key"))
async def test_actual_end_dispatch_handoff_rechecks_changed_current_facts(operation_store, tmp_path, monkeypatch, fault):
    async with _semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=True) as case:
        phases = _phases(case)
        selected = case["runtime"].source.selected
        runtime, _primary_result = await _primary(phases, selected)
        original_dispatch = dispatch.retain_end_review_dispatch
        actual_names = AdvisorCheckpointOwner._current_env_ref_names
        mutations, physical_before = [], []
        environment_reads = []
        sid, operation = str(case["running"].claim.session_id), case["running"].claim.operation_id

        async def changed_names(advisor, session_id):
            assert advisor is phases.service._advisor_checkpoint and session_id == sid
            names = await actual_names(advisor, session_id)
            assert names == runtime.env_ref_names and "changed_end_environment" not in names
            environment_reads.append(bool(mutations))
            return names | frozenset({"changed_end_environment"}) if mutations else names

        def change_after_write(*args, **kwargs):
            result = original_dispatch(*args, **kwargs)
            with locked_session_transaction(operation_store[0], sid) as conn:
                physical_before.append(tuple(conn.execute(select(attempts.c.attempt_id).where(attempts.c.session_id == sid)).scalars()))
                if fault == "head":
                    append_complete_snapshot(
                        conn,
                        session_id=sid,
                        operation_id=operation,
                        expected_checkpoint_id=result.selected.checkpoint_id,
                        expected_cursor=result.selected.cursor,
                        snapshot_json=result.selected.payload_json,
                        event_batch=(ContinuationEvent("checkpoint", '{"fixture":"actual later head"}'),),
                        handoff=False,
                    )
                elif fault == "closure":
                    reference = selected.stack.frames[0].messages
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == sid,
                            chunks.c.operation_id == operation,
                            chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                        )
                        .values(sha256="0" * 64)
                    )
                    assert changed.rowcount == 1
                elif fault == "ledger":
                    entry = conn.execute(select(usage).where(usage.c.session_id == sid)).one()
                    changed = conn.execute(
                        update(usage).where(usage.c.entry_id == entry.entry_id).values(prompt_tokens=entry.prompt_tokens + 1)
                    )
                    assert changed.rowcount == 1
                elif fault == "effect":
                    changed = conn.execute(
                        update(effects)
                        .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == result.effect_key)
                        .values(input_sha256="0" * 64)
                    )
                    assert changed.rowcount == 1
            mutations.append(result)
            if fault == "stop":
                stopped = phases.authority.request_cancel(session_id=case["running"].claim.session_id, operation_id=operation)
                assert stopped is not None and stopped.cancel_requested_at is not None
            return replace(result, effect_key="end:wrong-writer-key") if fault == "writer_key" else result

        async def forbidden_sdk(**kwargs):
            pytest.fail("changed END handoff entered a provider")

        monkeypatch.setattr(dispatch, "retain_end_review_dispatch", change_after_write)
        if fault == "environment":
            monkeypatch.setattr(AdvisorCheckpointOwner, "_current_env_ref_names", changed_names)
            assert await _read_current_env_names(phases.service._advisor_checkpoint, sid) == runtime.env_ref_names
            assert environment_reads == [False]
        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        expected = ComposerOperationCancelledDuringTurn if fault == "stop" else AuditIntegrityError
        with pytest.raises(expected):
            await dispatch.dispatch_and_adopt_end_review(phases, selected, runtime)
        assert len(mutations) == 1
        if fault == "environment":
            assert environment_reads == [False, True]
        assert phases.runtime is runtime and runtime.materialization.selected is selected
        with operation_store[0].connect() as conn:
            after = tuple(conn.execute(select(attempts.c.attempt_id).where(attempts.c.session_id == sid)).scalars())
        assert physical_before == [after]


@pytest.mark.asyncio
async def test_prescribed_end_keeps_full_history_proof_after_warm_handoff(operation_store, tmp_path, monkeypatch):
    original_adopt = dispatch.dispatch_and_adopt_end_review
    original_prepare = checkpoint_sql.prepare_v4_checkpoint_source_on_connection
    adopted, boundaries, attempts_before = [], [], []

    async def adopt(phases, selected, runtime):
        result = await original_adopt(phases, selected, runtime)
        assert selected.stack.active_action_source is not None
        adopted.append(result)
        return result

    def corrupt_before_source(conn, service, prepared):
        assert len(adopted) == 1
        phase, source = adopted[0]
        assert prepared.selected is phase.selected and prepared.selected.checkpoint_id == source.current.checkpoint_id
        sid, operation = str(prepared.running.claim.session_id), prepared.running.claim.operation_id
        root = decode_continuation_snapshot(source.current.payload_json)
        assert root.active_action_source is not None
        semantic_key = root.active_action_source.effect_key
        effect = conn.execute(
            select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == semantic_key)
        ).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
        entry = conn.execute(select(usage).where(usage.c.entry_id == physical.ledger_entry_id)).one()
        attempts_before.append(tuple(conn.execute(select(attempts.c.attempt_id).where(attempts.c.session_id == sid)).scalars()))
        changed = conn.execute(update(usage).where(usage.c.entry_id == entry.entry_id).values(prompt_tokens=entry.prompt_tokens + 1))
        assert changed.rowcount == 1
        boundaries.append(prepared)
        return original_prepare(conn, service, prepared)

    monkeypatch.setattr(end_sql, "dispatch_and_adopt_end_review", adopt)
    monkeypatch.setattr(checkpoint_sql, "prepare_v4_checkpoint_source_on_connection", corrupt_before_source)
    with pytest.raises(AuditIntegrityError, match="ledger"):
        async with actual_shared_end_case(operation_store, tmp_path, monkeypatch):
            pytest.fail("post-handoff corrupt historical ledger reached a paid END consequence")
    assert len(adopted) == len(boundaries) == len(attempts_before) == 1
    with operation_store[0].connect() as conn:
        after = tuple(
            conn.execute(
                select(attempts.c.attempt_id).where(attempts.c.session_id == str(boundaries[0].running.claim.session_id))
            ).scalars()
        )
    assert attempts_before == [after]
