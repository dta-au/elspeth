"""Actual P4, classifier SDK custody and immutable family-0 source controls."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.required_work import required_failure_leaves
from elspeth.web.sessions import continuation_classifier_primary as classifier
from elspeth.web.sessions import continuation_iteration_return as stages
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.composer_operations import COMPOSER_CANCEL_REQUESTED, ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_primary import allocate_primary_effect_on_connection, prepare_primary_provider_on_connection
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import classifier_15s_probe_settings, continuation_test_settings
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization
from tests.unit.web.sessions import test_continuation_initialization as bootstrap_fixture
from tests.unit.web.sessions import test_continuation_live_phases as live_fixture
from tests.unit.web.sessions.test_continuation_iteration_stages import _applied_case
from tests.unit.web.sessions.test_continuation_iteration_stages import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


@asynccontextmanager
async def _classifier_15s_case(store, tmp_path, monkeypatch):
    """Admit an authentic V2 classifier with one explicit test-only PRIMARY bound."""
    settings = classifier_15s_probe_settings()
    policy = continuation_policy_from_settings(settings)
    assert policy.actions.primary.preparation == 15.0
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    original_input = live_fixture.initialized_primary_input

    async def with_primary_probe(current_store, current_tmp_path, current_monkeypatch, *, settings_overrides=None):
        overrides = {**(settings_overrides or {}), "composer_continuation_action_bounds": settings.composer_continuation_action_bounds}
        return await original_input(current_store, current_tmp_path, current_monkeypatch, settings_overrides=overrides)

    monkeypatch.setattr(bootstrap_fixture, "minimal_continuation_policy", lambda: policy)
    monkeypatch.setattr(live_fixture, "initialized_primary_input", with_primary_probe)
    probe_store = (store[0], store[1], authority, store[3], store[4])
    async with _classifier_case(probe_store, tmp_path, monkeypatch) as case:
        yield case


@asynccontextmanager
async def _classifier_case(store, tmp_path, monkeypatch):
    actual_work = initialization.inventory_work

    def timed_work(running):
        selected = store[2].load_continuation_snapshot(running)
        root = decode_continuation_snapshot(selected.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled = time.monotonic()
        with store[0].connect() as conn:
            observed = database_now(conn)
            deadline = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).scalar_one()
        assert deadline is not None
        owner = ContinuationSQLDeadlineOwner(
            running,
            policy=policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=continuation_test_settings().composer_async_drain_seconds,
            hard_expires_at=sampled + (restore_utc(deadline) - observed).total_seconds(),
        )
        return actual_work(running, sql_deadline_owner=owner)

    monkeypatch.setattr(initialization, "inventory_work", timed_work)
    async with _applied_case(store, tmp_path, monkeypatch) as (phases, running, selected, inventory, captured, prior_sdk):
        ready = stages.retain_iteration_prepared(phases.authority, running, selected=selected, inventory=inventory, captured=captured)
        ready_selected = await load_selected_continuation(
            authority=phases.authority, running=running, selected=ready, required_work=phases.work
        )
        _runtime, ready_inventory = await phases.current(ready_selected)
        begun = stages.begin_iteration_paid_stage(
            phases.authority, running, selected=ready_selected, inventory=ready_inventory, stage="classify"
        )
        current = await load_selected_continuation(authority=phases.authority, running=running, selected=begun, required_work=phases.work)
        runtime, current_inventory = await phases.current(current)
        timing = phases.work.coordinator.sql_deadline_owner
        assert type(timing) is ContinuationSQLDeadlineOwner
        clock = ContinuationActionClock(timing.policy.actions.primary)
        timing.begin_action(timing.policy.actions.primary, started_at=clock.started_monotonic)
        phases.bind_action_clock(clock)
        issuer = classifier.ClassifierPrimaryIssuer(
            service=phases.service,
            authority=phases.authority,
            running=running,
            selected=current,
            inventory=current_inventory,
            runtime=runtime,
            required_work=phases.work,
            provider_owner=phases.provider_owner,
            clock=clock,
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            _messages, _tools, _delivery, prepared = phases.service._prepare_primary_delivery(
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                turn_evidence=runtime.live.turn_evidence,
                state=runtime.live.state,
                failed_turn=runtime.live.failed_turn,
            )
        yield phases, running, runtime, issuer, prepared, prior_sdk


def _reply():
    return ModelResponse(
        model="test/model",
        choices=[
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "The original classifier reply.", "tool_calls": None}}
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


async def _invoke(issuer, runtime, prepared, *, branch="composition_last_chance"):
    with prompt_guard_user_scope(runtime.bindings.message):
        return await issuer.invoke(
            branch=branch,
            prepared_input=prepared,
            recorder=runtime.live.recorder,
            credential_state=runtime.live.state if branch == "composition_last_chance" else None,
            deadline=runtime.bindings.deadline,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("staged_review", "composition_last_chance"))
async def test_classifier_retains_actual_sdk_source_cost_and_next_shared_primary_ordinal(operation_store, tmp_path, monkeypatch, branch):
    async with _classifier_15s_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, prepared, prior_sdk):
        assert len(prior_sdk) == 1
        physical = []
        with operation_store[0].connect() as conn:
            before_ordinal = conn.execute(select(ordinals.c.next_ordinal).where(ordinals.c.family == 0)).scalar_one()
        before_cost = phases.provider_owner.cost_budget.snapshot()

        async def completion(**kwargs):
            physical.append(kwargs)
            with operation_store[0].connect() as conn:
                row = conn.execute(select(effects).where(effects.c.effect_key == f"provider:0:{before_ordinal}")).one()
                assert row.disposition == "dispatched" and row.receipt_key is row.result_chunk_key is None
                assert conn.execute(select(ordinals.c.next_ordinal).where(ordinals.c.family == 0)).scalar_one() == before_ordinal + 1
            return _reply()

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared, branch=branch)
        assert issuer.result is result and issuer.sdk_entered and len(physical) == 1
        assert result.outcome.audit_call is result.buffer.llm_calls[-1]
        assert result.completion.message.content == "The original classifier reply."
        assert result.effect_key == f"provider:0:{before_ordinal}"
        assert result.source_checkpoint_id == issuer.selected.checkpoint_id
        assert result.source_cursor == issuer.selected.stack.cursor
        assert result.snapshot.cursor == result.source_cursor + 3
        root = decode_continuation_snapshot(result.snapshot.payload_json)
        assert len(root.frames) == 2 and root.frames[-1].frame_kind == "tool_batch"
        assert root.frames[0].pending_response == issuer.selected.stack.frames[0].pending_response
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            cold = classifier.read_original_classifier_primary_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
            )
            assert cold == result
            restored_cost = read_continuation_cost_snapshot_on_connection(conn, root)
            assert restored_cost == phases.provider_owner.cost_budget.snapshot()
            assert restored_cost.physical_calls == before_cost.physical_calls + 1
            assert result.outcome.audit_call.provider_cost is not None
            assert restored_cost.known_cost == before_cost.known_cost + Decimal(str(result.outcome.audit_call.provider_cost))
            unchanged = stored_rows(operation_store[0])
            with pytest.raises(AuditIntegrityError, match="sole compose"):
                prepare_primary_provider_on_connection(
                    conn,
                    running,
                    input_sha256=result.input_sha256,
                    snapshot_json=result.snapshot.payload_json,
                    expected_checkpoint_id=result.snapshot.checkpoint_id,
                    expected_cursor=result.snapshot.cursor,
                    input_json=read_chunk_text(conn, running, result.input_sha256),
                )
            # This is the actual allocator shared by ordinary PRIMARY. The
            # next allocation is rolled back; no invented ordinary phase or SDK.
            savepoint = conn.begin_nested()
            try:
                following = allocate_primary_effect_on_connection(conn, running, input_sha256=result.input_sha256, action_json=None)
                assert following.ordinal == before_ordinal + 1
                assert following.effect_key != result.effect_key
                assert following.effect_key not in {item.effect_id for item in root.effect_frontier}
            finally:
                savepoint.rollback()
            assert load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id) == result.snapshot
        assert stored_rows(operation_store[0]) == unchanged
        with pytest.raises(AuditIntegrityError, match="cannot issue or replay"):
            await _invoke(issuer, runtime, prepared, branch=branch)
        assert len(physical) == 1
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_another_classifier_issuer_cannot_replay_original_stage_after_actual_response(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, prepared, _prior_sdk):
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply()

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        selected = await load_selected_continuation(
            authority=phases.authority, running=running, selected=result.snapshot, required_work=phases.work
        )
        following, inventory = await phases.current(selected)
        repeated = classifier.ClassifierPrimaryIssuer(
            service=phases.service,
            authority=phases.authority,
            running=running,
            selected=selected,
            inventory=inventory,
            runtime=following,
            required_work=phases.work,
            provider_owner=phases.provider_owner,
            clock=ContinuationActionClock(issuer.clock.policy),
        )
        with prompt_guard_user_scope(following.bindings.message):
            _messages, _tools, _delivery, again = phases.service._prepare_primary_delivery(
                messages=following.live.llm_messages,
                tools=following.live.tools,
                recorder=following.live.recorder,
                turn_evidence=following.live.turn_evidence,
                state=following.live.state,
                failed_turn=following.live.failed_turn,
            )
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="replay is forbidden"):
            await _invoke(repeated, following, again)
        assert stored_rows(operation_store[0]) == before and len(physical) == 1
        assert repeated.result is None and not repeated.sdk_entered
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_classifier_fresh_clock_cannot_renew_expired_original_action(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, _running, runtime, issuer, prepared, _prior_sdk):

        async def forbidden(**kwargs):
            raise AssertionError("an expired original classifier action cannot enter SDK")

        monkeypatch.setattr(litellm, "acompletion", forbidden)
        timing = phases.work.coordinator.sql_deadline_owner
        assert type(timing) is ContinuationSQLDeadlineOwner
        assert timing.policy.actions.primary.preparation == 15.0
        timing.begin_action(timing.policy.actions.primary, started_at=time.monotonic() - timing.policy.actions.primary.total - 1)
        before = stored_rows(operation_store[0])
        with pytest.raises(TimeoutError, match="expired before preparation"):
            await _invoke(issuer, runtime, prepared)
        assert stored_rows(operation_store[0]) == before
        assert issuer.result is None and not issuer.sdk_entered
        phases.work.coordinator.assert_completed()


def read_chunk_text(conn, running, digest):
    return (
        conn.execute(
            select(chunks.c.payload).where(
                chunks.c.session_id == str(running.claim.session_id),
                chunks.c.operation_id == running.claim.operation_id,
                chunks.c.chunk_key == f"provider-input:{digest}",
            )
        )
        .scalar_one()
        .decode("utf-8")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("outcome", "recorder"))
async def test_classifier_publication_refuses_equal_copy_before_original_positive(operation_store, tmp_path, monkeypatch, changed):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, _running, runtime, issuer, prepared, _prior_sdk):
        physical, refused = [], []
        actual_publish = classifier.publish_classifier_primary_on_connection

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply()

        def observe(conn, original, **kwargs):
            substituted = {**kwargs, changed: replace(kwargs[changed])}
            before = tuple(conn.execute(select(effects).order_by(effects.c.effect_key)).all())
            with pytest.raises(AuditIntegrityError, match="original live response/recorder") as failure:
                actual_publish(conn, original, **substituted)
            refused.append(failure.value)
            assert tuple(conn.execute(select(effects).order_by(effects.c.effect_key)).all()) == before
            return actual_publish(conn, original, **kwargs)

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(classifier, "publish_classifier_primary_on_connection", observe)
        result = await _invoke(issuer, runtime, prepared)
        assert issuer.result is result and len(physical) == len(refused) == 1
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("ledger", "receipt", "input", "missing_evidence", "corrupt_evidence"))
async def test_classifier_cold_source_rejects_substitution_after_actual_positive(operation_store, tmp_path, monkeypatch, changed):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, prepared, _prior_sdk):
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply()

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        assert len(physical) == 1
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:

            def cold():
                return classifier.read_original_classifier_primary_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
                )

            assert cold() == result
            savepoint = conn.begin_nested()
            try:
                if changed == "ledger":
                    entry_id = _usage_entry_id(
                        session_id=str(running.claim.session_id),
                        source="composer",
                        run_id=None,
                        call_id=result.outcome.audit_call.call_id,
                    )
                    altered = conn.execute(update(usage).where(usage.c.entry_id == entry_id).values(prompt_tokens=999))
                elif changed == "receipt":
                    altered = conn.execute(
                        update(effects).where(effects.c.effect_key == result.effect_key).values(receipt_key="foreign-call")
                    )
                elif changed == "input":
                    descriptor = json.loads(read_chunk_text(conn, running, result.input_sha256))
                    descriptor["messages"].append({"role": "user", "content": "Substituted original request."})
                    payload = canonical_json(descriptor).encode("utf-8")
                    altered = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(running.claim.session_id),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == f"provider-input:{result.input_sha256}",
                        )
                        .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                    )
                else:
                    committed_root = decode_continuation_snapshot(result.snapshot.payload_json)
                    reference = committed_root.frames[0].evidence
                    assert reference != issuer.selected.stack.frames[0].evidence
                    target = (
                        chunks.c.session_id == str(running.claim.session_id),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                    )
                    if changed == "missing_evidence":
                        altered = conn.execute(delete(chunks).where(*target))
                    else:
                        payload = b"{}"
                        altered = conn.execute(
                            update(chunks)
                            .where(*target)
                            .values(
                                payload=payload,
                                sha256=sha256(payload).hexdigest(),
                                byte_length=len(payload),
                            )
                        )
                assert altered.rowcount == 1
                with pytest.raises(AuditIntegrityError):
                    cold()
            finally:
                savepoint.rollback()
            assert cold() == result
        assert len(physical) == 1
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_classifier_current_stop_refuses_before_allocation_or_sdk(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, prepared, _prior_sdk):

        async def forbidden(**kwargs):
            raise AssertionError("Stop before classifier preparation cannot enter SDK")

        monkeypatch.setattr(litellm, "acompletion", forbidden)
        phases.authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        before = stored_rows(operation_store[0])
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await _invoke(issuer, runtime, prepared)
        assert issuer.result is None and not issuer.sdk_entered
        assert stored_rows(operation_store[0]) == before
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_classifier_equal_selected_copy_cannot_replace_actual_runtime_owner(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, _prepared, _prior_sdk):
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="actual live owners"):
            classifier.ClassifierPrimaryIssuer(
                service=phases.service,
                authority=phases.authority,
                running=running,
                selected=replace(issuer.selected),
                inventory=issuer.inventory,
                runtime=runtime,
                required_work=phases.work,
                provider_owner=phases.provider_owner,
                clock=issuer.clock,
            )
        assert stored_rows(operation_store[0]) == before
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_classifier_publication_rollback_preserves_original_failure_and_paid_no_replay(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, prepared, _prior_sdk):
        physical = []
        original = RuntimeError("original classifier publication transaction rollback")
        actual_publish = classifier.publish_classifier_primary_on_connection

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply()

        def rollback(*args, **kwargs):
            actual_publish(*args, **kwargs)
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(classifier, "publish_classifier_primary_on_connection", rollback)
        with pytest.raises(RuntimeError) as failed:
            await _invoke(issuer, runtime, prepared)
        assert failed.value is original and issuer.result is None
        assert issuer.sdk_entered and len(physical) == 1
        with operation_store[0].connect() as conn:
            actual = conn.execute(select(effects).where(effects.c.owner == "provider", effects.c.disposition == "dispatched")).one()
            assert actual.receipt_key is actual.result_chunk_key is actual.provider_attempt_id is None
            current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            assert current.cursor == issuer.selected.stack.cursor + 2
            with pytest.raises(AuditIntegrityError):
                classifier.read_original_classifier_primary_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=actual.effect_key
                )
        with pytest.raises(RuntimeError) as repeated:
            await _invoke(issuer, runtime, prepared)
        assert repeated.value is original and len(physical) == 1
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_classifier_cancellation_joins_committed_actual_response_without_replay(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, issuer, prepared, _prior_sdk):
        physical = []
        entered, release = threading.Event(), threading.Event()
        actual_publish = classifier.publish_classifier_primary_on_connection

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply()

        def held_publish(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5), "test must release actual classifier publication"
            return actual_publish(*args, **kwargs)

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(classifier, "publish_classifier_primary_on_connection", held_publish)
        task = asyncio.create_task(_invoke(issuer, runtime, prepared))
        try:
            reached = await asyncio.to_thread(entered.wait, 15)
            if not reached and task.done():
                await task
            assert reached, "actual paid classifier did not reach publication"
            task.cancel(COMPOSER_CANCEL_REQUESTED)
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(BaseException) as original:
                await task
            assert any(
                isinstance(leaf, asyncio.CancelledError) and leaf.args == (COMPOSER_CANCEL_REQUESTED,)
                for leaf in required_failure_leaves(original.value)
            )
            result = issuer.result
            assert result is not None and issuer.sdk_entered and len(physical) == 1
            with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
                cold = classifier.read_original_classifier_primary_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
                )
                assert cold == result
            with pytest.raises(BaseException) as repeated:
                await _invoke(issuer, runtime, prepared)
            assert repeated.value is original.value and len(physical) == 1
        finally:
            release.set()
            if not task.done():
                task.cancel(COMPOSER_CANCEL_REQUESTED)
                await asyncio.gather(task, return_exceptions=True)
        phases.work.coordinator.assert_completed()
