"""Actual V4 preview phase, original producer and atomic P4 cold replay."""

import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from elspeth.web.composer import continuation_preview_dispatch as preview
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_validation_terminal_holds_table as holds
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case

operation_store = phases.operation_store


@asynccontextmanager
async def actual_preview_case(store, tmp_path, monkeypatch):
    actual_work = initialization_fixture.inventory_work

    def timed_work(running):
        stored = store[2].load_continuation_snapshot(running)
        root = decode_continuation_snapshot(stored.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled_at = time.monotonic()
        with store[0].connect() as conn:
            observed_at = database_now(conn)
            deadline = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).scalar_one()
        assert deadline is not None
        remaining = (restore_utc(deadline) - observed_at).total_seconds()
        assert remaining > 0
        clock_owner = ContinuationSQLDeadlineOwner(
            running,
            policy=policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=continuation_test_settings().composer_async_drain_seconds,
            hard_expires_at=sampled_at + remaining,
        )
        return actual_work(running, sql_deadline_owner=clock_owner)

    monkeypatch.setattr(initialization_fixture, "inventory_work", timed_work)
    async with _v4_tool_case(store, tmp_path, monkeypatch, tool="preview_pipeline", arguments={}) as case:
        current_store, owner, running, sdk = case
        clock_owner = owner.work.coordinator.sql_deadline_owner
        assert clock_owner is not None
        action = max(
            (
                clock_owner.policy.actions.read,
                clock_owner.policy.actions.session_sql,
                clock_owner.policy.actions.blob,
                clock_owner.policy.actions.external,
            ),
            key=lambda value: value.total,
        )
        clock = ContinuationActionClock(action)
        clock_owner.begin_action(action, started_at=clock.started_monotonic)
        owner.bind_action_clock(clock)
        yield current_store, owner, running, sdk


@pytest.mark.asyncio
async def test_actual_preview_phase_issues_once_retains_original_result_and_cold_never_reexecutes(operation_store, tmp_path, monkeypatch):
    async with actual_preview_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        actual = preview.execute_tool
        originals = []

        def observed(*args, **kwargs):
            with store[0].connect() as conn:
                row = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
                effect = conn.execute(select(effects).where(effects.c.effect_key == row.effect_key)).one()
            assert row.state == "active" and effect.disposition == "dispatched"
            assert effect.response_started_at is not None and effect.result_chunk_key is None
            result = actual(*args, **kwargs)
            originals.append(result)
            return result

        monkeypatch.setattr(preview, "execute_tool", observed)
        await phases._advance(owner)
        assert len(originals) == 1 and originals[0].success
        assert originals[0].runtime_preflight is not None
        assert originals[0].runtime_preflight.errors[0].error_code == "empty_pipeline"
        stored = owner.latest_snapshot
        assert stored is not None and owner.preview_terminal is None
        assert owner.runtime is owner.primary is owner.p4 is owner.finished_batch is None
        with store[0].connect() as conn:
            hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
            effect = conn.execute(select(effects).where(effects.c.effect_key == hold.effect_key)).one()
        assert hold.state == "consumed" and effect.disposition == "committed"
        assert effect.receipt_key is not None and effect.result_chunk_key is not None and effect.provider_attempt_id is None

        def forbidden(*args, **kwargs):
            raise AssertionError("cold preview reentered its producer")

        monkeypatch.setattr(preview, "execute_tool", forbidden)
        cold = ContinuationLivePhases(
            owner.service,
            owner.authority,
            owner.work,
            ProviderInvocationOwner(service=store[3], required_work=owner.work),
            owner.lease,
        )
        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=stored, required_work=owner.work)
        assert is_applied_batch_cut(selected)
        before = stored_rows(store[0])
        restored, _inventory = await cold.current(selected)
        assert restored.graph.batch is not None
        assert restored.graph.batch.batch.tool_outcomes[0].response is not None
        assert stored_rows(store[0]) == before and len(sdk) == 1 and len(originals) == 1
