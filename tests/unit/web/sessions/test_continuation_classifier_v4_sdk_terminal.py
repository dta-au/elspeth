"""Actual classifier SDK failures retain their original P4/request/H source."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from time import monotonic

import litellm
import pytest
from openai import OpenAIError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, read_chunk
from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import prepare_classifier_paid_incomplete_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import prepare_classifier_no_sdk_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import prepare_classifier_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_terminal_reservation import (
    classifier_sdk_terminal_reservation,
    classifier_terminal_reservation,
)
from elspeth.web.sessions.continuation_classifier_sdk_source import require_classifier_sdk_cold_source_on_connection
from elspeth.web.sessions.continuation_driver import ContinuationSegmentSettled
from elspeth.web.sessions.continuation_iteration_return import read_iteration_stage_on_connection
from elspeth.web.sessions.continuation_next_iteration import advance_continuation_iteration
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import read_v4_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalResult
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.helpers.continuation_app import observe_original_driver_failures
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_classifier_primary as classifier_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions.test_continuation_classifier_primary import _invoke
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import _v4_applied_case, _v4_classifier_case
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_v4_incomplete import _invoke as invoke_primary
from tests.unit.web.sessions.test_continuation_primary_v4_live import _v4_primary_input
from tests.unit.web.sessions.test_continuation_tool_failure import _actual_failure_driver

operation_store = _operation_store


def _admitted(conn, source) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == source.session_id,
            checkpoints.c.operation_id == source.operation_id,
            checkpoints.c.cursor == source.source_cursor + 1,
        )
    ).one()
    payload = read_chunk(conn, session_id=source.session_id, operation_id=source.operation_id, key=row.snapshot_chunk_key)
    return StoredContinuationSnapshot(payload.decode("utf-8"), row.cursor, row.checkpoint_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("staged_review", "composition_last_chance"))
@pytest.mark.parametrize("kind", ("provider_failure", "provider_timeout", "no_observed_result"))
async def test_actual_classifier_sdk_failure_closes_original_source_without_retry(operation_store, tmp_path, monkeypatch, branch, kind):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, prior_sdk = case
        sid = str(running.claim.session_id)
        with store[0].connect() as conn:
            prior_tools = tuple(
                conn.execute(
                    select(effects.c.effect_key, effects.c.disposition)
                    .where(effects.c.session_id == sid, effects.c.owner.in_(("tool_dispatch", "read")))
                    .order_by(effects.c.effect_key)
                )
            )
        original = {
            "provider_failure": OpenAIError("private SDK text must not be public"),
            "provider_timeout": TimeoutError("private SDK text must not be public"),
            "no_observed_result": asyncio.CancelledError("private SDK text must not be public"),
        }[kind]
        calls = []

        async def completion(**_arguments):
            calls.append(1)
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared, branch=branch)
        assert type(result) is SdkTerminalResult and result.family is SdkTerminalFamily.CLASSIFIER
        assert issuer.sdk_entered and issuer.result is None and issuer.terminal is result
        verified, receipt = result.verified, result.verified.receipt
        assert receipt.producer == "primary" and receipt.observation == kind
        assert receipt.sdk_entered and not receipt.sdk_result_observed and not receipt.retryable
        assert receipt.cost.status == "unknown" and receipt.cost.amount is None
        assert receipt.usage.status == "unavailable"
        assert result.deferred_cancellations == ((original,) if kind == "no_observed_result" else ())
        identity = receipt.identity
        with store[0].connect() as conn:
            cold = read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=identity.operation_id,
                effect_key=identity.effect_key,
                family=SdkTerminalFamily.CLASSIFIER,
                names=issuer.inventory.names,
            )
            assert cold == verified and cold is not None and cold.hold_state == "consumed"
            admitted = _admitted(conn, verified.source)
            terminal_root = decode_continuation_snapshot(verified.terminal.payload_json)
            applied_batch_cut(terminal_root.frames)
            assert terminal_root.frames == decode_continuation_snapshot(admitted.payload_json).frames
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == identity.effect_key)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == identity.attempt_id)).one()
            parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == identity.operation_id)).one()
            assert effect.disposition == "sdk_terminal" and physical.settled_at is not None and physical.ledger_entry_id is not None
            assert parent.status == "failed" and "private SDK text" not in parent.result_json
            assert (
                tuple(
                    conn.execute(
                        select(effects.c.effect_key, effects.c.disposition)
                        .where(effects.c.session_id == sid, effects.c.owner.in_(("tool_dispatch", "read")))
                        .order_by(effects.c.effect_key)
                    )
                )
                == prior_tools
            )
        with pytest.raises(AuditIntegrityError, match="cannot issue or replay"):
            await _invoke(issuer, runtime, prepared, branch=branch)
        assert calls == [1] and len(prior_sdk) == 1


@pytest.mark.asyncio
async def test_classifier_original_task_cancellation_retains_terminal_and_exact_deferred_cause(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case
        entered = asyncio.Event()
        calls = []
        original_cancellations = []

        async def completion(**_arguments):
            calls.append(1)
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as original:
                original_cancellations.append(original)
                raise

        monkeypatch.setattr(litellm, "acompletion", completion)
        task = asyncio.create_task(_invoke(issuer, runtime, prepared))
        observation = asyncio.create_task(entered.wait())
        try:
            done, _pending = await asyncio.wait((task, observation), timeout=issuer.clock.policy.total, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                pytest.fail("original classifier finished before its actual SDK entry")
            assert observation in done and entered.is_set()
            task.cancel("original classifier task cancellation")
            result = await task
        finally:
            for owned in (task, observation):
                if not owned.done():
                    owned.cancel()
            await asyncio.gather(task, observation, return_exceptions=True)
        assert type(result) is SdkTerminalResult and result.family is SdkTerminalFamily.CLASSIFIER
        assert len(original_cancellations) == 1
        assert any(actual is original_cancellations[0] for actual in result.deferred_cancellations)
        assert result.verified.receipt.observation == "no_observed_result" and calls == [1]
        with store[0].connect() as conn:
            assert (
                read_v4_sdk_terminal_verified_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=result.verified.receipt.identity.operation_id,
                    effect_key=result.verified.receipt.identity.effect_key,
                    family=SdkTerminalFamily.CLASSIFIER,
                    names=issuer.inventory.names,
                )
                == result.verified
            )


@pytest.mark.asyncio
async def test_classifier_owned_sdk_seam_failure_raises_original_without_terminal_or_retry(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case
        original = AuditIntegrityError("owned failure at actual SDK seam")
        calls = []

        async def completion(**_arguments):
            calls.append(1)
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        for _ in range(2):
            with pytest.raises(AuditIntegrityError) as caught:
                await _invoke(issuer, runtime, prepared)
            assert caught.value is original
        assert calls == [1] and issuer.sdk_entered and issuer.terminal is None
        with store[0].connect() as conn:
            hold = conn.execute(select(holds).where(holds.c.session_id == str(running.claim.session_id), holds.c.state == "active")).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == hold.provider_attempt_id)).one()
        assert physical.settled_at is None and physical.ledger_entry_id is None


@pytest.mark.asyncio
async def test_classifier_cold_sdk_source_rejects_legacy_recipe_and_foreign_model(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case

        async def completion(**_arguments):
            raise OpenAIError("original classifier provider failure")

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is SdkTerminalResult
        verified, identity = result.verified, result.verified.receipt.identity
        source = verified.source
        with store[0].begin() as conn:
            admitted = _admitted(conn, source)
            effect = conn.execute(
                select(effects).where(effects.c.session_id == source.session_id, effects.c.effect_key == source.effect_key)
            ).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == identity.attempt_id)).one()
            parent = conn.execute(
                select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)
            ).one()
            positive = require_classifier_sdk_cold_source_on_connection(
                conn,
                source=source,
                admitted=admitted,
                identity=identity,
                effect=effect,
                physical_row=physical,
                parent=parent,
                names=issuer.inventory.names,
            )
            assert positive.family is SdkTerminalFamily.CLASSIFIER and positive.requested_model == effect.requested_model
            paid = prepare_classifier_paid_overflow_tail_plan(
                identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
            )
            incomplete = prepare_classifier_paid_incomplete_tail_plan(
                identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
            )
            no_sdk = prepare_classifier_no_sdk_tail_plan(
                admitted=admitted,
                attempt_id=identity.attempt_id,
                requested_model=effect.requested_model,
                input_sha256=source.source_input_sha256,
                effect_key=source.effect_key,
                creator_sequence=source.creator_sequence,
                creator_generation=source.creator_generation,
                claim_attempt=source.claim_attempt,
                operation_epoch=source.session_operation_epoch,
            )
            legacy = classifier_terminal_reservation(
                paid_contract=paid.fallback_contract_sha256,
                paid_bytes=paid.reserved_bytes,
                incomplete_contract=incomplete.fallback_contract_sha256,
                incomplete_bytes=incomplete.reserved_bytes,
                no_sdk_contract=no_sdk.contract_sha256,
                no_sdk_bytes=no_sdk.reserved_bytes,
            )
            assert classifier_sdk_terminal_reservation(paid, incomplete, no_sdk) == (source.reserved_bytes, source.fallback_contract_sha256)
            assert classifier_sdk_terminal_reservation(paid, incomplete, no_sdk, historical_contract_sha256=legacy.contract_sha256) == (
                legacy.reserved_bytes,
                legacy.contract_sha256,
            )
            assert legacy.contract_sha256 != source.fallback_contract_sha256
            old_source = replace(source, reserved_bytes=legacy.reserved_bytes, fallback_contract_sha256=legacy.contract_sha256)
            with conn.begin_nested() as savepoint:
                conn.execute(
                    update(holds)
                    .where(holds.c.session_id == source.session_id, holds.c.effect_key == source.effect_key)
                    .values(reserved_bytes=legacy.reserved_bytes, fallback_contract_sha256=legacy.contract_sha256)
                )
                with pytest.raises(AuditIntegrityError, match="SDK-capable combined hold recipe"):
                    require_classifier_sdk_cold_source_on_connection(
                        conn,
                        source=old_source,
                        admitted=admitted,
                        identity=identity,
                        effect=effect,
                        physical_row=physical,
                        parent=parent,
                        names=issuer.inventory.names,
                    )
                savepoint.rollback()
            with conn.begin_nested() as savepoint:
                conn.execute(
                    update(effects)
                    .where(effects.c.session_id == source.session_id, effects.c.effect_key == source.effect_key)
                    .values(requested_model="foreign/model")
                )
                with pytest.raises(AuditIntegrityError, match="original complete physical request"):
                    read_v4_sdk_terminal_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=source.operation_id,
                        effect_key=source.effect_key,
                        family=SdkTerminalFamily.CLASSIFIER,
                        names=issuer.inventory.names,
                    )
                savepoint.rollback()


@pytest.mark.asyncio
async def test_primary_sdk_producer_label_cannot_replace_original_classifier_source(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, store = await _v4_primary_input(operation_store, tmp_path, monkeypatch)

    async def completion(**_arguments):
        raise OpenAIError("original ordinary PRIMARY failure")

    monkeypatch.setattr(litellm, "acompletion", completion)
    await invoke_primary(kwargs, produced, recorder, store)
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]), effects.c.disposition == "sdk_terminal")).one()
        ordinary = read_v4_sdk_terminal_verified_on_connection(
            conn, session_id=store[4], operation_id=effect.operation_id, effect_key=effect.effect_key, family=SdkTerminalFamily.PRIMARY
        )
        assert ordinary is not None and ordinary.receipt.producer == "primary"
        with pytest.raises(AuditIntegrityError, match="classifier PRIMARY lost its original prepared source"):
            read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=store[4],
                operation_id=effect.operation_id,
                effect_key=effect.effect_key,
                family=SdkTerminalFamily.CLASSIFIER,
            )


@pytest.mark.asyncio
async def test_actual_driver_releases_classifier_sdk_terminal_without_downstream_tools_or_retry(operation_store, tmp_path, monkeypatch):
    # Use the existing genuine V4 metadata/P4 fixture before any classifier
    # stage. Its work factory receives the real claimed context and DB hard
    # deadline, just as the existing classifier/PRIMARY driver fixtures do.
    actual_work = classifier_fixture.initialization.inventory_work
    original_input = phase_fixture.initialized_primary_input

    async def capped_input(current_store, current_tmp_path, current_monkeypatch, *, settings_overrides=None):
        # Original admission/service construction must actually make this first
        # metadata mutation reach the shared classifier's last-chance branch.
        return await original_input(
            current_store,
            current_tmp_path,
            current_monkeypatch,
            settings_overrides={**(settings_overrides or {}), "composer_max_composition_turns": 1},
        )

    def timed_work(running):
        stored = operation_store[2].load_continuation_snapshot(running)
        root = decode_continuation_snapshot(stored.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled = monotonic()
        with operation_store[0].connect() as conn:
            observed = database_now(conn)
            deadline = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).scalar_one()
        assert deadline is not None
        from elspeth.web.sessions.time_normalization import restore_utc

        clock = ContinuationSQLDeadlineOwner(
            running,
            policy=policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=continuation_test_settings().composer_async_drain_seconds,
            hard_expires_at=sampled + (restore_utc(deadline) - observed).total_seconds(),
        )
        return actual_work(running, sql_deadline_owner=clock)

    monkeypatch.setattr(classifier_fixture.initialization, "inventory_work", timed_work)
    monkeypatch.setattr(phase_fixture, "initialized_primary_input", capped_input)
    async with _v4_applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, _selected, _inventory, _captured, prior_sdk):
        store = (operation_store[0], operation_store[1], owner.authority, operation_store[3], operation_store[4])
        clock = owner.work.coordinator.sql_deadline_owner
        assert type(clock) is ContinuationSQLDeadlineOwner
        # The preparation fixture has an actual warm runtime with its own
        # progress sink. Give the real driver a fresh phase owner; start_progress
        # must bind its real sink before the retained applied-P4 runtime restores.
        cold = ContinuationLivePhases(owner.service, owner.authority, owner.work, owner.provider_owner, owner.lease)
        assert cold.runtime is None
        driver = _actual_failure_driver(store, cold, running, drain_signal=clock.drain_signal)
        driver.work, driver.provider = cold.work, cold.provider_owner
        assert owner.service._max_composition_turns == 1
        # Establish the real unpaid fold through its shared producer before
        # testing a separately admitted classifier action. This fixture setup
        # does not claim the driver's raw-P4 SQL action meets its 15 s bound.
        assert await advance_continuation_iteration(owner, driver.services, _selected) is None
        ready = owner.authority.load_continuation_snapshot(running)
        with store[0].connect() as conn:
            ready_stage = read_iteration_stage_on_connection(conn, running, selected=ready)
        assert ready_stage is not None and ready_stage.stage == "classify_ready"
        assert ready.cursor == _selected.stack.cursor + 1 and cold.runtime is None
        ready_selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=ready, required_work=owner.work
        )
        assert applied_batch_cut(ready_selected.stack.frames)[1].response == applied_batch_cut(_selected.stack.frames)[1].response
        key = running.claim.session_id, running.claim.operation_id
        cancellation = asyncio.Event()
        assert key not in driver.worker._local_cancels
        driver.worker._local_cancels[key] = cancellation
        calls = []

        async def completion(**_arguments):
            calls.append(1)
            raise OpenAIError("private classifier SDK failure")

        with store[0].connect() as conn:
            prior_tools = tuple(
                conn.execute(
                    select(effects.c.effect_key, effects.c.disposition)
                    .where(effects.c.session_id == str(running.claim.session_id), effects.c.owner.in_(("tool_dispatch", "read")))
                    .order_by(effects.c.effect_key)
                )
            )
        monkeypatch.setattr(litellm, "acompletion", completion)
        try:
            # Observe the first original phase error at its actual caller,
            # before any public terminal projection can erase that traceback.
            with monkeypatch.context() as observation_patch:
                originals = observe_original_driver_failures(
                    observation_patch, running.claim.operation_id, session_id=running.claim.session_id
                )
                try:
                    outcome = await driver.run(None)
                finally:
                    (tmp_path / "classifier-driver-originals.json").write_text(json.dumps(originals, indent=2) + "\n", encoding="utf-8")
            assert type(outcome) is ContinuationSegmentSettled and outcome.terminal.status == "failed"
            assert outcome.terminal.failure_code == "http_error", (outcome.terminal.result_json, originals)
            assert outcome.terminal.result_json is not None
            assert json.loads(outcome.terminal.result_json)["error_type"] == "provider_sdk_terminal"
            with store[0].connect() as conn:
                effect = conn.execute(
                    select(effects).where(effects.c.session_id == str(running.claim.session_id), effects.c.disposition == "sdk_terminal")
                ).one()
                cold = read_v4_sdk_terminal_verified_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    effect_key=effect.effect_key,
                    family=SdkTerminalFamily.CLASSIFIER,
                )
                parent = conn.execute(
                    select(jobs).where(
                        jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id
                    )
                ).one()
                physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
                assert (
                    tuple(
                        conn.execute(
                            select(effects.c.effect_key, effects.c.disposition)
                            .where(effects.c.session_id == str(running.claim.session_id), effects.c.owner.in_(("tool_dispatch", "read")))
                            .order_by(effects.c.effect_key)
                        )
                    )
                    == prior_tools
                )
            assert cold is not None and cold.hold_state == "consumed" and not cold.receipt.retryable
            assert (outcome.terminal.checkpoint_id, outcome.terminal.journal_cursor) == (cold.terminal.checkpoint_id, cold.terminal.cursor)
            assert parent.status == "failed" and "provider_sdk_terminal" in parent.result_json
            assert "private classifier SDK failure" not in parent.result_json
            assert physical.settled_at is not None and physical.ledger_entry_id is not None
            assert calls == [1] and len(prior_sdk) == 1
            owner.work.coordinator.assert_completed()
        finally:
            await driver.close_progress()
            assert driver.worker._local_cancels.pop(key) is cancellation
